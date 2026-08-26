"""One authenticated browser context with realm-bound HentaiVerse runtimes.

The account context owns the browser lifecycle.  Each enabled realm receives
one immutable browser target and one driver that can never be switched to the
other target.  Higher layers retain campaign and scheduling policy; this module
only establishes and enforces the browser/realm ownership boundary.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Collection
from enum import StrEnum
from functools import partial
from typing import Any, Self, cast

from hbrowser.gallery.browser import (
    BrowserCloser,
    BrowserFactory,
    BrowserOwner,
    BrowserOwnershipError,
    ProcessOwnershipError,
    TabFactory,
    TabHandle,
    TabNavigator,
    TabTransport,
    TargetIdGetter,
    create_browser,
    stop_browser,
)

from .hv import HVDriver
from .realm import Realm, RealmDetectionError, realm_from_url
from .runtime import (
    PROTOCOL_COMMAND_TIMEOUT_SECONDS,
    BrowserOperationDeadline,
    is_browser_generation_error,
    log_context,
    wait_for_zendriver,
)
from .urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL

type AccountAuthenticator = Callable[[RealmBoundHVDriver], Awaitable[None]]
type BoundDriverFactory[TabT] = Callable[
    [Realm, TabTransport[TabT]], RealmBoundHVDriver
]
type CurrentUrlReader[TabT] = Callable[[TabT], Awaitable[object]]

_ACCOUNT_CLOSE_TIMEOUT_SECONDS = 20.0
_ACCOUNT_LOGIN_LOG_SCOPE = "Account · Login"


def _never_stop() -> bool:
    return False


class AccountContextError(RuntimeError):
    """Base error for account-context lifecycle and binding failures."""


class AccountContextStateError(AccountContextError):
    """An account-context operation is invalid in the current state."""


class AccountContextStartupStopped(AccountContextError):
    """Account startup reached a safe boundary after a stop request."""


class AccountContextCloseTimeout(
    AccountContextError,
    ProcessOwnershipError,
    TimeoutError,
):
    """Account browser ownership remains unresolved after bounded cleanup."""


class RealmTabBindingError(AccountContextError):
    """A realm driver or operation attempted to use a different browser tab."""


class RealmBindingViolationError(AccountContextError):
    """A completed realm operation left its tab in another realm."""


class RealmNotEnabledError(AccountContextError):
    """A caller requested a runtime that this context did not enable."""


class AccountContextState(StrEnum):
    """Lifecycle state of :class:`HentaiVerseAccountContext`."""

    NEW = "new"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


def _normalize_account_label(account_label: str | None) -> str | None:
    if account_label is None:
        return None
    if not isinstance(account_label, str):
        raise TypeError("account_label must be a string or None")
    normalized = account_label.strip()
    if not normalized:
        raise ValueError("account_label must not be empty")
    return normalized


def realm_root_url(realm: Realm) -> str:
    """Return the canonical home URL for ``realm``."""

    if not isinstance(realm, Realm):
        raise TypeError("realm must be a Realm")
    if realm is Realm.ISEKAI:
        return HENTAIVERSE_ISEKAI_ROOT_URL
    return HENTAIVERSE_ROOT_URL


async def _default_authenticator(driver: RealmBoundHVDriver) -> None:
    await driver.login()


class RealmBoundHVDriver(HVDriver):
    """An :class:`HVDriver` permanently attached to one owner-managed target.

    The browser is externally owned by :class:`BrowserOwner`.  This driver must
    therefore never launch or close the browser.  Its inherited HentaiVerse
    methods remain available for existing hvbrowser and hvbattle clients, while
    the ``page`` property rejects replacement after the initial binding.
    """

    def __init__(
        self,
        realm: Realm,
        transport: TabTransport[Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if not isinstance(realm, Realm):
            raise TypeError("realm must be a Realm")
        if transport.role != realm.value:
            raise RealmTabBindingError(
                f"tab role {transport.role!r} does not match realm {realm.value!r}"
            )

        self._realm_page: Any = None
        self._realm_page_frozen = False
        self._realm = realm
        self._transport = transport
        self._bound = False
        super().__init__(*args, **kwargs)

    @property
    def page(self) -> Any:
        """The fixed raw tab used by inherited browser clients."""

        return self._realm_page

    @page.setter
    def page(self, value: Any) -> None:
        current = getattr(self, "_realm_page", None)
        if getattr(self, "_realm_page_frozen", False) and value is not current:
            raise RealmTabBindingError(
                f"cannot replace the {self._realm.value} realm tab"
            )
        self._realm_page = value

    @property
    def realm(self) -> Realm:
        return self._realm

    @property
    def transport(self) -> TabTransport[Any]:
        return self._transport

    @property
    def tab_handle(self) -> TabHandle:
        return self._transport.handle

    @property
    def is_bound(self) -> bool:
        return self._bound

    async def bind(self, browser: Any) -> Self:
        """Bind once to this driver's transport without taking ownership."""

        if self._bound:
            raise RealmTabBindingError(f"{self._realm.value} driver is already bound")

        async def bind_tab(tab: Any) -> None:
            self.bind_existing_browser(browser, tab, owns_browser=False)
            self._realm_page_frozen = True
            self._bound = True

        await self._transport.execute(bind_tab)
        return self

    async def _init_browser(self) -> None:
        raise AccountContextStateError(
            "a realm-bound driver cannot launch a browser; start its account "
            "context instead"
        )


