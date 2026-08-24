import asyncio
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hbrowser import LogPersistenceError

from hvbrowser import (
    HENTAIVERSE_ISEKAI_ROOT_URL,
    HENTAIVERSE_ROOT_URL,
    HentaiVerseSession,
    HVDriver,
    Realm,
    RealmDetectionError,
    RealmNavigator,
    realm_from_url,
)
from hvbrowser.runtime import LogPersistenceError as RuntimeLogPersistenceError
from hvbrowser.runtime import ZendriverOperationTimeout


class RealmParsingTests(unittest.TestCase):
    def test_trusted_hentaiverse_urls_map_to_typed_realms(self) -> None:
        cases = (
            ("https://hentaiverse.org/", Realm.PERSISTENT),
            ("https://hentaiverse.org:443/", Realm.PERSISTENT),
            ("https://hentaiverse.org/?s=Battle&ss=ba", Realm.PERSISTENT),
            ("https://hentaiverse.org/isekai", Realm.ISEKAI),
            ("https://hentaiverse.org/isekai/?s=Battle&ss=ba", Realm.ISEKAI),
        )

        for url, expected in cases:
            with self.subTest(url=url):
                self.assertIs(realm_from_url(url), expected)

    def test_lookalike_paths_remain_persistent(self) -> None:
        for url in (
            "https://hentaiverse.org/?next=/isekai/",
            "https://hentaiverse.org/#isekai",
            "https://hentaiverse.org/isekaiish/",
            "https://hentaiverse.org/foo/isekai/",
        ):
            with self.subTest(url=url):
                self.assertIs(realm_from_url(url), Realm.PERSISTENT)

    def test_untrusted_or_unreadable_locations_fail_closed(self) -> None:
        for url in (
            "http://hentaiverse.org/isekai/",
            "https://hentaiverse.org:0/",
            "https://hentaiverse.org:444/isekai/",
            "https://example.test/isekai/",
            None,
        ):
            with self.subTest(url=url):
                with self.assertRaises(RealmDetectionError):
                    realm_from_url(url)


class RealmNavigatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_reads_the_page_location(self) -> None:
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value="https://hentaiverse.org/isekai/")
        )
        navigator = RealmNavigator(
            SimpleNamespace(page=page, get=AsyncMock(), gohomepage=AsyncMock())
        )

        self.assertIs(await navigator.current(), Realm.ISEKAI)
        page.evaluate.assert_awaited_once_with("window.location.href")

    async def test_current_location_hang_raises_typed_watchdog_timeout(self) -> None:
        release = asyncio.Event()

        async def hang(_: str) -> str:
            await release.wait()
            return HENTAIVERSE_ROOT_URL

        page = SimpleNamespace(evaluate=AsyncMock(side_effect=hang))
        navigator = RealmNavigator(
            SimpleNamespace(page=page, get=AsyncMock(), gohomepage=AsyncMock())
        )

        with (
            patch("hvbrowser.runtime.PROTOCOL_COMMAND_TIMEOUT_SECONDS", 0.01),
            self.assertRaises(ZendriverOperationTimeout) as raised,
        ):
            await navigator.current()

        self.assertEqual(raised.exception.timeout_seconds, 0.01)
        page.evaluate.assert_awaited_once_with("window.location.href")
        release.set()
        await asyncio.sleep(0)

    async def test_go_home_routes_each_realm_through_the_browser(self) -> None:
        browser = SimpleNamespace(
            page=SimpleNamespace(),
            get=AsyncMock(),
            gohomepage=AsyncMock(),
        )
        navigator = RealmNavigator(browser)

        await navigator.go_home(Realm.PERSISTENT, force=True)
        await navigator.go_home(Realm.ISEKAI, force=True)

        browser.gohomepage.assert_awaited_once_with(force=True)
        browser.get.assert_awaited_once_with(HENTAIVERSE_ISEKAI_ROOT_URL)


