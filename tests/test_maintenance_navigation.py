import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hvbrowser import (
    LotteryClient,
    LotteryKind,
    LotteryPageError,
    LotterySnapshot,
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
    MaintenanceNavigator,
    MonsterLabClient,
    MonsterLabPageError,
    MonsterLabSnapshot,
    Realm,
    RealmNavigator,
    classify_maintenance_navigation_blocker,
)
from hvbrowser.runtime import ZendriverOperationTimeout


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
        wait=AsyncMock(),
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
    async def test_bazaar_selector_hang_is_terminal_without_probe_or_retry(
        self,
    ) -> None:
        release = asyncio.Event()

        async def hang(*_args: object, **_kwargs: object) -> object:
            await release.wait()
            return object()

        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers()),
            select=AsyncMock(side_effect=hang),
        )
        driver = _driver(page)

        with (
            patch(
                "hvbrowser.maintenance_navigation._SELECTOR_OUTER_TIMEOUT_SECONDS",
                0.01,
            ),
            self.assertRaises(ZendriverOperationTimeout) as raised,
        ):
            await _navigation(driver).select_bazaar(
                Realm.PERSISTENT,
                navigate_first=False,
            )

        self.assertEqual(raised.exception.timeout_seconds, 0.01)
        page.evaluate.assert_awaited_once()
        page.select.assert_awaited_once()
        driver.gohomepage.assert_not_awaited()
        driver.get.assert_not_awaited()
        release.set()
        await asyncio.sleep(0)

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
    async def test_direct_navigation_timeout_never_runs_same_browser_probe(
        self,
    ) -> None:
        for client_name in ("lottery", "monster-lab"):
            with self.subTest(client=client_name):
                page = SimpleNamespace(evaluate=AsyncMock(return_value=_markers()))
                driver = _driver(page)
                timeout = ZendriverOperationTimeout(timeout_seconds=0.01)
                driver.get.side_effect = timeout

                if client_name == "lottery":
                    operation = LotteryClient(
                        driver,
                        _navigation(driver),
                    )._open_directly(LotteryKind.WEAPON)
                else:
                    operation = MonsterLabClient(
                        driver,
                        _navigation(driver),
                    )._open_directly()

                with self.assertRaises(ZendriverOperationTimeout) as raised:
                    await operation

                self.assertIs(raised.exception, timeout)
                page.evaluate.assert_awaited_once()

    async def test_lottery_opens_requested_menu_through_shared_navigation(
        self,
    ) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=la"
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        bazaar = SimpleNamespace(mouse_move=AsyncMock())
        menu_entry = SimpleNamespace(
            mouse_move=AsyncMock(),
            mouse_click=AsyncMock(),
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=bazaar),
            xpath=AsyncMock(return_value=[menu_entry]),
        )
        driver = _driver(page)

        await LotteryClient(driver, _navigation(driver))._navigate(LotteryKind.ARMOR)

        menu_xpath = page.xpath.await_args.args[0]
        self.assertIn("//*[@id='child_Bazaar']", menu_xpath)
        self.assertIn("contains(@onclick, 'ss=la')", menu_xpath)
        self.assertIn("contains(@href, 'ss=la')", menu_xpath)
        bazaar.mouse_move.assert_awaited_once_with()
        driver.wait.assert_awaited_once_with(
            menu_entry.mouse_click,
            ischangeurl=True,
            owner=menu_entry,
            operation_timeout=15.0,
        )
        driver.get.assert_not_awaited()

    async def test_lottery_unchanged_menu_route_uses_direct_url_once(self) -> None:
        urls = iter(
            [
                "https://hentaiverse.org/",
                "https://hentaiverse.org/?s=Bazaar&ss=lt",
            ]
        )

        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return next(urls)
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        bazaar = SimpleNamespace(mouse_move=AsyncMock())
        menu_entry = SimpleNamespace(
            mouse_move=AsyncMock(),
            mouse_click=AsyncMock(),
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=bazaar),
            xpath=AsyncMock(return_value=[menu_entry]),
        )
        driver = _driver(page)

        with self.assertLogs("hvbrowser.lottery", level="WARNING") as captured:
            await LotteryClient(driver, _navigation(driver))._navigate(
                LotteryKind.WEAPON
            )

        driver.wait.assert_awaited_once_with(
            menu_entry.mouse_click,
            ischangeurl=True,
            owner=menu_entry,
            operation_timeout=15.0,
        )
        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=lt")
        self.assertEqual(driver.get.await_count, 1)
        self.assertIn("retrying once", captured.output[0])

    async def test_lottery_correct_route_reloads_once_after_read_error(self) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=lt"
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        bazaar = SimpleNamespace(mouse_move=AsyncMock())
        menu_entry = SimpleNamespace(
            mouse_move=AsyncMock(),
            mouse_click=AsyncMock(),
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=bazaar),
            xpath=AsyncMock(
                side_effect=[
                    [menu_entry],
                    [],
                    [SimpleNamespace(text="You currently have 1,600,000 GP")],
                    [SimpleNamespace(text="You hold 200 tickets")],
                ]
            ),
        )
        driver = _driver(page)

        with self.assertLogs("hvbrowser.lottery", level="WARNING") as captured:
            snapshot = await LotteryClient(driver, _navigation(driver)).inspect(
                LotteryKind.WEAPON
            )

        self.assertEqual(
            snapshot,
            LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200),
        )
        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=lt")
        self.assertEqual(driver.get.await_count, 1)
        self.assertIn("not readable after navigation", captured.output[0])

    async def test_lottery_second_read_error_is_not_retried(self) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=la"
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        bazaar = SimpleNamespace(mouse_move=AsyncMock())
        menu_entry = SimpleNamespace(
            mouse_move=AsyncMock(),
            mouse_click=AsyncMock(),
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=bazaar),
            xpath=AsyncMock(side_effect=[[menu_entry], [], []]),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(LotteryPageError, "GP balance is missing"):
            await LotteryClient(driver, _navigation(driver)).inspect(LotteryKind.ARMOR)

        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=la")
        self.assertEqual(driver.get.await_count, 1)
        self.assertEqual(page.xpath.await_count, 3)

    async def test_lottery_direct_fallback_rejects_wrong_realm(self) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/isekai/?s=Bazaar&ss=lt"
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=object()),
            xpath=AsyncMock(return_value=[]),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(LotteryPageError, "wrong realm"):
            await LotteryClient(driver, _navigation(driver))._navigate(
                LotteryKind.WEAPON
            )

        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=lt")
        self.assertEqual(driver.get.await_count, 1)

    async def test_lottery_battle_before_direct_fallback_stops_safely(self) -> None:
        marker_results = iter(
            [
                _markers(),
                _markers(),
                _markers(active=True),
            ]
        )

        async def evaluate(script: str) -> object:
            if "nextFloor" in script and "battle_main" in script:
                return next(marker_results)
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=object()),
            xpath=AsyncMock(return_value=[]),
        )
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await LotteryClient(driver, _navigation(driver))._navigate(
                LotteryKind.ARMOR
            )

        self.assertIs(
            raised.exception.blocker,
            MaintenanceNavigationBlocker.ACTIVE,
        )
        driver.get.assert_not_awaited()

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

    async def test_monster_lab_opens_clickable_menu_and_verifies_route(self) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=ml"
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        bazaar = SimpleNamespace(mouse_move=AsyncMock())
        menu_entry = SimpleNamespace(
            mouse_move=AsyncMock(),
            mouse_click=AsyncMock(),
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=bazaar),
            xpath=AsyncMock(return_value=[menu_entry]),
        )
        driver = _driver(page)

        await MonsterLabClient(driver, _navigation(driver))._navigate()

        menu_xpath = page.xpath.await_args.args[0]
        self.assertIn("//*[@id='child_Bazaar']", menu_xpath)
        self.assertIn("contains(@onclick, 'ss=ml')", menu_xpath)
        self.assertIn("contains(@href, 'ss=ml')", menu_xpath)
        bazaar.mouse_move.assert_awaited_once_with()
        driver.wait.assert_awaited_once_with(
            menu_entry.mouse_click,
            ischangeurl=True,
            owner=menu_entry,
            operation_timeout=15.0,
        )
        driver.get.assert_not_awaited()

    async def test_monster_lab_unchanged_menu_route_uses_direct_url_once(self) -> None:
        urls = iter(
            [
                "https://hentaiverse.org/",
                "https://hentaiverse.org/?s=Bazaar&ss=ml",
            ]
        )

        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return next(urls)
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        bazaar = SimpleNamespace(mouse_move=AsyncMock())
        menu_entry = SimpleNamespace(
            mouse_move=AsyncMock(),
            mouse_click=AsyncMock(),
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=bazaar),
            xpath=AsyncMock(return_value=[menu_entry]),
        )
        driver = _driver(page)

        with self.assertLogs("hvbrowser.monster_lab", level="WARNING") as captured:
            await MonsterLabClient(driver, _navigation(driver))._navigate()

        driver.wait.assert_awaited_once_with(
            menu_entry.mouse_click,
            ischangeurl=True,
            owner=menu_entry,
            operation_timeout=15.0,
        )
        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=ml")
        self.assertEqual(driver.get.await_count, 1)
        self.assertIn("retrying once", captured.output[0])

    async def test_monster_lab_correct_route_reloads_once_after_read_error(
        self,
    ) -> None:
        api_results = iter([False, True])

        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=ml"
            if script == "typeof do_feed_all === 'function'":
                return next(api_results)
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        menu_entry = SimpleNamespace(
            mouse_move=AsyncMock(),
            mouse_click=AsyncMock(),
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=SimpleNamespace(mouse_move=AsyncMock())),
            xpath=AsyncMock(side_effect=[[menu_entry], [], []]),
        )
        driver = _driver(page)

        with self.assertLogs("hvbrowser.monster_lab", level="WARNING") as captured:
            snapshot = await MonsterLabClient(driver, _navigation(driver)).inspect()

        self.assertEqual(snapshot, MonsterLabSnapshot(frozenset()))
        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=ml")
        self.assertEqual(driver.get.await_count, 1)
        self.assertIn("not readable after navigation", captured.output[0])

    async def test_monster_lab_second_read_error_is_not_retried(self) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=ml"
            if script == "typeof do_feed_all === 'function'":
                return False
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        menu_entry = SimpleNamespace(
            mouse_move=AsyncMock(),
            mouse_click=AsyncMock(),
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=SimpleNamespace(mouse_move=AsyncMock())),
            xpath=AsyncMock(return_value=[menu_entry]),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(MonsterLabPageError, "API is missing"):
            await MonsterLabClient(driver, _navigation(driver)).inspect()

        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=ml")
        self.assertEqual(driver.get.await_count, 1)

    async def test_monster_lab_caps_menu_fallback_and_read_retry_at_two_direct_opens(
        self,
    ) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=ml"
            if script == "typeof do_feed_all === 'function'":
                return False
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=SimpleNamespace(mouse_move=AsyncMock())),
            xpath=AsyncMock(return_value=[]),
        )
        driver = _driver(page)

        with self.assertLogs("hvbrowser.monster_lab", level="WARNING") as captured:
            with self.assertRaisesRegex(MonsterLabPageError, "API is missing"):
                await MonsterLabClient(driver, _navigation(driver)).inspect()

        expected_url = "https://hentaiverse.org/?s=Bazaar&ss=ml"
        self.assertEqual(
            [call.args for call in driver.get.await_args_list],
            [(expected_url,), (expected_url,)],
        )
        api_checks = [
            call
            for call in page.evaluate.await_args_list
            if call.args == ("typeof do_feed_all === 'function'",)
        ]
        self.assertEqual(len(api_checks), 2)
        self.assertEqual(len(captured.output), 2)
        self.assertIn("retrying once", captured.output[0])
        self.assertIn("not readable after navigation", captured.output[1])

    async def test_monster_lab_direct_fallback_rejects_untrusted_destination(
        self,
    ) -> None:
        cases = (
            (
                "https://hentaiverse.org/isekai/?s=Bazaar&ss=ml",
                "wrong realm",
            ),
            (
                "https://example.test/?s=Bazaar&ss=ml",
                "verify the Monster Lab URL",
            ),
            (
                "https://hentaiverse.org/unexpected?s=Bazaar&ss=ml",
                "unexpected path",
            ),
            (
                "https://hentaiverse.org/?s=Bazaar&ss=la",
                "requested route",
            ),
        )

        for current_url, message in cases:
            with self.subTest(current_url=current_url):

                async def evaluate(script: str) -> object:
                    if script == "window.location.href":
                        return current_url
                    if "nextFloor" in script and "battle_main" in script:
                        return _markers()
                    raise AssertionError(f"Unexpected evaluate script: {script!r}")

                page = SimpleNamespace(evaluate=AsyncMock(side_effect=evaluate))
                driver = _driver(page)

                with self.assertRaisesRegex(MonsterLabPageError, message):
                    await MonsterLabClient(
                        driver,
                        _navigation(driver),
                    )._open_directly()

                driver.get.assert_awaited_once_with(
                    "https://hentaiverse.org/?s=Bazaar&ss=ml"
                )

    async def test_monster_lab_battle_before_direct_fallback_stops_safely(
        self,
    ) -> None:
        marker_results = iter(
            [
                _markers(),
                _markers(),
                _markers(active=True),
            ]
        )

        async def evaluate(script: str) -> object:
            if "nextFloor" in script and "battle_main" in script:
                return next(marker_results)
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=object()),
            xpath=AsyncMock(return_value=[]),
        )
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await MonsterLabClient(driver, _navigation(driver))._navigate()

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        driver.get.assert_not_awaited()

    async def test_monster_lab_battle_after_direct_navigation_stops_safely(
        self,
    ) -> None:
        marker_results = iter([_markers(), _markers(active=True)])

        async def evaluate(script: str) -> object:
            if "nextFloor" in script and "battle_main" in script:
                return next(marker_results)
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(evaluate=AsyncMock(side_effect=evaluate))
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await MonsterLabClient(driver, _navigation(driver))._open_directly()

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=ml")

    async def test_lottery_bazaar_error_uses_direct_url(self) -> None:
        selection_error = RuntimeError("disconnected")

        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=la"
            if "nextFloor" in script and "battle_main" in script:
                return _markers()
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(side_effect=selection_error),
            xpath=AsyncMock(
                side_effect=[
                    [SimpleNamespace(text="You currently have 1,600,000 GP")],
                    [SimpleNamespace(text="You hold 100 tickets")],
                ]
            ),
        )
        driver = _driver(page)

        snapshot = await LotteryClient(driver, _navigation(driver)).inspect(
            LotteryKind.ARMOR
        )

        self.assertEqual(snapshot.tickets, 100)
        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=la")


if __name__ == "__main__":
    unittest.main()
