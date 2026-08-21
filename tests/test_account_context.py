import asyncio
import unittest
from collections.abc import Awaitable, Callable, Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

from hvbrowser.account_context import (
    AccountContextStartupStopped,
    AccountContextState,
    AccountContextStateError,
    HentaiVerseAccountContext,
    RealmBindingViolationError,
    RealmBoundHVDriver,
    RealmNotEnabledError,
    RealmRuntime,
    RealmTabBindingError,
)
from hvbrowser.realm import Realm
from hvbrowser.runtime import ZendriverOperationTimeout, log_context
from hvbrowser.urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL


@dataclass
class _FakeTab:
    target_id: str
    url: str = "about:blank"
    navigations: list[str] = field(default_factory=list)
    opened: bool = False

    async def evaluate(self, script: str) -> str:
        if script != "window.location.href":
            raise AssertionError(f"unexpected script: {script}")
        return self.url

    async def get(self, url: str) -> None:
        self.url = url
        self.navigations.append(url)


@dataclass
class _FakeBrowser:
    current_tab: _FakeTab


class _RecordingLogContext:
    """Test double with the nesting semantics of hbrowser.log_context."""

    def __init__(self) -> None:
        self._active: ContextVar[dict[str, str]] = ContextVar(
            "test_log_context",
            default={},
        )
        self.entered: list[dict[str, str]] = []

    @property
    def current(self) -> dict[str, str]:
        return dict(self._active.get())

    @contextmanager
    def __call__(
        self,
        *,
        account: str | None = None,
        realm: str | None = None,
        tab_role: str | None = None,
        activity: str | None = None,
        scope: str | None = None,
    ) -> Iterator[None]:
        supplied = {
            "account": account,
            "realm": realm,
            "tab_role": tab_role,
            "activity": activity,
            "scope": scope,
        }
        merged = self.current
        merged.update(
            (field_name, value)
            for field_name, value in supplied.items()
            if value is not None
        )
        self.entered.append(merged)
        token = self._active.set(merged)
        try:
            yield
        finally:
            self._active.reset(token)


