import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hvbrowser import (
    LotteryClient,
    LotteryKind,
    LotteryPageError,
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
    MaintenanceNavigator,
    MonsterLabClient,
    Realm,
    RealmNavigator,
    classify_maintenance_navigation_blocker,
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
    return SimpleNamespace(
        page=page,
        get=AsyncMock(),
        gohomepage=AsyncMock(),
    )


def _navigation(driver: SimpleNamespace) -> MaintenanceNavigator:
    return MaintenanceNavigator(driver, RealmNavigator(driver))


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
                script = page.evaluate.await_args.args[0]
                for marker in (
                    "riddlesubmit",
                    "finishbattle.png",
                    "btcp",
                    "battle_main",
                ):
                    self.assertIn(marker, script)

    async def test_invalid_marker_payload_fails_closed(self) -> None:
        for payload in (None, [], {}, {**_markers(), "active": 1}):
            with self.subTest(payload=payload):
                page = SimpleNamespace(evaluate=AsyncMock(return_value=payload))
                with self.assertRaisesRegex(RuntimeError, "marker payload"):
                    await classify_maintenance_navigation_blocker(page)


class MaintenanceNavigatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_landing_marker_blocks_before_bazaar_selection(self) -> None:
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                payload = _markers(
                    challenge=blocker is MaintenanceNavigationBlocker.CHALLENGE,
                    completion=blocker is MaintenanceNavigationBlocker.COMPLETION,
                    next_floor=blocker is MaintenanceNavigationBlocker.NEXT_FLOOR,
                    active=blocker is MaintenanceNavigationBlocker.ACTIVE,
                )
                page = SimpleNamespace(
                    evaluate=AsyncMock(side_effect=[_markers(), payload]),
                    select=AsyncMock(),
                )
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await _navigation(driver).select_bazaar(
                        Realm.PERSISTENT,
                        navigate_first=True,
                    )

                self.assertIs(raised.exception.blocker, blocker)
                driver.gohomepage.assert_awaited_once_with(force=True)
                page.select.assert_not_awaited()

    async def test_unsafe_initial_markers_block_before_navigation(self) -> None:
        for blocker in (
            MaintenanceNavigationBlocker.CHALLENGE,
            MaintenanceNavigationBlocker.NEXT_FLOOR,
            MaintenanceNavigationBlocker.ACTIVE,
        ):
            with self.subTest(blocker=blocker):
                payload = _markers(
                    challenge=blocker is MaintenanceNavigationBlocker.CHALLENGE,
                    next_floor=blocker is MaintenanceNavigationBlocker.NEXT_FLOOR,
                    active=blocker is MaintenanceNavigationBlocker.ACTIVE,
                )
                page = SimpleNamespace(
                    evaluate=AsyncMock(return_value=payload),
                    select=AsyncMock(),
                )
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await _navigation(driver).select_bazaar(
                        Realm.PERSISTENT,
                        navigate_first=True,
                    )

                self.assertIs(raised.exception.blocker, blocker)
                driver.gohomepage.assert_not_awaited()
                driver.get.assert_not_awaited()
                page.select.assert_not_awaited()

    async def test_initial_completion_may_leave_for_persistent_maintenance(
        self,
    ) -> None:
        bazaar = object()
        page = SimpleNamespace(
            evaluate=AsyncMock(
                side_effect=[
                    _markers(completion=True),
                    _markers(),
                ]
            ),
            select=AsyncMock(return_value=bazaar),
        )
        driver = _driver(page)

        selected = await _navigation(driver).select_bazaar(
            Realm.PERSISTENT,
            navigate_first=True,
        )

        self.assertIs(selected, bazaar)
        driver.gohomepage.assert_awaited_once_with(force=True)

    async def test_timeout_without_markers_allows_one_same_realm_retry(self) -> None:
        bazaar = object()
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=[_markers()] * 6),
            select=AsyncMock(side_effect=[TimeoutError("loading"), bazaar]),
        )
        driver = _driver(page)

        selected = await _navigation(driver).select_bazaar(
            Realm.ISEKAI,
            navigate_first=True,
        )

        self.assertIs(selected, bazaar)
        self.assertEqual(driver.get.await_count, 2)
        driver.gohomepage.assert_not_awaited()

    async def test_marker_appearing_during_timeout_blocks_without_retry(
        self,
    ) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(
                side_effect=[
                    _markers(),
                    _markers(),
                    _markers(active=True),
                ]
            ),
            select=AsyncMock(side_effect=TimeoutError("missing Bazaar")),
        )
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await _navigation(driver).select_bazaar(
                Realm.PERSISTENT,
                navigate_first=True,
            )

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        self.assertIsInstance(raised.exception.__cause__, TimeoutError)

    async def test_repair_style_navigation_classifies_before_moving(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers(active=True)),
            select=AsyncMock(),
        )
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError):
            await _navigation(driver).select_bazaar(
                Realm.ISEKAI,
                navigate_first=False,
            )

        driver.get.assert_not_awaited()
        page.select.assert_not_awaited()


class MaintenanceClientIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lottery_opens_requested_menu_through_shared_navigation(
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

        await LotteryClient(driver, _navigation(driver))._navigate(LotteryKind.ARMOR)

        page.xpath.assert_awaited_once_with(
            "//div[contains(text(), 'Armor Lottery')]",
            timeout=5,
        )
        bazaar.mouse_move.assert_awaited_once_with()
        menu_entry.mouse_click.assert_awaited_once_with()

    async def test_lottery_propagates_typed_battle_block(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers(active=True)),
            select=AsyncMock(),
            xpath=AsyncMock(),
        )
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await LotteryClient(driver, _navigation(driver)).inspect(LotteryKind.WEAPON)

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        driver.gohomepage.assert_not_awaited()
        page.xpath.assert_not_awaited()

    async def test_monster_lab_propagates_typed_challenge(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers(challenge=True)),
            select=AsyncMock(),
            xpath=AsyncMock(),
        )
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await MonsterLabClient(driver, _navigation(driver)).inspect()

        self.assertIs(
            raised.exception.blocker,
            MaintenanceNavigationBlocker.CHALLENGE,
        )
        driver.gohomepage.assert_not_awaited()

    async def test_lottery_wraps_non_timeout_bazaar_error(self) -> None:
        selection_error = RuntimeError("disconnected")
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers()),
            select=AsyncMock(side_effect=selection_error),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(LotteryPageError, "Bazaar menu") as raised:
            await LotteryClient(driver, _navigation(driver)).inspect(LotteryKind.ARMOR)

        self.assertIs(raised.exception.__cause__, selection_error)


if __name__ == "__main__":
    unittest.main()