class RealmRuntime[TabT]:
    """Execute work on one fixed realm tab and enforce realm identity.

    Calls to :meth:`execute` hold both the runtime operation lock and the
    underlying target lock.  Existing clients may use the supplied bound driver
    inside the callback without consulting a mutable current-page pointer.
    """

    def __init__(
        self,
        *,
        realm: Realm,
        transport: TabTransport[TabT],
        driver: RealmBoundHVDriver,
        current_url_reader: CurrentUrlReader[TabT] | None,
        account_label: str | None = None,
    ) -> None:
        if transport.role != realm.value:
            raise RealmTabBindingError(
                f"tab role {transport.role!r} does not match realm {realm.value!r}"
            )
        if driver.realm is not realm or driver.transport is not transport:
            raise RealmTabBindingError(
                "realm driver and runtime must share one realm transport"
            )
        if not driver.is_bound:
            raise RealmTabBindingError("realm driver must be bound before use")

        self._realm = realm
        self._transport = transport
        self._driver = driver
        self._current_url_reader = current_url_reader
        self._account_label = _normalize_account_label(account_label)
        self._operation_lock = asyncio.Lock()

    @property
    def realm(self) -> Realm:
        return self._realm

    @property
    def account_label(self) -> str | None:
        """The optional user-facing account label attached to runtime logs."""

        return self._account_label

    @property
    def transport(self) -> TabTransport[TabT]:
        return self._transport

    @property
    def tab_handle(self) -> TabHandle:
        return self._transport.handle

    @property
    def driver(self) -> RealmBoundHVDriver:
        """The immutable-page driver used by operations in this runtime."""

        return self._driver

    async def _read_current_url(self) -> object:
        with log_context(
            account=self._account_label,
            realm=self._realm.value,
            tab_role=self.tab_handle.role,
        ):

            async def read(tab: TabT) -> object:
                reader = self._current_url_reader
                if reader is None:
                    page = cast(Any, tab)
                    return await wait_for_zendriver(
                        page.evaluate("window.location.href"),
                        timeout=PROTOCOL_COMMAND_TIMEOUT_SECONDS,
                        owner=page,
                    )
                return await wait_for_zendriver(
                    reader(tab),
                    timeout=PROTOCOL_COMMAND_TIMEOUT_SECONDS,
                    owner=tab,
                )

            return await self._transport.execute(read)

    async def current_realm(self) -> Realm:
        """Inspect the realm currently displayed by the fixed tab."""

        return realm_from_url(await self._read_current_url())

    async def _assert_current_realm(self) -> None:
        current = await self.current_realm()
        if current is not self._realm:
            raise RealmTabBindingError(
                f"{self._realm.value} runtime observed the {current.value} realm"
            )

    async def _navigate_home(self) -> None:
        await self._transport.navigate(realm_root_url(self._realm))
        await self._assert_current_realm()

    async def ensure_realm(self) -> None:
        """Restore this tab to its bound realm if it drifted elsewhere."""

        with log_context(
            account=self._account_label,
            realm=self._realm.value,
            tab_role=self.tab_handle.role,
        ):
            async with self._operation_lock:
                try:
                    await self._assert_current_realm()
                except RealmDetectionError, RealmTabBindingError:
                    await self._navigate_home()

    async def _ensure_before_operation(self) -> None:
        try:
            await self._assert_current_realm()
        except RealmDetectionError, RealmTabBindingError:
            await self._navigate_home()

    async def _restore_after_operation(self) -> None:
        try:
            await self._assert_current_realm()
        except (RealmDetectionError, RealmTabBindingError) as drift_error:
            try:
                await self._navigate_home()
            except BaseException as restore_error:
                if not isinstance(
                    restore_error, Exception
                ) or is_browser_generation_error(restore_error):
                    restore_error.add_note(
                        "browser generation failed while restoring the bound realm"
                    )
                    raise
                drift_error.add_note(
                    "restoring the bound realm also failed: "
                    f"{type(restore_error).__name__}"
                )
            raise RealmBindingViolationError(
                f"operation left the {self._realm.value} realm tab outside its binding"
            ) from drift_error

    async def execute[ResultT](
        self,
        operation: Callable[[RealmBoundHVDriver], Awaitable[ResultT]],
    ) -> ResultT:
        """Run one operation against the fixed driver and verify its realm."""

        with log_context(
            account=self._account_label,
            realm=self._realm.value,
            tab_role=self.tab_handle.role,
        ):
            async with self._operation_lock:
                await self._ensure_before_operation()

                async def run(_: TabT) -> ResultT:
                    return await operation(self._driver)

                try:
                    result = await self._transport.execute(run)
                except BaseException as operation_error:
                    if not isinstance(
                        operation_error, Exception
                    ) or is_browser_generation_error(operation_error):
                        raise
                    try:
                        await self._restore_after_operation()
                    except RealmBindingViolationError as binding_error:
                        operation_error.add_note(
                            "the realm operation failed after leaving its bound realm; "
                            f"the tab was restored ({type(binding_error).__name__})"
                        )
                    raise

                await self._restore_after_operation()
                return result

    async def navigate(self, url: str) -> None:
        """Navigate within this runtime's realm, rejecting cross-realm URLs."""

        try:
            destination = realm_from_url(url)
        except RealmDetectionError as error:
            raise RealmTabBindingError(
                f"{self._realm.value} runtime rejected an untrusted destination"
            ) from error
        if destination is not self._realm:
            raise RealmTabBindingError(
                f"{self._realm.value} runtime rejected a cross-realm destination"
            )

        with log_context(
            account=self._account_label,
            realm=self._realm.value,
            tab_role=self.tab_handle.role,
            activity="Navigation",
        ):
            async with self._operation_lock:
                await self._ensure_before_operation()
                await self._transport.navigate(url)
                await self._restore_after_operation()

    async def go_home(self) -> None:
        """Navigate the fixed tab to its canonical realm root."""

        await self.navigate(realm_root_url(self._realm))

    async def _authenticate(
        self,
        authenticator: AccountAuthenticator,
    ) -> None:
        """Authenticate the account through this runtime's bootstrap tab.

        Authentication applies to the shared browser account, not to the
        Persistent realm that happens to host the bootstrap tab.  Keep the tab
        role as diagnostic metadata while giving user-facing records an
        explicit account-level label and no realm identity.
        """

        async with self._operation_lock:
            with log_context(
                account=self._account_label,
                tab_role=self.tab_handle.role,
                activity="Login",
                scope=_ACCOUNT_LOGIN_LOG_SCOPE,
            ):

                async def authenticate(_: TabT) -> None:
                    await authenticator(self._driver)

                await self._transport.execute(authenticate)
            await self._assert_current_realm()

    async def _establish(self) -> None:
        with log_context(
            account=self._account_label,
            realm=self._realm.value,
            tab_role=self.tab_handle.role,
            activity="Establish",
        ):
            async with self._operation_lock:
                await self._navigate_home()