class AccountContextTests(unittest.IsolatedAsyncioTestCase):
    def _context(
        self,
        *,
        authenticator: Callable[[RealmBoundHVDriver], Awaitable[None]] | None = None,
        enabled_realms: Collection[Realm] = tuple(Realm),
        account_label: str | None = None,
        on_open_tab: Callable[[], None] | None = None,
        current_url_reader: Callable[[_FakeTab], Awaitable[object]] | None = None,
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
            isekai_tab.opened = True
            if on_open_tab is not None:
                on_open_tab()
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
            current_url_reader=current_url_reader,
            enabled_realms=enabled_realms,
            account_label=account_label,
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

    async def test_persistent_only_does_not_open_an_isekai_tab(self) -> None:
        (
            context,
            _,
            persistent_tab,
            isekai_tab,
            browser_closer,
            authentication_targets,
        ) = self._context(enabled_realms=(Realm.PERSISTENT,))

        async with context:
            self.assertEqual(context.enabled_realms, frozenset({Realm.PERSISTENT}))
            self.assertEqual(authentication_targets, ["persistent-target"])
            self.assertIs(context.persistent.driver.page, persistent_tab)
            self.assertFalse(isekai_tab.opened)
            self.assertEqual(len(context.owner.tabs), 1)
            with self.assertRaisesRegex(RealmNotEnabledError, "isekai"):
                _ = context.isekai

        browser_closer.assert_awaited_once()

    async def test_isekai_only_uses_a_private_persistent_login_bootstrap(
        self,
    ) -> None:
        (
            context,
            _,
            persistent_tab,
            isekai_tab,
            browser_closer,
            authentication_targets,
        ) = self._context(enabled_realms=(Realm.ISEKAI,))

        async with context:
            self.assertEqual(context.enabled_realms, frozenset({Realm.ISEKAI}))
            self.assertEqual(authentication_targets, ["persistent-target"])
            self.assertEqual(persistent_tab.url, HENTAIVERSE_ROOT_URL)
            self.assertTrue(isekai_tab.opened)
            self.assertIs(context.isekai.driver.page, isekai_tab)
            self.assertEqual(isekai_tab.url, HENTAIVERSE_ISEKAI_ROOT_URL)
            self.assertEqual(len(context.owner.tabs), 2)
            with self.assertRaisesRegex(RealmNotEnabledError, "persistent"):
                context.runtime(Realm.PERSISTENT)

        browser_closer.assert_awaited_once()

    def test_enabled_realms_must_be_non_empty_realm_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one realm"):
            self._context(enabled_realms=())
        with self.assertRaisesRegex(TypeError, "Realm values"):
            self._context(enabled_realms=("persistent",))  # type: ignore[arg-type]

    def test_account_label_is_optional_and_normalized(self) -> None:
        context, *_ = self._context(account_label="  main  ")

        self.assertEqual(context.account_label, "main")
        self.assertIsNone(self._context()[0].account_label)
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            self._context(account_label="  ")
        with self.assertRaisesRegex(TypeError, "string or None"):
            self._context(account_label=object())  # type: ignore[arg-type]

    async def test_runtime_account_label_is_validated_and_normalized(self) -> None:
        context, *_ = self._context()
        await context.start()
        self.addAsyncCleanup(context.close)
        persistent = context.persistent

        async def read_url(tab: _FakeTab) -> object:
            return await tab.evaluate("window.location.href")

        runtime = RealmRuntime(
            realm=Realm.PERSISTENT,
            transport=persistent.transport,
            driver=persistent.driver,
            current_url_reader=read_url,
            account_label="  alt  ",
        )
        self.assertEqual(runtime.account_label, "alt")

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            RealmRuntime(
                realm=Realm.PERSISTENT,
                transport=persistent.transport,
                driver=persistent.driver,
                current_url_reader=read_url,
                account_label="  ",
            )
        with self.assertRaisesRegex(TypeError, "string or None"):
            RealmRuntime(
                realm=Realm.PERSISTENT,
                transport=persistent.transport,
                driver=persistent.driver,
                current_url_reader=read_url,
                account_label=object(),  # type: ignore[arg-type]
            )

    async def test_startup_sets_account_login_and_realm_establishment_contexts(
        self,
    ) -> None:
        context, *_ = self._context(account_label="main")
        recorder = _RecordingLogContext()

        with patch("hvbrowser.account_context.log_context", new=recorder):
            await context.start()
        self.addAsyncCleanup(context.close)

        self.assertEqual(context.persistent.account_label, "main")
        self.assertEqual(context.isekai.account_label, "main")
        self.assertIn(
            {"account": "main", "scope": "Browser"},
            recorder.entered,
        )
        self.assertIn(
            {
                "account": "main",
                "tab_role": "persistent",
                "activity": "Login",
                "scope": "Account · Login",
            },
            recorder.entered,
        )
        self.assertFalse(
            any(
                context.get("activity") == "Login" and "realm" in context
                for context in recorder.entered
            )
        )
        self.assertIn(
            {
                "account": "main",
                "realm": "isekai",
                "tab_role": "isekai",
                "activity": "Establish",
            },
            recorder.entered,
        )
        self.assertEqual(recorder.current, {})

    async def test_runtime_context_selects_realm_and_restores_ambient_values(
        self,
    ) -> None:
        context, *_ = self._context(account_label="main")
        await context.start()
        self.addAsyncCleanup(context.close)
        recorder = _RecordingLogContext()

        with patch("hvbrowser.account_context.log_context", new=recorder):
            with recorder(activity="Check-in"):
                persistent_context = await context.persistent.execute(
                    lambda _: _return(recorder.current)
                )
                self.assertEqual(recorder.current, {"activity": "Check-in"})

            with recorder(activity="Battle"):
                isekai_context = await context.isekai.execute(
                    lambda _: _return(recorder.current)
                )
                self.assertEqual(recorder.current, {"activity": "Battle"})

        self.assertEqual(
            persistent_context,
            {
                "account": "main",
                "realm": "persistent",
                "tab_role": "persistent",
                "activity": "Check-in",
            },
        )
        self.assertEqual(
            isekai_context,
            {
                "account": "main",
                "realm": "isekai",
                "tab_role": "isekai",
                "activity": "Battle",
            },
        )
        self.assertEqual(recorder.current, {})

    async def test_navigation_overrides_activity_without_leaking_context(self) -> None:
        context, *_ = self._context(account_label="main")
        await context.start()
        self.addAsyncCleanup(context.close)
        recorder = _RecordingLogContext()

        with patch("hvbrowser.account_context.log_context", new=recorder):
            with recorder(activity="Battle"):
                await context.isekai.navigate(HENTAIVERSE_ISEKAI_ROOT_URL)
                self.assertEqual(recorder.current, {"activity": "Battle"})

        self.assertIn(
            {
                "account": "main",
                "realm": "isekai",
                "tab_role": "isekai",
                "activity": "Navigation",
            },
            recorder.entered,
        )
        self.assertEqual(recorder.current, {})

    def test_runtime_reexports_log_context(self) -> None:
        self.assertTrue(callable(log_context))

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

    async def test_injected_current_url_reader_hang_is_bounded_inside_transport(
        self,
    ) -> None:
        should_hang = False
        reader_started = asyncio.Event()
        release_reader = asyncio.Event()

        async def read_url(tab: _FakeTab) -> object:
            if not should_hang:
                return tab.url
            reader_started.set()
            await release_reader.wait()
            return tab.url

        context, *_ = self._context(
            enabled_realms=(Realm.PERSISTENT,),
            current_url_reader=read_url,
        )
        await context.start()
        should_hang = True

        with patch(
            "hvbrowser.account_context._CURRENT_URL_TIMEOUT_SECONDS",
            0.01,
        ):
            reading = asyncio.create_task(context.persistent.current_realm())
            await reader_started.wait()

            contender_entered = asyncio.Event()

            async def contender(_: _FakeTab) -> None:
                contender_entered.set()

            contender_task = asyncio.create_task(
                context.persistent.transport.execute(contender)
            )
            await asyncio.sleep(0)
            self.assertFalse(contender_entered.is_set())
            contender_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await contender_task

            with self.assertRaises(ZendriverOperationTimeout) as raised:
                await reading

        self.assertEqual(raised.exception.timeout_seconds, 0.01)
        release_reader.set()
        await asyncio.sleep(0)
        await context.close()

    async def test_runtime_restores_and_reports_post_operation_drift(self) -> None:
        context, _, persistent_tab, _, _, _ = self._context()
        await context.start()
        self.addAsyncCleanup(context.close)

        async def cross_realm(driver: RealmBoundHVDriver) -> None:
            await driver.page.get(HENTAIVERSE_ISEKAI_ROOT_URL)

        with self.assertRaises(RealmBindingViolationError):
            await context.persistent.execute(cross_realm)

        self.assertEqual(persistent_tab.url, HENTAIVERSE_ROOT_URL)

    async def test_realm_restore_timeout_remains_a_generation_error(self) -> None:
        context, *_ = self._context(enabled_realms=(Realm.PERSISTENT,))
        await context.start()
        self.addAsyncCleanup(context.close)
        runtime = context.persistent
        timeout = ZendriverOperationTimeout(timeout_seconds=0.01)
        runtime._assert_current_realm = AsyncMock(  # type: ignore[method-assign]
            side_effect=RealmTabBindingError("drifted")
        )
        runtime._navigate_home = AsyncMock(  # type: ignore[method-assign]
            side_effect=timeout
        )

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await runtime._restore_after_operation()

        self.assertIs(raised.exception, timeout)
        self.assertTrue(
            any("restoring the bound realm" in note for note in timeout.__notes__)
        )

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

    async def test_preexisting_stop_never_starts_the_browser(self) -> None:
        context, _, persistent_tab, isekai_tab, browser_closer, authentications = (
            self._context()
        )

        with self.assertRaises(AccountContextStartupStopped):
            await context.start(stop_requested=lambda: True)

        self.assertEqual(context.state, AccountContextState.CLOSED)
        self.assertEqual(authentications, [])
        self.assertEqual(persistent_tab.navigations, [])
        self.assertFalse(isekai_tab.opened)
        browser_closer.assert_not_awaited()

    async def test_stop_after_login_does_not_open_the_next_realm(self) -> None:
        stop = False
        authentications: list[str] = []

        async def authenticate(driver: RealmBoundHVDriver) -> None:
            nonlocal stop
            authentications.append(driver.tab_handle.target_id)
            await driver.page.get(HENTAIVERSE_ROOT_URL)
            stop = True

        context, _, persistent_tab, isekai_tab, browser_closer, _ = self._context(
            authenticator=authenticate
        )

        with self.assertRaises(AccountContextStartupStopped):
            await context.start(stop_requested=lambda: stop)

        self.assertEqual(context.state, AccountContextState.CLOSED)
        self.assertEqual(authentications, ["persistent-target"])
        self.assertEqual(persistent_tab.url, HENTAIVERSE_ROOT_URL)
        self.assertFalse(isekai_tab.opened)
        browser_closer.assert_awaited_once()

    async def test_stop_after_tab_open_does_not_bind_or_establish_that_realm(
        self,
    ) -> None:
        stop = False

        def request_stop() -> None:
            nonlocal stop
            stop = True

        context, _, _, isekai_tab, browser_closer, authentications = self._context(
            on_open_tab=request_stop
        )

        with self.assertRaises(AccountContextStartupStopped):
            await context.start(stop_requested=lambda: stop)

        self.assertEqual(context.state, AccountContextState.CLOSED)
        self.assertEqual(authentications, ["persistent-target"])
        self.assertTrue(isekai_tab.opened)
        self.assertEqual(isekai_tab.navigations, [])
        browser_closer.assert_awaited_once()

    async def test_startup_cleanup_survives_repeated_cancellation(self) -> None:
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def fail_authentication(_: RealmBoundHVDriver) -> None:
            raise PermissionError("login failed")

        async def close_browser(_: _FakeBrowser) -> None:
            close_started.set()
            await allow_close.wait()

        context, _, _, _, browser_closer, _ = self._context(
            authenticator=fail_authentication
        )
        browser_closer.side_effect = close_browser

        starting = asyncio.create_task(context.start())
        await close_started.wait()
        starting.cancel()
        await asyncio.sleep(0)
        starting.cancel()
        await asyncio.sleep(0)

        self.assertFalse(starting.done())
        self.assertEqual(context.state, AccountContextState.CLOSING)

        allow_close.set()
        with self.assertRaises(asyncio.CancelledError):
            await starting

        self.assertEqual(context.state, AccountContextState.CLOSED)
        browser_closer.assert_awaited_once()

    async def test_close_survives_repeated_cancellation(self) -> None:
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def close_browser(_: _FakeBrowser) -> None:
            close_started.set()
            await allow_close.wait()

        context, _, _, _, browser_closer, _ = self._context()
        browser_closer.side_effect = close_browser
        await context.start()

        closing = asyncio.create_task(context.close())
        await close_started.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)

        self.assertFalse(closing.done())
        self.assertEqual(context.state, AccountContextState.CLOSING)

        allow_close.set()
        with self.assertRaises(asyncio.CancelledError):
            await closing

        self.assertEqual(context.state, AccountContextState.CLOSED)
        browser_closer.assert_awaited_once()

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

    async def test_close_can_retry_after_transient_owner_cleanup_failure(self) -> None:
        context, _, _, _, browser_closer, _ = self._context()
        browser_closer.side_effect = [ConnectionError("transient close failure"), None]
        await context.start()

        with self.assertRaisesRegex(ConnectionError, "transient close failure"):
            await context.close()

        self.assertEqual(context.state, AccountContextState.CLOSING)
        with self.assertRaises(AccountContextStateError):
            _ = context.persistent

        await context.close()

        self.assertEqual(context.state, AccountContextState.CLOSED)
        self.assertEqual(browser_closer.await_count, 2)

    async def test_startup_cleanup_can_retry_after_transient_failure(self) -> None:
        async def fail_authentication(_: RealmBoundHVDriver) -> None:
            raise PermissionError("login failed")

        context, _, _, _, browser_closer, _ = self._context(
            authenticator=fail_authentication
        )
        browser_closer.side_effect = [ConnectionError("transient close failure"), None]

        with self.assertRaisesRegex(PermissionError, "login failed") as raised:
            await context.start()

        self.assertEqual(context.state, AccountContextState.CLOSING)
        self.assertTrue(
            any("cleanup also failed" in note for note in raised.exception.__notes__)
        )

        await context.close()

        self.assertEqual(context.state, AccountContextState.CLOSED)
        self.assertEqual(browser_closer.await_count, 2)


async def _return[ResultT](value: ResultT) -> ResultT:
    return value


if __name__ == "__main__":
    unittest.main()
