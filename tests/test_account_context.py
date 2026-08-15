import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

from hvbrowser.account_context import (
    AccountContextState,
    AccountContextStateError,
    HentaiVerseAccountContext,
    RealmBindingViolationError,
    RealmBoundHVDriver,
    RealmTabBindingError,
)
from hvbrowser.realm import Realm
from hvbrowser.urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL


@dataclass(slots=True)
class _FakeTab:
    target_id: str
    url: str = "about:blank"
    navigations: list[str] = field(default_factory=list)

    async def evaluate(self, script: str) -> str:
        if script != "window.location.href":
            raise AssertionError(f"unexpected script: {script}")
        return self.url

    async def get(self, url: str) -> None:
        self.url = url
        self.navigations.append(url)


@dataclass(slots=True)
class _FakeBrowser:
    current_tab: _FakeTab


class AccountContextTests(unittest.IsolatedAsyncioTestCase):
    def _context(
        self,
        *,
        authenticator: Callable[[RealmBoundHVDriver], Awaitable[None]] | None = None,
    ) -> tuple[
        HentaiVerseAccountContext[_FakeBrowser, _FakeTab],
        _FakeBrowser,
        _FakeTab,
        _FakeTab,
        AsyncMock,
        list[str],
    ]:
        persistent_tab = _FakeTab("persistent-target")
        isekai_tab = _FakeTab("isekai-target")
        browser = _FakeBrowser(persistent_tab)
        browser_closer = AsyncMock()
        authentication_targets: list[str] = []

        async def create_browser() -> tuple[_FakeBrowser, _FakeTab]:
            return browser, persistent_tab

        async def open_tab(_: _FakeBrowser) -> _FakeTab:
            return isekai_tab

        async def navigate(tab: _FakeTab, url: str) -> None:
            await tab.get(url)

        async def authenticate(driver: RealmBoundHVDriver) -> None:
            authentication_targets.append(driver.tab_handle.target_id)
            await driver.page.get(HENTAIVERSE_ROOT_URL)

        context = HentaiVerseAccountContext(
            owner_id="account-test",
            authenticator=authenticator or authenticate,
            browser_factory=create_browser,
            browser_closer=browser_closer,
            tab_factory=open_tab,
            tab_navigator=navigate,
            target_id_getter=lambda tab: tab.target_id,
        )
        return (
            context,
            browser,
            persistent_tab,
            isekai_tab,
            browser_closer,
            authentication_targets,
        )

    async def test_one_login_establishes_two_immutable_realm_targets(self) -> None:
        (
            context,
            _,
            persistent_tab,
            isekai_tab,
            browser_closer,
            authentication_targets,
        ) = self._context()

        async with context:
            self.assertEqual(context.state, AccountContextState.OPEN)
            self.assertEqual(authentication_targets, ["persistent-target"])
            self.assertEqual(context.persistent.realm, Realm.PERSISTENT)
            self.assertEqual(context.isekai.realm, Realm.ISEKAI)
            self.assertEqual(
                context.persistent.tab_handle.target_id,
                "persistent-target",
            )
            self.assertEqual(context.isekai.tab_handle.target_id, "isekai-target")
            self.assertEqual(
                context.persistent.tab_handle.owner_id,
                context.isekai.tab_handle.owner_id,
            )
            self.assertIs(context.persistent.driver.page, persistent_tab)
            self.assertIs(context.isekai.driver.page, isekai_tab)
            self.assertFalse(context.persistent.driver.owns_browser)
            self.assertFalse(context.isekai.driver.owns_browser)
            self.assertEqual(persistent_tab.url, HENTAIVERSE_ROOT_URL)
            self.assertEqual(isekai_tab.url, HENTAIVERSE_ISEKAI_ROOT_URL)

        self.assertEqual(context.state, AccountContextState.CLOSED)
        browser_closer.assert_awaited_once()

    async def test_runtime_does_not_follow_browser_current_tab(self) -> None:
        context, browser, persistent_tab, isekai_tab, _, _ = self._context()
        await context.start()
        self.addAsyncCleanup(context.close)
        browser.current_tab = isekai_tab

        persistent_seen = await context.persistent.execute(
            lambda driver: _return(driver.page.target_id)
        )
        browser.current_tab = persistent_tab
        isekai_seen = await context.isekai.execute(
            lambda driver: _return(driver.page.target_id)
        )

        self.assertEqual(persistent_seen, "persistent-target")
        self.assertEqual(isekai_seen, "isekai-target")

    async def test_runtime_repairs_drift_before_running_operation(self) -> None:
        context, _, persistent_tab, _, _, _ = self._context()
        await context.start()
        self.addAsyncCleanup(context.close)
        persistent_tab.url = HENTAIVERSE_ISEKAI_ROOT_URL

        seen_url = await context.persistent.execute(
            lambda driver: driver.page.evaluate("window.location.href")
        )

        self.assertEqual(seen_url, HENTAIVERSE_ROOT_URL)
        self.assertEqual(persistent_tab.url, HENTAIVERSE_ROOT_URL)
        self.assertEqual(persistent_tab.navigations[-1], HENTAIVERSE_ROOT_URL)

    async def test_runtime_restores_and_reports_post_operation_drift(self) -> None:
        context, _, persistent_tab, _, _, _ = self._context()
        await context.start()
        self.addAsyncCleanup(context.close)

        async def cross_realm(driver: RealmBoundHVDriver) -> None:
            await driver.page.get(HENTAIVERSE_ISEKAI_ROOT_URL)

        with self.assertRaises(RealmBindingViolationError):
            await context.persistent.execute(cross_realm)

        self.assertEqual(persistent_tab.url, HENTAIVERSE_ROOT_URL)

    async def test_failed_operation_still_restores_bound_realm(self) -> None:
        context, _, _, isekai_tab, _, _ = self._context()
        await context.start()
        self.addAsyncCleanup(context.close)

        async def fail_after_drift(driver: RealmBoundHVDriver) -> None:
            await driver.page.get("https://example.test/outside")
            raise LookupError("operation failed")

        with self.assertRaisesRegex(LookupError, "operation failed"):
            await context.isekai.execute(fail_after_drift)

        self.assertEqual(isekai_tab.url, HENTAIVERSE_ISEKAI_ROOT_URL)

    async def test_navigation_rejects_cross_realm_before_touching_tab(self) -> None:
        context, _, _, isekai_tab, _, _ = self._context()
        await context.start()
        self.addAsyncCleanup(context.close)
        previous_navigations = list(isekai_tab.navigations)

        with self.assertRaisesRegex(RealmTabBindingError, "cross-realm"):
            await context.isekai.navigate(HENTAIVERSE_ROOT_URL)

        self.assertEqual(isekai_tab.navigations, previous_navigations)

    async def test_bound_driver_rejects_page_replacement(self) -> None:
        context, _, _, _, _, _ = self._context()
        await context.start()
        self.addAsyncCleanup(context.close)

        with self.assertRaisesRegex(RealmTabBindingError, "cannot replace"):
            context.persistent.driver.page = _FakeTab("replacement")

    async def test_authentication_failure_closes_the_owned_browser(self) -> None:
        async def fail_authentication(_: RealmBoundHVDriver) -> None:
            raise PermissionError("login failed")

        context, _, _, _, browser_closer, _ = self._context(
            authenticator=fail_authentication
        )

        with self.assertRaisesRegex(PermissionError, "login failed"):
            await context.start()

        self.assertEqual(context.state, AccountContextState.CLOSED)
        browser_closer.assert_awaited_once()
        with self.assertRaises(AccountContextStateError):
            await context.start()

    async def test_close_is_idempotent_and_runtimes_require_open_context(self) -> None:
        context, _, _, _, browser_closer, _ = self._context()

        with self.assertRaises(AccountContextStateError):
            _ = context.persistent

        await context.start()
        await context.close()
        await context.close()

        browser_closer.assert_awaited_once()
        with self.assertRaises(AccountContextStateError):
            _ = context.isekai


async def _return[ResultT](value: ResultT) -> ResultT:
    return value


if __name__ == "__main__":
    unittest.main()
