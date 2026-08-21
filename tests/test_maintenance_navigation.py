import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hvbrowser import (
    LotteryClient,
    LotteryKind,
    LotteryPageError,
    LotterySnapshot,
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
    MaintenanceNavigationContext,
    MaintenanceNavigationObservation,
    MonsterLabClient,
    MonsterLabPageError,
    MonsterLabSnapshot,
    Realm,
    observe_maintenance_navigation,
)
from hvbrowser.runtime import ZendriverOperationTimeout


def _markers(
    *,
    url: str = "https://hentaiverse.org/",
    challenge: bool = False,
    completion: bool = False,
    next_floor: bool = False,
    active: bool = False,
) -> dict[str, object]:
    return {
        "url": url,
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


class MaintenanceObservationPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_observation_is_risk_prioritized(self) -> None:
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

                observed = await observe_maintenance_navigation(page)

                self.assertEqual(observed.blocker, expected)
                script = page.evaluate.await_args.args[0]
                self.assertIn("window.location.href", script)
                for marker in (
                    "riddlesubmit",
                    "finishbattle.png",
                    "btcp",
                    "battle_main",
                ):
                    self.assertIn(marker, script)

    async def test_invalid_atomic_payload_fails_closed(self) -> None:
        for payload in (None, [], {}, {**_markers(), "active": 1}):
            with self.subTest(payload=payload):
                page = SimpleNamespace(evaluate=AsyncMock(return_value=payload))
                with self.assertRaisesRegex(RuntimeError, "observation payload"):
                    await observe_maintenance_navigation(page)


class MaintenanceNavigationObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_and_markers_are_atomic_and_risk_prioritized(self) -> None:
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

                observed = await observe_maintenance_navigation(page)

                self.assertEqual(
                    observed,
                    MaintenanceNavigationObservation(
                        "https://hentaiverse.org/",
                        Realm.PERSISTENT,
                        expected,
                    ),
                )
                script = page.evaluate.await_args.args[0]
                self.assertIn("window.location.href", script)
                for marker in (
                    "riddlesubmit",
                    "finishbattle.png",
                    "btcp",
                    "battle_main",
                ):
                    self.assertIn(marker, script)

    async def test_invalid_observation_payload_fails_closed(self) -> None:
        for payload in (
            None,
            [],
            {},
            {**_markers(), "url": 1},
            {**_markers(), "active": 1},
        ):
            with self.subTest(payload=payload):
                page = SimpleNamespace(evaluate=AsyncMock(return_value=payload))
                with self.assertRaisesRegex(RuntimeError, "observation payload"):
                    await observe_maintenance_navigation(page)

    async def test_untrusted_origin_preserves_atomic_blocker(self) -> None:
        for url in (
            "https://example.test/",
            "https://hentaiverse.org:0/",
        ):
            with self.subTest(url=url):
                page = SimpleNamespace(
                    evaluate=AsyncMock(
                        return_value=_markers(
                            url=url,
                            active=True,
                        )
                    )
                )

                observed = await observe_maintenance_navigation(page)

                self.assertEqual(
                    observed,
                    MaintenanceNavigationObservation(
                        url,
                        None,
                        MaintenanceNavigationBlocker.ACTIVE,
                    ),
                )

    async def test_generation_error_is_propagated_without_second_evaluation(
        self,
    ) -> None:
        timeout = ZendriverOperationTimeout(timeout_seconds=0.01)
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=timeout))

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await observe_maintenance_navigation(page)

        self.assertIs(raised.exception, timeout)
        page.evaluate.assert_awaited_once()


class MaintenanceTrustRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_get_timeout_is_terminal_without_probe_or_retry(
        self,
    ) -> None:
        page = SimpleNamespace(evaluate=AsyncMock(return_value=_markers()))
        driver = _driver(page)
        timeout = ZendriverOperationTimeout(timeout_seconds=0.01)
        driver.get.side_effect = timeout

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await LotteryClient(driver)._open_directly(LotteryKind.WEAPON)

        self.assertIs(raised.exception, timeout)
        page.evaluate.assert_awaited_once()

    async def test_each_landing_marker_blocks_after_direct_get(self) -> None:
        route = "https://hentaiverse.org/?s=Bazaar&ss=lt"
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                payload = _markers(
                    url=route,
                    challenge=blocker is MaintenanceNavigationBlocker.CHALLENGE,
                    completion=blocker is MaintenanceNavigationBlocker.COMPLETION,
                    next_floor=blocker is MaintenanceNavigationBlocker.NEXT_FLOOR,
                    active=blocker is MaintenanceNavigationBlocker.ACTIVE,
                )
                page = SimpleNamespace(
                    evaluate=AsyncMock(side_effect=[_markers(), payload]),
                )
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await LotteryClient(driver)._open_directly(LotteryKind.WEAPON)

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_awaited_once_with(route)

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
                page = SimpleNamespace(evaluate=AsyncMock(return_value=payload))
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await LotteryClient(driver)._open_directly(LotteryKind.WEAPON)

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_not_awaited()

    async def test_post_battle_context_may_leave_trusted_persistent_completion(
        self,
    ) -> None:
        route = "https://hentaiverse.org/?s=Bazaar&ss=lt"
        page = SimpleNamespace(
            evaluate=AsyncMock(
                side_effect=[
                    _markers(completion=True),
                    _markers(url=route),
                ]
            ),
        )
        driver = _driver(page)

        await LotteryClient(driver)._open_directly(
            LotteryKind.WEAPON,
            context=MaintenanceNavigationContext.POST_BATTLE,
        )

        driver.get.assert_awaited_once_with(route)

    async def test_unreadable_page_allows_one_direct_retry(self) -> None:
        route = "https://hentaiverse.org/?s=Bazaar&ss=lt"
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers(url=route)),
            xpath=AsyncMock(
                side_effect=[
                    [],
                    [SimpleNamespace(text="You currently have 1,600,000 GP")],
                    [SimpleNamespace(text="You hold 200 tickets")],
                ]
            ),
        )
        driver = _driver(page)

        snapshot = await LotteryClient(driver).inspect(
            LotteryKind.WEAPON,
            context=MaintenanceNavigationContext.ORDINARY,
        )

        self.assertEqual(snapshot.tickets, 200)
        self.assertEqual(driver.get.await_count, 2)

    async def test_marker_appearing_during_get_error_is_typed_without_retry(
        self,
    ) -> None:
        navigation_error = RuntimeError("navigation interrupted")
        page = SimpleNamespace(
            evaluate=AsyncMock(
                side_effect=[
                    _markers(),
                    _markers(active=True),
                ]
            ),
        )
        driver = _driver(page)
        driver.get.side_effect = navigation_error

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await LotteryClient(driver)._open_directly(LotteryKind.WEAPON)

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        self.assertIs(raised.exception.__cause__, navigation_error)

    async def test_wrong_path_with_marker_is_safety_error_before_moving(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value=_markers(
                    url="https://hentaiverse.org/unexpected",
                    active=True,
                )
            ),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(LotteryPageError, "unexpected path") as raised:
            await LotteryClient(driver)._open_directly(LotteryKind.WEAPON)

        self.assertNotIsInstance(raised.exception, MaintenanceNavigationBlockedError)
        driver.get.assert_not_awaited()


class MaintenanceTrustContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_untrusted_identity_wins_over_marker_before_direct_get(self) -> None:
        destinations = (
            "https://example.test/",
            "https://hentaiverse.org:0/",
            "https://hentaiverse.org/isekai/",
            "https://hentaiverse.org/unexpected",
        )
        for client_name in ("lottery", "monster-lab"):
            for destination in destinations:
                with self.subTest(client=client_name, destination=destination):
                    page = SimpleNamespace(
                        evaluate=AsyncMock(
                            return_value=_markers(url=destination, active=True)
                        )
                    )
                    driver = _driver(page)
                    if client_name == "lottery":
                        operation = LotteryClient(driver)._open_directly(
                            LotteryKind.WEAPON
                        )
                        error_type = LotteryPageError
                    else:
                        operation = MonsterLabClient(driver)._open_directly()
                        error_type = MonsterLabPageError

                    with self.assertRaises(error_type) as raised:
                        await operation

                    self.assertNotIsInstance(
                        raised.exception,
                        MaintenanceNavigationBlockedError,
                    )
                    driver.get.assert_not_awaited()

    async def test_untrusted_identity_wins_over_marker_after_direct_get(self) -> None:
        destinations = (
            "https://example.test/",
            "https://hentaiverse.org:0/",
            "https://hentaiverse.org/isekai/",
            "https://hentaiverse.org/unexpected",
        )
        for client_name in ("lottery", "monster-lab"):
            for destination in destinations:
                with self.subTest(client=client_name, destination=destination):
                    page = SimpleNamespace(
                        evaluate=AsyncMock(
                            side_effect=[
                                _markers(),
                                _markers(url=destination, active=True),
                            ]
                        )
                    )
                    driver = _driver(page)
                    if client_name == "lottery":
                        operation = LotteryClient(driver)._open_directly(
                            LotteryKind.WEAPON
                        )
                        error_type = LotteryPageError
                    else:
                        operation = MonsterLabClient(driver)._open_directly()
                        error_type = MonsterLabPageError

                    with self.assertRaises(error_type) as raised:
                        await operation

                    self.assertNotIsInstance(
                        raised.exception,
                        MaintenanceNavigationBlockedError,
                    )
                    driver.get.assert_awaited_once()

    async def test_direct_navigation_generation_timeout_has_no_second_probe(
        self,
    ) -> None:
        page = SimpleNamespace(evaluate=AsyncMock(return_value=_markers()))
        driver = _driver(page)
        timeout = ZendriverOperationTimeout(timeout_seconds=0.01)
        driver.get.side_effect = timeout

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await LotteryClient(driver)._open_directly(LotteryKind.WEAPON)

        self.assertIs(raised.exception, timeout)
        page.evaluate.assert_awaited_once()

    async def test_each_trusted_landing_marker_blocks(self) -> None:
        route = "https://hentaiverse.org/?s=Bazaar&ss=lt"
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                payload = _markers(
                    url=route,
                    challenge=blocker is MaintenanceNavigationBlocker.CHALLENGE,
                    completion=blocker is MaintenanceNavigationBlocker.COMPLETION,
                    next_floor=blocker is MaintenanceNavigationBlocker.NEXT_FLOOR,
                    active=blocker is MaintenanceNavigationBlocker.ACTIVE,
                )
                page = SimpleNamespace(
                    evaluate=AsyncMock(side_effect=[_markers(), payload])
                )
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await LotteryClient(driver)._open_directly(LotteryKind.WEAPON)

                self.assertIs(raised.exception.blocker, blocker)

    async def test_ordinary_context_blocks_every_initial_marker(self) -> None:
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                page = SimpleNamespace(
                    evaluate=AsyncMock(
                        return_value=_markers(
                            challenge=(
                                blocker is MaintenanceNavigationBlocker.CHALLENGE
                            ),
                            completion=(
                                blocker is MaintenanceNavigationBlocker.COMPLETION
                            ),
                            next_floor=(
                                blocker is MaintenanceNavigationBlocker.NEXT_FLOOR
                            ),
                            active=blocker is MaintenanceNavigationBlocker.ACTIVE,
                        )
                    )
                )
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await LotteryClient(driver)._open_directly(LotteryKind.WEAPON)

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_not_awaited()

    async def test_post_battle_context_is_required_to_leave_completion(self) -> None:
        route = "https://hentaiverse.org/?s=Bazaar&ss=lt"
        page = SimpleNamespace(
            evaluate=AsyncMock(
                side_effect=[
                    _markers(completion=True),
                    _markers(url=route),
                ]
            )
        )
        driver = _driver(page)

        await LotteryClient(driver)._open_directly(
            LotteryKind.WEAPON,
            context=MaintenanceNavigationContext.POST_BATTLE,
        )

        driver.get.assert_awaited_once_with(route)

    async def test_read_failure_allows_one_direct_reload(self) -> None:
        route = "https://hentaiverse.org/?s=Bazaar&ss=lt"
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers(url=route)),
            xpath=AsyncMock(
                side_effect=[
                    [],
                    [SimpleNamespace(text="You currently have 1,600,000 GP")],
                    [SimpleNamespace(text="You hold 200 tickets")],
                ]
            ),
        )
        driver = _driver(page)

        await LotteryClient(driver).inspect(
            LotteryKind.WEAPON,
            context=MaintenanceNavigationContext.ORDINARY,
        )

        self.assertEqual(driver.get.await_count, 2)

    async def test_trusted_marker_after_navigation_error_is_typed(self) -> None:
        navigation_error = RuntimeError("navigation interrupted")
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=[_markers(), _markers(active=True)])
        )
        driver = _driver(page)
        driver.get.side_effect = navigation_error

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await LotteryClient(driver)._open_directly(LotteryKind.WEAPON)

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        self.assertIs(raised.exception.__cause__, navigation_error)

    async def test_wrong_path_with_marker_is_safety_error_not_blocker(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value=_markers(
                    url="https://hentaiverse.org/unexpected",
                    active=True,
                )
            )
        )
        driver = _driver(page)

        with self.assertRaisesRegex(LotteryPageError, "unexpected path") as raised:
            await LotteryClient(driver)._open_directly(LotteryKind.WEAPON)

        self.assertNotIsInstance(raised.exception, MaintenanceNavigationBlockedError)
        driver.get.assert_not_awaited()


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
                    operation = LotteryClient(driver)._open_directly(LotteryKind.WEAPON)
                else:
                    operation = MonsterLabClient(driver)._open_directly()

                with self.assertRaises(ZendriverOperationTimeout) as raised:
                    await operation

                self.assertIs(raised.exception, timeout)
                page.evaluate.assert_awaited_once()

    async def test_lottery_opens_requested_canonical_direct_route(
        self,
    ) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=la"
            if "nextFloor" in script and "battle_main" in script:
                return _markers(url="https://hentaiverse.org/?s=Bazaar&ss=la")
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(),
            xpath=AsyncMock(),
        )
        driver = _driver(page)

        await LotteryClient(driver)._navigate(LotteryKind.ARMOR)

        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=la")
        page.select.assert_not_awaited()
        page.xpath.assert_not_awaited()
        driver.wait.assert_not_awaited()

    async def test_lottery_initial_completion_may_leave_via_direct_url(self) -> None:
        marker_results = iter(
            [
                _markers(completion=True),
                _markers(url="https://hentaiverse.org/?s=Bazaar&ss=lt"),
            ]
        )

        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=lt"
            if "nextFloor" in script and "battle_main" in script:
                return next(marker_results)
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(),
            xpath=AsyncMock(),
        )
        driver = _driver(page)

        await LotteryClient(driver)._navigate(
            LotteryKind.WEAPON,
            context=MaintenanceNavigationContext.POST_BATTLE,
        )

        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=lt")
        self.assertEqual(driver.get.await_count, 1)
        page.select.assert_not_awaited()
        page.xpath.assert_not_awaited()
        driver.wait.assert_not_awaited()

    async def test_lottery_correct_route_reloads_once_after_read_error(self) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=lt"
            if "nextFloor" in script and "battle_main" in script:
                return _markers(url="https://hentaiverse.org/?s=Bazaar&ss=lt")
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(),
            xpath=AsyncMock(
                side_effect=[
                    [],
                    [SimpleNamespace(text="You currently have 1,600,000 GP")],
                    [SimpleNamespace(text="You hold 200 tickets")],
                ]
            ),
        )
        driver = _driver(page)

        with self.assertLogs("hvbrowser.lottery", level="WARNING") as captured:
            snapshot = await LotteryClient(driver).inspect(
                LotteryKind.WEAPON,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(
            snapshot,
            LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200),
        )
        self.assertEqual(driver.get.await_count, 2)
        self.assertTrue(
            all(
                call.args == ("https://hentaiverse.org/?s=Bazaar&ss=lt",)
                for call in driver.get.await_args_list
            )
        )
        self.assertIn("not readable after navigation", captured.output[0])

    async def test_lottery_second_read_error_is_not_retried(self) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=la"
            if "nextFloor" in script and "battle_main" in script:
                return _markers(url="https://hentaiverse.org/?s=Bazaar&ss=la")
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(),
            xpath=AsyncMock(side_effect=[[], []]),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(LotteryPageError, "GP balance is missing"):
            await LotteryClient(driver).inspect(
                LotteryKind.ARMOR,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(driver.get.await_count, 2)
        self.assertEqual(page.xpath.await_count, 2)

    async def test_lottery_direct_navigation_rejects_wrong_realm(self) -> None:
        marker_results = iter(
            [
                _markers(),
                _markers(url="https://hentaiverse.org/isekai/?s=Bazaar&ss=lt"),
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

        with self.assertRaisesRegex(LotteryPageError, "wrong realm"):
            await LotteryClient(driver)._navigate(LotteryKind.WEAPON)

        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=lt")
        self.assertEqual(driver.get.await_count, 1)

    async def test_lottery_noncompletion_battle_states_block_before_direct(
        self,
    ) -> None:
        cases = (
            (_markers(challenge=True), MaintenanceNavigationBlocker.CHALLENGE),
            (_markers(next_floor=True), MaintenanceNavigationBlocker.NEXT_FLOOR),
            (_markers(active=True), MaintenanceNavigationBlocker.ACTIVE),
        )
        for marker_payload, blocker in cases:
            with self.subTest(blocker=blocker):
                page = SimpleNamespace(
                    evaluate=AsyncMock(return_value=marker_payload),
                )
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await LotteryClient(driver)._navigate(LotteryKind.ARMOR)

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_not_awaited()

    async def test_lottery_battle_after_direct_navigation_stops_safely(
        self,
    ) -> None:
        marker_payloads = {
            MaintenanceNavigationBlocker.CHALLENGE: _markers(challenge=True),
            MaintenanceNavigationBlocker.COMPLETION: _markers(completion=True),
            MaintenanceNavigationBlocker.NEXT_FLOOR: _markers(next_floor=True),
            MaintenanceNavigationBlocker.ACTIVE: _markers(active=True),
        }
        for blocker, marker_payload in marker_payloads.items():
            with self.subTest(blocker=blocker):
                marker_results = iter([_markers(), marker_payload])

                async def evaluate(script: str) -> object:
                    if "nextFloor" in script and "battle_main" in script:
                        return next(marker_results)
                    raise AssertionError(f"Unexpected evaluate script: {script!r}")

                page = SimpleNamespace(evaluate=AsyncMock(side_effect=evaluate))
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await LotteryClient(driver)._open_directly(LotteryKind.ARMOR)

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_awaited_once_with(
                    "https://hentaiverse.org/?s=Bazaar&ss=la"
                )

    async def test_lottery_propagates_typed_battle_block(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=_markers(active=True)),
            select=AsyncMock(),
            xpath=AsyncMock(),
        )
        driver = _driver(page)

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await LotteryClient(driver).inspect(
                LotteryKind.WEAPON,
                context=MaintenanceNavigationContext.ORDINARY,
            )

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
            await MonsterLabClient(driver).inspect(
                context=MaintenanceNavigationContext.ORDINARY
            )

        self.assertIs(
            raised.exception.blocker,
            MaintenanceNavigationBlocker.CHALLENGE,
        )
        driver.gohomepage.assert_not_awaited()

    async def test_monster_lab_opens_canonical_direct_route(self) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=ml"
            if "nextFloor" in script and "battle_main" in script:
                return _markers(url="https://hentaiverse.org/?s=Bazaar&ss=ml")
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(),
            xpath=AsyncMock(),
        )
        driver = _driver(page)

        await MonsterLabClient(driver)._navigate()

        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=ml")
        page.select.assert_not_awaited()
        page.xpath.assert_not_awaited()
        driver.wait.assert_not_awaited()

    async def test_monster_lab_initial_completion_may_leave_via_direct_url(
        self,
    ) -> None:
        marker_results = iter(
            [
                _markers(completion=True),
                _markers(url="https://hentaiverse.org/?s=Bazaar&ss=ml"),
            ]
        )

        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=ml"
            if "nextFloor" in script and "battle_main" in script:
                return next(marker_results)
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(),
            xpath=AsyncMock(),
        )
        driver = _driver(page)

        await MonsterLabClient(driver)._navigate(
            context=MaintenanceNavigationContext.POST_BATTLE
        )

        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=ml")
        self.assertEqual(driver.get.await_count, 1)
        page.select.assert_not_awaited()
        page.xpath.assert_not_awaited()
        driver.wait.assert_not_awaited()

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
                return _markers(url="https://hentaiverse.org/?s=Bazaar&ss=ml")
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(),
            xpath=AsyncMock(side_effect=[[], []]),
        )
        driver = _driver(page)

        with self.assertLogs("hvbrowser.monster_lab", level="WARNING") as captured:
            snapshot = await MonsterLabClient(driver).inspect(
                context=MaintenanceNavigationContext.ORDINARY
            )

        self.assertEqual(snapshot, MonsterLabSnapshot(frozenset()))
        self.assertEqual(driver.get.await_count, 2)
        self.assertIn("not readable after navigation", captured.output[0])

    async def test_monster_lab_second_read_error_is_not_retried(self) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=ml"
            if script == "typeof do_feed_all === 'function'":
                return False
            if "nextFloor" in script and "battle_main" in script:
                return _markers(url="https://hentaiverse.org/?s=Bazaar&ss=ml")
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(),
            xpath=AsyncMock(),
        )
        driver = _driver(page)

        with self.assertRaisesRegex(MonsterLabPageError, "API is missing"):
            await MonsterLabClient(driver).inspect(
                context=MaintenanceNavigationContext.ORDINARY
            )

        self.assertEqual(driver.get.await_count, 2)

    async def test_monster_lab_caps_read_retry_at_two_direct_opens(
        self,
    ) -> None:
        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=ml"
            if script == "typeof do_feed_all === 'function'":
                return False
            if "nextFloor" in script and "battle_main" in script:
                return _markers(url="https://hentaiverse.org/?s=Bazaar&ss=ml")
            raise AssertionError(f"Unexpected evaluate script: {script!r}")

        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=evaluate),
            select=AsyncMock(return_value=SimpleNamespace(mouse_move=AsyncMock())),
            xpath=AsyncMock(return_value=[]),
        )
        driver = _driver(page)

        with self.assertLogs("hvbrowser.monster_lab", level="WARNING") as captured:
            with self.assertRaisesRegex(MonsterLabPageError, "API is missing"):
                await MonsterLabClient(driver).inspect(
                    context=MaintenanceNavigationContext.ORDINARY
                )

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
        self.assertEqual(len(captured.output), 1)
        self.assertIn("not readable after navigation", captured.output[0])

    async def test_monster_lab_direct_navigation_rejects_untrusted_destination(
        self,
    ) -> None:
        cases = (
            (
                "https://hentaiverse.org/isekai/?s=Bazaar&ss=ml",
                "wrong realm",
            ),
            (
                "https://example.test/?s=Bazaar&ss=ml",
                "untrusted",
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
                marker_results = iter([_markers(), _markers(url=current_url)])

                async def evaluate(script: str) -> object:
                    if "nextFloor" in script and "battle_main" in script:
                        return next(marker_results)
                    raise AssertionError(f"Unexpected evaluate script: {script!r}")

                page = SimpleNamespace(evaluate=AsyncMock(side_effect=evaluate))
                driver = _driver(page)

                with self.assertRaisesRegex(MonsterLabPageError, message):
                    await MonsterLabClient(driver)._open_directly()

                driver.get.assert_awaited_once_with(
                    "https://hentaiverse.org/?s=Bazaar&ss=ml"
                )

    async def test_monster_lab_noncompletion_battle_states_block_before_direct(
        self,
    ) -> None:
        cases = (
            ("challenge", MaintenanceNavigationBlocker.CHALLENGE),
            ("next_floor", MaintenanceNavigationBlocker.NEXT_FLOOR),
            ("active", MaintenanceNavigationBlocker.ACTIVE),
        )
        for marker_name, blocker in cases:
            with self.subTest(blocker=blocker):
                page = SimpleNamespace(
                    evaluate=AsyncMock(return_value=_markers(**{marker_name: True})),
                )
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await MonsterLabClient(driver)._navigate()

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_not_awaited()

    async def test_monster_lab_battle_after_direct_navigation_stops_safely(
        self,
    ) -> None:
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                marker_results = iter(
                    [
                        _markers(),
                        _markers(
                            challenge=blocker is MaintenanceNavigationBlocker.CHALLENGE,
                            completion=(
                                blocker is MaintenanceNavigationBlocker.COMPLETION
                            ),
                            next_floor=(
                                blocker is MaintenanceNavigationBlocker.NEXT_FLOOR
                            ),
                            active=blocker is MaintenanceNavigationBlocker.ACTIVE,
                        ),
                    ]
                )

                async def evaluate(script: str) -> object:
                    if "nextFloor" in script and "battle_main" in script:
                        return next(marker_results)
                    raise AssertionError(f"Unexpected evaluate script: {script!r}")

                page = SimpleNamespace(evaluate=AsyncMock(side_effect=evaluate))
                driver = _driver(page)

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await MonsterLabClient(driver)._open_directly()

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_awaited_once_with(
                    "https://hentaiverse.org/?s=Bazaar&ss=ml"
                )

    async def test_lottery_does_not_depend_on_bazaar_menu_selection(self) -> None:
        selection_error = RuntimeError("disconnected")

        async def evaluate(script: str) -> object:
            if script == "window.location.href":
                return "https://hentaiverse.org/?s=Bazaar&ss=la"
            if "nextFloor" in script and "battle_main" in script:
                return _markers(url="https://hentaiverse.org/?s=Bazaar&ss=la")
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

        snapshot = await LotteryClient(driver).inspect(
            LotteryKind.ARMOR,
            context=MaintenanceNavigationContext.ORDINARY,
        )

        self.assertEqual(snapshot.tickets, 100)
        driver.get.assert_awaited_once_with("https://hentaiverse.org/?s=Bazaar&ss=la")
        page.select.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