class HentaiVerseAccountContext[BrowserT, TabT]:
    """Own one authenticated browser and its enabled realm runtimes.

    Authentication always uses the browser factory's Persistent main tab.  An
    Isekai-only context retains that tab as a private authentication bootstrap,
    while exposing only the requested Isekai runtime.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        authenticator: AccountAuthenticator | None = None,
        browser_factory: BrowserFactory[BrowserT, TabT] | None = None,
        browser_closer: BrowserCloser[BrowserT] | None = None,
        tab_factory: TabFactory[BrowserT, TabT] | None = None,
        tab_navigator: TabNavigator[TabT] | None = None,
        target_id_getter: TargetIdGetter[TabT] | None = None,
        current_url_reader: CurrentUrlReader[TabT] | None = None,
        driver_factory: BoundDriverFactory[TabT] | None = None,
        owner_id: str | None = None,
        account_label: str | None = None,
        enabled_realms: Collection[Realm] = tuple(Realm),
    ) -> None:
        requested_realms = tuple(enabled_realms)
        if not requested_realms:
            raise ValueError("enabled_realms must contain at least one realm")
        if not all(isinstance(realm, Realm) for realm in requested_realms):
            raise TypeError("enabled_realms entries must be Realm values")
        self._enabled_realms = frozenset(requested_realms)
        self._account_label = _normalize_account_label(account_label)

        default_browser_factory = partial(create_browser, headless=headless)
        resolved_browser_factory = browser_factory or cast(
            BrowserFactory[BrowserT, TabT],
            default_browser_factory,
        )
        self._browser: BrowserT | None = None

        async def capture_browser() -> tuple[BrowserT, TabT]:
            browser, tab = await resolved_browser_factory()
            self._browser = browser
            return browser, tab

        self._owner = BrowserOwner(
            headless=headless,
            main_tab_role=Realm.PERSISTENT.value,
            owner_id=owner_id,
            browser_factory=capture_browser,
            browser_closer=browser_closer
            or cast(BrowserCloser[BrowserT], stop_browser),
            tab_factory=tab_factory,
            tab_navigator=tab_navigator,
            target_id_getter=target_id_getter,
        )
        self._authenticator = authenticator or _default_authenticator
        self._current_url_reader = current_url_reader
        self._driver_factory = driver_factory or cast(
            BoundDriverFactory[TabT],
            lambda realm, transport: RealmBoundHVDriver(
                realm,
                transport,
                headless=headless,
            ),
        )
        self._runtimes: dict[Realm, RealmRuntime[TabT]] = {}
        self._state = AccountContextState.NEW
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._close_deadline: float | None = None
        self._close_timeout_seconds: float | None = None

    @property
    def state(self) -> AccountContextState:
        return self._state

    @property
    def owner(self) -> BrowserOwner[BrowserT, TabT]:
        return self._owner

    @property
    def enabled_realms(self) -> frozenset[Realm]:
        """The realm runtimes this context will expose after startup."""

        return self._enabled_realms

    @property
    def account_label(self) -> str | None:
        """The optional user-facing label attached to this account's logs."""

        return self._account_label

    def _require_open(self) -> None:
        if self._state is not AccountContextState.OPEN:
            raise AccountContextStateError(
                f"account context is not open (state: {self._state.value})"
            )

    @property
    def persistent(self) -> RealmRuntime[TabT]:
        return self.runtime(Realm.PERSISTENT)

    @property
    def isekai(self) -> RealmRuntime[TabT]:
        return self.runtime(Realm.ISEKAI)

    def runtime(self, realm: Realm) -> RealmRuntime[TabT]:
        """Return the fixed runtime for ``realm``."""

        if not isinstance(realm, Realm):
            raise TypeError("realm must be a Realm")
        self._require_open()
        try:
            return self._runtimes[realm]
        except KeyError:
            raise RealmNotEnabledError(
                f"the {realm.value} realm is not enabled for this account context"
            ) from None

    async def _bind_runtime(
        self,
        realm: Realm,
        transport: TabTransport[TabT],
    ) -> RealmRuntime[TabT]:
        browser = self._browser
        if browser is None:
            raise AccountContextStateError("started browser owner has no browser")
        driver = self._driver_factory(realm, transport)
        if not isinstance(driver, RealmBoundHVDriver):
            raise TypeError("driver_factory must return RealmBoundHVDriver")
        await driver.bind(browser)
        return RealmRuntime(
            realm=realm,
            transport=transport,
            driver=driver,
            current_url_reader=self._current_url_reader,
            account_label=self._account_label,
        )

    @staticmethod
    async def _raise_if_startup_stopped(
        stop_requested: Callable[[], bool],
    ) -> None:
        await asyncio.sleep(0)
        if stop_requested():
            raise AccountContextStartupStopped

    @staticmethod
    def _bounded_close_deadline(deadline_at: float | None = None) -> float:
        if deadline_at is not None and (
            isinstance(deadline_at, bool)
            or not isinstance(deadline_at, int | float)
            or not math.isfinite(deadline_at)
        ):
            raise ValueError("deadline_at must be a finite monotonic deadline")
        started_at = time.monotonic()
        local_deadline_at = started_at + _ACCOUNT_CLOSE_TIMEOUT_SECONDS
        return (
            local_deadline_at
            if deadline_at is None
            else min(float(deadline_at), local_deadline_at)
        )

    @staticmethod
    def _raise_close_timeout(message: str) -> None:
        raise AccountContextCloseTimeout(message)

    async def _acquire_lifecycle_for_close(self, *, deadline_at: float) -> None:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            self._raise_close_timeout(
                "Account close deadline expired before lifecycle ownership"
            )
        try:
            async with asyncio.timeout(remaining):
                await self._lifecycle_lock.acquire()
        except TimeoutError as error:
            raise AccountContextCloseTimeout(
                "Account close deadline expired while waiting for lifecycle ownership"
            ) from error
        if time.monotonic() >= deadline_at:
            self._lifecycle_lock.release()
            self._raise_close_timeout(
                "Account close deadline expired while waiting for lifecycle ownership"
            )

    def _ensure_close_task(
        self,
        *,
        deadline_at: float,
    ) -> asyncio.Task[None]:
        task = self._close_task
        if task is None:
            started_at = time.monotonic()
            self._close_timeout_seconds = max(
                0.0,
                deadline_at - started_at,
            )
            self._close_deadline = deadline_at
            # Bind every deadline field before task creation so eager task
            # factories cannot start owner cleanup against incomplete state.
            task = asyncio.create_task(
                self._owner.close(deadline=BrowserOperationDeadline(deadline_at))
            )
            task.add_done_callback(self._observe_close_task)
            self._close_task = task
        return task

    @staticmethod
    def _observe_close_task(task: asyncio.Task[None]) -> None:
        """Retrieve detached cleanup failures without consuming later result()."""

        if not task.cancelled():
            task.exception()

    def _record_close_attempt(
        self,
        task: asyncio.Task[None],
        cleanup_error: BaseException | None,
    ) -> None:
        """Retain running cleanup; let a completed ownership failure be retried."""

        if self._close_task is not task:
            return
        if task.done():
            self._close_task = None
            self._close_deadline = None
            self._close_timeout_seconds = None
        self._state = (
            AccountContextState.CLOSED
            if cleanup_error is None
            else AccountContextState.CLOSING
        )

    def _reconcile_completed_close_attempt(self) -> None:
        """Retire a finished generation before an explicit close retry."""

        task = self._close_task
        if task is None or not task.done():
            return
        cleanup_error: BaseException | None
        if task.cancelled():
            cleanup_error = AccountContextCloseTimeout(
                "Account browser cleanup task was cancelled before ownership proof"
            )
        else:
            cleanup_error = task.exception()
        self._record_close_attempt(task, cleanup_error)

    async def _wait_for_close_task(
        self,
        task: asyncio.Task[None],
        *,
        caller_deadline_at: float,
        shared_deadline_at: float,
        shared_timeout_seconds: float,
    ) -> tuple[BaseException | None, asyncio.CancelledError | None]:
        """Wait for shared cleanup while postponing caller cancellation."""

        delayed_cancellation: asyncio.CancelledError | None = None
        deadline = min(shared_deadline_at, caller_deadline_at)
        while not task.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return (
                    AccountContextCloseTimeout(
                        "Account browser ownership remains unresolved after "
                        f"{shared_timeout_seconds:g} seconds"
                    ),
                    delayed_cancellation,
                )
            try:
                done, _ = await asyncio.wait((task,), timeout=remaining)
                if not done:
                    return (
                        AccountContextCloseTimeout(
                            "Account browser ownership remains unresolved after "
                            f"{shared_timeout_seconds:g} seconds"
                        ),
                        delayed_cancellation,
                    )
            except asyncio.CancelledError as error:
                if delayed_cancellation is None:
                    delayed_cancellation = error

        if time.monotonic() >= deadline:
            return (
                AccountContextCloseTimeout(
                    "Account browser ownership remains unresolved because cleanup "
                    "was reported only after "
                    f"the {shared_timeout_seconds:g}-second deadline"
                ),
                delayed_cancellation,
            )
        try:
            task.result()
        except BrowserOwnershipError as error:
            timeout = AccountContextCloseTimeout(
                "Account browser ownership remains unresolved after "
                f"{shared_timeout_seconds:g} seconds"
            )
            timeout.__cause__ = error
            return timeout, delayed_cancellation
        except BaseException as error:
            return error, delayed_cancellation
        return None, delayed_cancellation

    async def _settle_expired_close_attempt(
        self,
        *,
        caller_deadline_at: float,
    ) -> bool:
        """Settle an older expired task before retrying owner cleanup.

        The task below already received the old absolute deadline.  A later
        explicit ``close`` call may give its cancellation/failure path time to
        finish, but must not start a second owner close concurrently.  Once the
        old task is terminal, its failed attempt is retired and the caller can
        retry with the time still left on its own deadline.
        """

        await self._acquire_lifecycle_for_close(deadline_at=caller_deadline_at)
        try:
            self._reconcile_completed_close_attempt()
            if self._state is AccountContextState.CLOSED:
                return True
            task = self._close_task
            shared_deadline_at = self._close_deadline
            if (
                task is None
                or shared_deadline_at is None
                or shared_deadline_at > time.monotonic()
            ):
                return False
        finally:
            self._lifecycle_lock.release()

        settlement_started_at = time.monotonic()
        settlement_timeout_seconds = max(
            0.0,
            caller_deadline_at - settlement_started_at,
        )
        cleanup_error, delayed_cancellation = await self._wait_for_close_task(
            task,
            caller_deadline_at=caller_deadline_at,
            shared_deadline_at=caller_deadline_at,
            shared_timeout_seconds=settlement_timeout_seconds,
        )

        try:
            await self._acquire_lifecycle_for_close(deadline_at=caller_deadline_at)
        except AccountContextCloseTimeout as record_timeout:
            if cleanup_error is None:
                cleanup_error = record_timeout
        else:
            try:
                self._record_close_attempt(task, cleanup_error)
            finally:
                self._lifecycle_lock.release()

        if delayed_cancellation is not None:
            if cleanup_error is not None:
                delayed_cancellation.add_note(
                    "account browser cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
            if isinstance(cleanup_error, ProcessOwnershipError):
                cleanup_error.add_note(
                    "account close cancellation was postponed while settling "
                    "an expired browser cleanup attempt"
                )
                raise cleanup_error from delayed_cancellation
            raise delayed_cancellation from None
        if not task.done():
            if cleanup_error is None:
                cleanup_error = AccountContextCloseTimeout(
                    "Account browser ownership remains unresolved while settling "
                    "an expired cleanup attempt"
                )
            raise cleanup_error
        return cleanup_error is None

    async def start(
        self,
        *,
        stop_requested: Callable[[], bool] = _never_stop,
    ) -> Self:
        """Start, authenticate once, and establish the enabled realm tabs."""

        if not callable(stop_requested):
            raise TypeError("stop_requested must be callable")

        with log_context(account=self._account_label):
            async with self._lifecycle_lock:
                if self._state is AccountContextState.OPEN:
                    return self
                if self._state is not AccountContextState.NEW:
                    raise AccountContextStateError(
                        f"cannot start account context in {self._state.value!r} state"
                    )

                try:
                    await self._raise_if_startup_stopped(stop_requested)
                    with log_context(scope="Browser"):
                        await self._owner.start()
                    await self._raise_if_startup_stopped(stop_requested)
                    authentication_runtime = await self._bind_runtime(
                        Realm.PERSISTENT,
                        self._owner.main_tab,
                    )
                    await self._raise_if_startup_stopped(stop_requested)
                    await authentication_runtime._authenticate(self._authenticator)
                    await self._raise_if_startup_stopped(stop_requested)

                    runtimes: dict[Realm, RealmRuntime[TabT]] = {}
                    if Realm.PERSISTENT in self._enabled_realms:
                        runtimes[Realm.PERSISTENT] = authentication_runtime
                    if Realm.ISEKAI in self._enabled_realms:
                        await self._raise_if_startup_stopped(stop_requested)
                        isekai_transport = await self._owner.open_tab(
                            Realm.ISEKAI.value
                        )
                        await self._raise_if_startup_stopped(stop_requested)
                        isekai = await self._bind_runtime(
                            Realm.ISEKAI,
                            isekai_transport,
                        )
                        await self._raise_if_startup_stopped(stop_requested)
                        await isekai._establish()
                        await self._raise_if_startup_stopped(stop_requested)
                        runtimes[Realm.ISEKAI] = isekai
                except BaseException as startup_error:
                    self._state = AccountContextState.CLOSING
                    close_deadline_at = self._bounded_close_deadline()
                    close_task = self._ensure_close_task(deadline_at=close_deadline_at)
                    shared_deadline_at = self._close_deadline
                    shared_timeout_seconds = self._close_timeout_seconds
                    if shared_deadline_at is None or shared_timeout_seconds is None:
                        raise AccountContextStateError(
                            "close task has no ownership deadline"
                        )
                    (
                        cleanup_error,
                        delayed_cancellation,
                    ) = await self._wait_for_close_task(
                        close_task,
                        caller_deadline_at=close_deadline_at,
                        shared_deadline_at=shared_deadline_at,
                        shared_timeout_seconds=shared_timeout_seconds,
                    )
                    self._record_close_attempt(close_task, cleanup_error)
                    if cleanup_error is not None:
                        startup_error.add_note(
                            "account browser cleanup also failed: "
                            f"{type(cleanup_error).__name__}"
                        )
                    if delayed_cancellation is not None:
                        if isinstance(cleanup_error, ProcessOwnershipError):
                            cleanup_error.add_note(
                                "account startup cancellation was postponed during "
                                "unresolved browser cleanup"
                            )
                            raise cleanup_error from delayed_cancellation
                        raise delayed_cancellation from None
                    if isinstance(cleanup_error, ProcessOwnershipError):
                        cleanup_error.add_note(
                            "account startup also failed: "
                            f"{type(startup_error).__name__}"
                        )
                        raise cleanup_error from startup_error
                    raise

                self._runtimes = runtimes
                self._state = AccountContextState.OPEN
                return self

    async def close(self, *, deadline_at: float | None = None) -> None:
        """Close the owner, postponing caller cancellation until it finishes."""

        close_deadline_at = self._bounded_close_deadline(deadline_at)
        if await self._settle_expired_close_attempt(
            caller_deadline_at=close_deadline_at
        ):
            return
        await self._acquire_lifecycle_for_close(deadline_at=close_deadline_at)
        try:
            self._reconcile_completed_close_attempt()
            if self._state is AccountContextState.CLOSED:
                return
            self._state = AccountContextState.CLOSING
            close_task = self._ensure_close_task(deadline_at=close_deadline_at)
            shared_deadline_at = self._close_deadline
            shared_timeout_seconds = self._close_timeout_seconds
            if shared_deadline_at is None or shared_timeout_seconds is None:
                raise AccountContextStateError("close task has no ownership deadline")
        finally:
            self._lifecycle_lock.release()

        cleanup_error, delayed_cancellation = await self._wait_for_close_task(
            close_task,
            caller_deadline_at=close_deadline_at,
            shared_deadline_at=shared_deadline_at,
            shared_timeout_seconds=shared_timeout_seconds,
        )
        try:
            await self._acquire_lifecycle_for_close(deadline_at=close_deadline_at)
        except AccountContextCloseTimeout as record_timeout:
            if cleanup_error is None:
                cleanup_error = record_timeout
        else:
            try:
                self._record_close_attempt(close_task, cleanup_error)
            finally:
                self._lifecycle_lock.release()
        if delayed_cancellation is not None:
            if cleanup_error is not None:
                delayed_cancellation.add_note(
                    "account browser cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
            if isinstance(cleanup_error, ProcessOwnershipError):
                cleanup_error.add_note(
                    "account close cancellation was postponed during unresolved "
                    "browser cleanup"
                )
                raise cleanup_error from delayed_cancellation
            raise delayed_cancellation from None
        if cleanup_error is not None:
            raise cleanup_error

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: Any,
        exc_value: Any,
        traceback: Any,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.close()


__all__ = [
    "AccountAuthenticator",
    "AccountContextCloseTimeout",
    "AccountContextError",
    "AccountContextStartupStopped",
    "AccountContextState",
    "AccountContextStateError",
    "BoundDriverFactory",
    "CurrentUrlReader",
    "HentaiVerseAccountContext",
    "RealmBindingViolationError",
    "RealmBoundHVDriver",
    "RealmNotEnabledError",
    "RealmRuntime",
    "RealmTabBindingError",
    "realm_root_url",
]
