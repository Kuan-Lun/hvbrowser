import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hvbrowser.player import (
    PlayerClient,
    PlayerPageError,
    PlayerSnapshot,
    PlayerStateChangedError,
    StaminaRecoveryError,
    StaminaRecoveryOutcome,
)


def _element(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        mouse_move=AsyncMock(),
        mouse_click=AsyncMock(),
        click=AsyncMock(),
    )


def _driver(
    *,
    xpath_results: list[object],
    selected: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        page=SimpleNamespace(
            select=AsyncMock(return_value=selected),
            xpath=AsyncMock(side_effect=xpath_results),
            wait=AsyncMock(),
        )
    )


class PlayerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_reads_level_and_stamina_without_mutation(self) -> None:
        level = _element("PFUDOR Lv.500")
        stamina = _element("Stamina: 83")
        driver = _driver(xpath_results=[[stamina]], selected=level)

        snapshot = await PlayerClient(driver).inspect()

        self.assertEqual(snapshot, PlayerSnapshot(level=500, stamina=83))
        driver.page.select.assert_awaited_once_with("#level_readout", timeout=5)
        driver.page.xpath.assert_awaited_once_with(
            "//div[contains(text(), 'Stamina:')]",
            timeout=5,
        )
        driver.page.wait.assert_not_awaited()

    async def test_readouts_fail_closed_when_text_is_malformed(self) -> None:
        malformed_level = _driver(
            xpath_results=[],
            selected=_element("PFUDOR 500"),
        )
        with self.assertRaisesRegex(PlayerPageError, "parse level"):
            await PlayerClient(malformed_level).read_level()

        malformed_stamina = _driver(
            xpath_results=[[_element("Stamina: unknown")]],
        )
        with self.assertRaisesRegex(PlayerPageError, "parse stamina"):
            await PlayerClient(malformed_stamina).read_stamina()

    async def test_recovery_reports_not_available_without_clicking(self) -> None:
        stamina_readout = _element("Stamina: 80")
        driver = _driver(
            xpath_results=[[_element("Stamina: 80")], []],
            selected=stamina_readout,
        )

        report = await PlayerClient(driver).recover_stamina(expected_before=80)

        self.assertIs(report.outcome, StaminaRecoveryOutcome.NOT_AVAILABLE)
        self.assertFalse(report.recovered)
        self.assertEqual((report.before, report.after), (80, 80))
        stamina_readout.mouse_move.assert_awaited_once_with()
        driver.page.wait.assert_not_awaited()

    async def test_recovery_requires_a_confirmed_stamina_increase(self) -> None:
        restorative = _element()
        driver = _driver(
            xpath_results=[
                [_element("Stamina: 80")],
                [restorative],
                [],
                [_element("Stamina: 95")],
            ],
            selected=_element("Stamina: 80"),
        )

        report = await PlayerClient(driver).recover_stamina(expected_before=80)

        self.assertIs(report.outcome, StaminaRecoveryOutcome.RECOVERED)
        self.assertTrue(report.recovered)
        self.assertEqual((report.before, report.after), (80, 95))
        restorative.mouse_click.assert_awaited_once_with()
        driver.page.wait.assert_awaited_once_with(1)

    async def test_server_rejection_is_a_typed_known_outcome(self) -> None:
        restorative = _element()
        rejection = _element("You cannot use that item now")
        driver = _driver(
            xpath_results=[
                [_element("Stamina: 80")],
                [restorative],
                [rejection],
            ],
            selected=_element("Stamina: 80"),
        )

        report = await PlayerClient(driver).recover_stamina()

        self.assertIs(report.outcome, StaminaRecoveryOutcome.REJECTED)
        self.assertFalse(report.recovered)
        self.assertEqual(report.server_message, "You cannot use that item now")
        rejection.click.assert_awaited_once_with()

    async def test_unchanged_stamina_after_submit_is_unknown(self) -> None:
        driver = _driver(
            xpath_results=[
                [_element("Stamina: 80")],
                [_element()],
                [],
                [_element("Stamina: 80")],
            ],
            selected=_element("Stamina: 80"),
        )

        with self.assertRaisesRegex(StaminaRecoveryError, "did not increase"):
            await PlayerClient(driver).recover_stamina()

    async def test_expected_stamina_prevents_a_stale_recovery(self) -> None:
        driver = _driver(xpath_results=[[_element("Stamina: 79")]])

        with self.assertRaises(PlayerStateChangedError):
            await PlayerClient(driver).recover_stamina(expected_before=80)

        driver.page.select.assert_not_awaited()
        driver.page.wait.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