class HentaiVerseSessionTests(unittest.IsolatedAsyncioTestCase):
    def _browser(self, events: list[str]) -> SimpleNamespace:
        async def initialize() -> None:
            events.append("initialize")

        async def login() -> None:
            events.append("login")

        async def gohomepage(force: bool = False) -> None:
            del force
            events.append("home")

        async def close(*_args: object) -> None:
            events.append("close")

        return SimpleNamespace(
            page=SimpleNamespace(),
            headless=True,
            _init_browser=AsyncMock(side_effect=initialize),
            login=AsyncMock(side_effect=login),
            gohomepage=AsyncMock(side_effect=gohomepage),
            get=AsyncMock(),
            __aexit__=AsyncMock(side_effect=close),
        )

    def test_component_graph_shares_one_raw_browser(self) -> None:
        browser = self._browser([])
        session = HentaiVerseSession(browser=browser)  # type: ignore[arg-type]

        self.assertIs(session.browser, browser)
        self.assertIs(session.realm._driver, browser)
        self.assertFalse(hasattr(session, "maintenance_navigation"))
        self.assertIs(session.player.driver, browser)
        self.assertIs(session.equipment.driver, browser)
        self.assertIs(session.market._driver, browser)
        self.assertIs(session.lottery.driver, browser)
        self.assertIs(session.monster_lab.driver, browser)

    def test_injected_browser_rejects_browser_options(self) -> None:
        browser = self._browser([])
        with self.assertRaisesRegex(TypeError, "injected browser"):
            HentaiVerseSession(headless=False, browser=browser)  # type: ignore[arg-type]

    async def test_start_orders_browser_hook_login_and_home(self) -> None:
        events: list[str] = []
        browser = self._browser(events)
        session = HentaiVerseSession(browser=browser)  # type: ignore[arg-type]

        async def browser_ready() -> None:
            events.append("hook")

        entered = await session.start(on_browser_ready=browser_ready)

        self.assertIs(entered, session)
        self.assertEqual(events, ["initialize", "hook", "login", "home"])
        browser.gohomepage.assert_awaited_once_with(force=False)

        await session.__aexit__(None, None, None)
        self.assertEqual(events[-1], "close")

    async def test_start_failure_closes_the_raw_browser(self) -> None:
        events: list[str] = []
        browser = self._browser(events)
        browser.login.side_effect = RuntimeError("login failed")
        session = HentaiVerseSession(browser=browser)  # type: ignore[arg-type]

        with self.assertRaisesRegex(RuntimeError, "login failed"):
            await session.start()

        browser.__aexit__.assert_awaited_once()


class RawDriverBoundaryTests(unittest.TestCase):
    def test_runtime_reexports_log_persistence_boundary(self) -> None:
        self.assertIs(RuntimeLogPersistenceError, LogPersistenceError)

    def test_driver_inherits_public_page_diagnostic_capture(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(HVDriver.save_page_diagnostic))

    def test_driver_only_installs_hentaiverse_transport_urls(self) -> None:
        driver = HVDriver(headless=True)

        self.assertEqual(driver.url["HentaiVerse"], HENTAIVERSE_ROOT_URL)
        self.assertEqual(
            driver.url["HentaiVerse isekai"],
            HENTAIVERSE_ISEKAI_ROOT_URL,
        )
        for domain_operation in (
            "get_level",
            "get_stamina",
            "inspect_market",
            "loetterycheck",
            "marketcheck",
            "monstercheck",
            "recoverstamina",
            "repairequipment",
        ):
            self.assertFalse(hasattr(driver, domain_operation))


class BudgetedNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_translates_remaining_budget_to_hbrowser_deadline(self) -> None:
        driver = HVDriver(headless=True)
        driver.myget = AsyncMock()

        await driver.navigate_with_budget(
            "https://hentaiverse.org/?s=Battle&ss=ar",
            budget_seconds=6.0,
        )

        driver.myget.assert_awaited_once()
        call = driver.myget.await_args
        self.assertEqual(
            call.args,
            ("https://hentaiverse.org/?s=Battle&ss=ar",),
        )
        deadline = call.kwargs["deadline"]
        self.assertTrue(hasattr(deadline, "bounded"))
        self.assertTrue(hasattr(deadline, "expired"))
        self.assertGreater(deadline.remaining(), 0)
        self.assertLessEqual(deadline.remaining(), 6.0)

    async def test_expired_budget_never_dispatches_navigation(self) -> None:
        driver = HVDriver(headless=True)
        driver.myget = AsyncMock()

        with self.assertRaisesRegex(TimeoutError, "expired before dispatch"):
            await driver.navigate_with_budget(
                "https://hentaiverse.org/?s=Battle&ss=ar",
                budget_seconds=0,
            )

        driver.myget.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
