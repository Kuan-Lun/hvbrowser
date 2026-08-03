import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hvbrowser import (
    LotteryClient,
    LotteryKind,
    LotteryPageError,
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
    MonsterLabClient,
)
from hvbrowser.maintenance_navigation import (
    classify_maintenance_navigation_blocker,
    select_bazaar_for_maintenance,
)


def _markers(
    *,
    challenge: bool = False,
    completion: bool = False,
    next_floor: bool = False,
    active: bool = False,
) -> dict[str, bool]:
    return {
        "challenge": challenge,
        "completion": completion,
        "nextFloor": next_floor,
        "active": active,
    }


def _driver(page: object) -> SimpleNamespace:
    return SimpleNamespace(page=page, gohomepage=AsyncMock())


class MaintenanceMarkerClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_marker_snapshot_is_atomic_and_risk_prioritized(self) -> None:
        cases = (
            (
                _markers(
                    challenge=True,
                    completion=True,
                    next_floor=True,
                    active=True,
                ),
                MaintenanceNavigationBlocker.CHALLENGE,
            ),
            (
                _markers(completion=True, next_floor=True, active=True),
                MaintenanceNavigationBlocker.COMPLETION,
            ),
            (
                _markers(next_floor=True, active=True),
                MaintenanceNavigationBlocker.NEXT_FLOOR,
            ),
            (_markers(active=True), MaintenanceNavigationBlocker.ACTIVE),
            (_markers(), None),
        )

        for payload, expected in cases:
            with self.subTest(expected=expected):
                page = SimpleNamespace(evaluate=AsyncMock(return_value=payload))

                observed = await classify_maintenance_navigation_blocker(page)

                self.assertEqual(observed, expected)
                page.evaluate.assert_awaited_once()
                script = page.evaluate.await_args.args[0]
                for marker in (
                    "riddlesubmit",
                    "finishbattle.png",
                    "btcp",
                    "battle_main",
                ):
                    self.assertIn(marker, script)

    async def test_invalid_atomic_marker_payload_fails_closed(self) -> None:
        invalid_payloads = (
            None,
            [],
            {},
            {**_markers(), "active": 1},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                page = SimpleNamespace(evaluate=AsyncMock(return_value=payload))
                with self.assertRaisesRegex(RuntimeError, "marker payload"):
                    await classify_maintenance_navigation_blocker(page)

    def test_public_blocker_error_has_stable_typed_detail(self) -> None:
        error = MaintenanceNavigationBlockedError(MaintenanceNavigationBlocker.ACTIVE)

        self.assertIs(error.blocker, MaintenanceNavigationBlocker.ACTIVE)
        self.assertEqual(
            str(error),
            "Maintenance navigation blocked: battle_state=active",
        )


class MaintenanceBazaarNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_landing_battle_marker_blocks_before_bazaar_selection(
        self,
    ) -> None:
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                payload = _markers(
                    challenge=blocker is MaintenanceNavigationBlocker.CHALLENGE,
                    completion=blocker is MaintenanceNavigationBlocker.COMPLETION,
                    next_floor=blocker is MaintenanceNavigationBlocker.NEXT_FLOOR,
                    active=blocker is MaintenanceNavigationBlocker.ACTIVE,
                )
                page = SimpleNamespace(
                    evaluate=AsyncMock(return_value=payload),
                    select=AsyncMock(),
                )
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await select_bazaar_for_maintenance(driver)

                self.assertIs(raised.exception.blocker, blocker)
                driver.gohomepage.assert_awaited_once_with(force=True)
                page.select.assert_not_awaited()

    async def test_initial_completion_can_leave_for_persistent_maintenance(
        self,
    ) -> None:
        bazaar = object()
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers()),
            select=AsyncMock(return_value=bazaar),
        )
        driver = _driver(page)

        selected = await select_bazaar_for_maintenance(driver)

        self.assertIs(selected, bazaar)
        driver.gohomepage.assert_awaited_once_with(force=True)
        page.evaluate.assert_awaited_once()

    async def test_marker_appearing_during_timeout_blocks_without_reload(
        self,
    ) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=[_markers(), _markers(active=True)]),
            select=AsyncMock(side_effect=TimeoutError("missing Bazaar")),
        )
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await select_bazaar_for_maintenance(driver)

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        self.assertIsInstance(raised.exception.__cause__, TimeoutError)
        driver.gohomepage.assert_awaited_once_with(force=True)
        page.select.assert_awaited_once_with("#parent_Bazaar")

    async def test_timeout_without_markers_allows_one_retry(self) -> None:
        bazaar = object()
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=[_markers()] * 5),
            select=AsyncMock(side_effect=[TimeoutError("still loading"), bazaar]),
        )
        driver = _driver(page)

        selected = await select_bazaar_for_maintenance(driver)

        self.assertIs(selected, bazaar)
        self.assertEqual(driver.gohomepage.await_count, 2)
        self.assertEqual(page.select.await_count, 2)

    async def test_none_without_markers_allows_one_retry(self) -> None:
        bazaar = object()
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=[_markers()] * 5),
            select=AsyncMock(side_effect=[None, bazaar]),
        )
        driver = _driver(page)

        selected = await select_bazaar_for_maintenance(driver)

        self.assertIs(selected, bazaar)
        self.assertEqual(driver.gohomepage.await_count, 2)
        self.assertEqual(page.select.await_count, 2)

    async def test_second_marker_free_timeout_is_not_retried_again(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=[_markers()] * 6),
            select=AsyncMock(side_effect=TimeoutError("missing Bazaar")),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(TimeoutError, "missing Bazaar"):
            await select_bazaar_for_maintenance(driver)

        self.assertEqual(driver.gohomepage.await_count, 2)
        self.assertEqual(page.select.await_count, 2)

    async def test_second_marker_free_none_is_not_retried_again(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=[_markers()] * 6),
            select=AsyncMock(return_value=None),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(TimeoutError, "returned no element"):
            await select_bazaar_for_maintenance(driver)

        self.assertEqual(driver.gohomepage.await_count, 2)
        self.assertEqual(page.select.await_count, 2)

    async def test_non_timeout_selection_error_is_never_retried(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers()),
            select=AsyncMock(side_effect=RuntimeError("disconnected")),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            await select_bazaar_for_maintenance(driver)

        driver.gohomepage.assert_awaited_once_with(force=True)
        page.select.assert_awaited_once_with("#parent_Bazaar")


class MaintenanceClientIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lottery_marker_free_navigation_opens_requested_menu(
        self,
    ) -> None:
        bazaar = SimpleNamespace(mouse_move=AsyncMock())
        menu_entry = SimpleNamespace(
            mouse_move=AsyncMock(),
            mouse_click=AsyncMock(),
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers()),
            select=AsyncMock(return_value=bazaar),
            xpath=AsyncMock(return_value=[menu_entry]),
            wait=AsyncMock(),
        )
        driver = _driver(page)

        await LotteryClient(driver)._navigate(LotteryKind.ARMOR)

        driver.gohomepage.assert_awaited_once_with(force=True)
        page.xpath.assert_awaited_once_with(
            "//div[contains(text(), 'Armor Lottery')]", timeout=5
        )
        bazaar.mouse_move.assert_awaited_once_with()
        menu_entry.mouse_move.assert_awaited_once_with()
        menu_entry.mouse_click.assert_awaited_once_with()
        page.wait.assert_awaited_once_with(1)

    async def test_lottery_propagates_typed_battle_block_without_mutation(
        self,
    ) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers(active=True)),
            select=AsyncMock(),
            xpath=AsyncMock(),
        )
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await LotteryClient(driver).inspect(LotteryKind.WEAPON)

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        page.select.assert_not_awaited()
        page.xpath.assert_not_awaited()

    async def test_monster_lab_propagates_typed_challenge_without_mutation(
        self,
    ) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers(challenge=True)),
            select=AsyncMock(),
            xpath=AsyncMock(),
        )
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await MonsterLabClient(driver).inspect()

        self.assertIs(
            raised.exception.blocker,
            MaintenanceNavigationBlocker.CHALLENGE,
        )
        page.select.assert_not_awaited()
        page.xpath.assert_not_awaited()

    async def test_lottery_wraps_non_timeout_bazaar_error_without_retry(
        self,
    ) -> None:
        selection_error = RuntimeError("disconnected")
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers()),
            select=AsyncMock(side_effect=selection_error),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(LotteryPageError, "Bazaar menu") as raised:
            await LotteryClient(driver).inspect(LotteryKind.ARMOR)

        self.assertIs(raised.exception.__cause__, selection_error)
        driver.gohomepage.assert_awaited_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
