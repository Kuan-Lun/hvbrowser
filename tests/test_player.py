import asyncio
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

from hvbrowser import (
    PlayerClient,
    PlayerPageError,
    PlayerSnapshot,
    PlayerStateChangedError,
    StaminaRecoveryError,
    StaminaRecoveryOutcome,
)
from hvbrowser.player import _PlayerPageState
from hvbrowser.runtime import PageStateTimeout, ZendriverOperationTimeout


class _Element:
    def __init__(self, action: Callable[[], None] | None = None) -> None:
        self._action = action
        self.moves = 0
        self.clicks = 0

    async def mouse_move(self) -> None:
        self.moves += 1

    async def mouse_click(self) -> None:
        self.clicks += 1
        if self._action is not None:
            self._action()

    async def click(self) -> None:
        self.clicks += 1


class _Page:
    def __init__(self, *, level: int = 500, stamina: int = 80) -> None:
        self.level = level
        self.stamina = stamina
        self.restorative_available = True
        self.error: str | None = None
        self.on_submit: Callable[[], None] = lambda: setattr(self, "stamina", 95)
        self.stamina_element = _Element()
        self.restorative = _Element(lambda: self.on_submit())
        self.error_element = _Element()
        self.expressions: list[str] = []

    async def evaluate(self, expression: str) -> dict[str, object]:
        self.expressions.append(expression)
        return {
            "levelText": f"Lv. {self.level}",
            "staminaText": f"Stamina: {self.stamina}",
            "hasStaminaReadout": True,
            "restorativeAvailable": self.restorative_available,
            "errorText": self.error,
        }

    async def query_selector(self, selector: str) -> Any:
        if selector == "#stamina_readout":
            return self.stamina_element
        if "recoverform" in selector:
            return self.restorative if self.restorative_available else None
        if selector == "p.messagebox_error":
            return self.error_element if self.error is not None else None
        raise AssertionError(f"unexpected selector: {selector}")


def _client(page: _Page) -> PlayerClient:
    return PlayerClient(type("Driver", (), {"page": page})())


class PlayerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_expected_stamina_is_rejected_before_read(self) -> None:
        page = _Page()
        page.evaluate = AsyncMock(wraps=page.evaluate)

        for invalid in (-1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    await _client(page).recover_stamina(  # type: ignore[arg-type]
                        expected_before=invalid
                    )

        page.evaluate.assert_not_awaited()

    async def test_inspect_uses_one_atomic_snapshot(self) -> None:
        page = _Page(level=612, stamina=76)

        snapshot = await _client(page).inspect()

        self.assertEqual(snapshot, PlayerSnapshot(level=612, stamina=76))
        self.assertEqual(len(page.expressions), 1)

    async def test_malformed_snapshot_fails_closed(self) -> None:
        page = _Page()
        page.evaluate = AsyncMock(
            return_value={
                "levelText": "unknown",
                "staminaText": "Stamina: 80",
                "hasStaminaReadout": True,
                "restorativeAvailable": False,
                "errorText": None,
            }
        )

        with self.assertRaises(PlayerPageError):
            await _client(page).inspect()

    async def test_expected_stamina_prevents_stale_recovery(self) -> None:
        page = _Page(stamina=79)

        with self.assertRaises(PlayerStateChangedError):
            await _client(page).recover_stamina(expected_before=80)

        self.assertEqual(page.restorative.clicks, 0)

    async def test_unavailable_restorative_is_known_without_click(self) -> None:
        page = _Page()
        page.restorative_available = False

        report = await _client(page).recover_stamina()

        self.assertIs(report.outcome, StaminaRecoveryOutcome.NOT_AVAILABLE)
        self.assertEqual(report.after, 80)
        self.assertEqual(page.restorative.clicks, 0)

    async def test_recovery_confirms_state_change_without_fixed_sleep(self) -> None:
        page = _Page(stamina=80)

        report = await _client(page).recover_stamina(expected_before=80)

        self.assertIs(report.outcome, StaminaRecoveryOutcome.RECOVERED)
        self.assertEqual(report.after, 95)
        self.assertEqual(page.restorative.clicks, 1)

    async def test_server_rejection_is_a_typed_known_outcome(self) -> None:
        page = _Page()

        def reject() -> None:
            page.error = "You cannot use that item now"

        page.on_submit = reject

        report = await _client(page).recover_stamina()

        self.assertIs(report.outcome, StaminaRecoveryOutcome.REJECTED)
        self.assertEqual(report.server_message, "You cannot use that item now")
        self.assertEqual(page.error_element.clicks, 1)

    async def test_delayed_success_does_not_replay_submission(self) -> None:
        page = _Page(stamina=80)
        page.on_submit = lambda: None
        confirmed = _PlayerPageState(
            "Lv. 500",
            "Stamina: 95",
            True,
            True,
            None,
        )

        with patch(
            "hvbrowser.player.wait_for_page_state",
            new=AsyncMock(return_value=confirmed),
        ):
            report = await _client(page).recover_stamina()

        self.assertIs(report.outcome, StaminaRecoveryOutcome.RECOVERED)
        self.assertEqual(page.restorative.clicks, 1)

    async def test_semantic_timeout_is_not_a_protocol_timeout(self) -> None:
        page = _Page()
        page.on_submit = lambda: None

        with (
            patch(
                "hvbrowser.player.wait_for_page_state",
                new=AsyncMock(side_effect=PageStateTimeout("unchanged")),
            ),
            self.assertRaisesRegex(StaminaRecoveryError, "did not increase"),
        ):
            await _client(page).recover_stamina()

        self.assertEqual(page.restorative.clicks, 1)

    async def test_submission_hang_is_terminal_without_state_probe(self) -> None:
        page = _Page()
        release = asyncio.Event()

        async def hang() -> None:
            await release.wait()

        page.restorative.mouse_click = AsyncMock(side_effect=hang)  # type: ignore[method-assign]
        state_wait = AsyncMock()
        with (
            patch("hvbrowser.runtime.PROTOCOL_COMMAND_TIMEOUT_SECONDS", 0.01),
            patch("hvbrowser.player.wait_for_page_state", new=state_wait),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await _client(page).recover_stamina()

        state_wait.assert_not_awaited()
        release.set()
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
