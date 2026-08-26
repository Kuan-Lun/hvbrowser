"""Shared browser primitives used by HentaiVerse domain packages.

Protocol liveness and website state convergence are deliberately separate.
Every individual Zendriver command has one short, fixed watchdog; a domain
operation that waits for the page to change uses an absolute semantic deadline
and short-lived DOM observations.  A semantic timeout therefore never becomes
the timeout of a live Zendriver transaction.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Self
from uuid import uuid4

from hbrowser.gallery.browser.process import (
    OwnedProcess,
    ProcessOwnershipError,
    start_owned_process,
)
from hbrowser.gallery.element_action import ElementAction
from hbrowser.gallery.utils import Deadline as BrowserOperationDeadline
from hbrowser.gallery.utils import (
    LogPersistenceError,
    ZendriverOperationTimeout,
    close_forwarded_logging,
    configure_forwarded_logging,
    is_browser_generation_error,
    log_context,
    wait_for_zendriver,
)
from hbrowser.gallery.utils.mutation import wait_for_zendriver_mutation
from hbrowser.notify import notify
from zendriver import cdp

PROTOCOL_COMMAND_TIMEOUT_SECONDS = 5.0
LOCAL_DOM_STATE_TIMEOUT_SECONDS = 5.0
SERVER_STATE_RECEIPT_TIMEOUT_SECONDS = 15.0
_DOM_CHANGE_BINDING = "__hvbrowser_dom_changed__"
_DOM_OBSERVER_ATTRIBUTE = "__hvbrowserDomObserver"
_DOM_CHANGE_PAYLOAD = "changed"
_DOM_HUB_ATTRIBUTE = "_hvbrowser_dom_change_hub"
_STATE_RECONCILIATION_INTERVAL_SECONDS = 1.0


class PageStateTimeout(TimeoutError):
    """A healthy browser did not expose the requested semantic state in time."""


@dataclass(frozen=True, slots=True)
class Deadline:
    """One absolute event-loop deadline shared by all phases of an operation."""

    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> Self:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int | float)
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise ValueError("deadline seconds must be a finite positive number")
        return cls(asyncio.get_running_loop().time() + float(seconds))

    def remaining(self) -> float:
        return max(0.0, self.expires_at - asyncio.get_running_loop().time())

    def command_timeout(self, operation: str) -> float:
        """Return the remaining command budget, capped at five seconds."""

        remaining = self.remaining()
        if remaining <= 0:
            raise PageStateTimeout(
                f"{operation} was not started because its semantic deadline expired"
            )
        return min(PROTOCOL_COMMAND_TIMEOUT_SECONDS, remaining)


def _command_timeout(deadline: Deadline | None, operation: str) -> float:
    if deadline is None:
        return PROTOCOL_COMMAND_TIMEOUT_SECONDS
    return deadline.command_timeout(operation)


def _require_timely_result(deadline: Deadline | None, operation: str) -> None:
    if deadline is not None and deadline.remaining() <= 0:
        raise PageStateTimeout(f"{operation} completed after its semantic deadline")


async def evaluate_page(
    page: Any,
    expression: str,
    *,
    deadline: Deadline | None = None,
) -> object:
    """Evaluate one page expression with the fixed protocol-command budget."""

    timeout = _command_timeout(deadline, "Page evaluation")
    result = await wait_for_zendriver(
        page.evaluate(expression),
        timeout=timeout,
        owner=page,
    )
    _require_timely_result(deadline, "Page evaluation")
    return result


async def query_page(
    page: Any,
    selector: str,
    *,
    deadline: Deadline | None = None,
) -> Any:
    """Run one immediate CSS query without Zendriver's selector polling loop."""

    timeout = _command_timeout(deadline, "DOM query")
    result = await wait_for_zendriver(
        page.query_selector(selector),
        timeout=timeout,
        owner=page,
    )
    _require_timely_result(deadline, "DOM query")
    return result


async def invoke_mutation[ResultT](
    operation_call: Callable[[], Awaitable[ResultT]],
    *,
    owner: Any,
    operation: str,
    deadline: Deadline | None = None,
) -> ResultT:
    """Invoke one mutation and wait only for its protocol acknowledgement."""

    timeout = _command_timeout(deadline, operation)
    return await wait_for_zendriver_mutation(
        operation_call(),
        timeout=timeout,
        owner=owner,
        operation=operation,
        completion_deadline_expires_at=(
            deadline.expires_at if deadline is not None else None
        ),
    )


class _DomChangeHub:
    """Wake local futures from short-lived page bindings and lifecycle events."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._ready = False
        self._setup_lock = asyncio.Lock()
        self._waiters: dict[str, asyncio.Future[None]] = {}
        self._handlers: dict[str, tuple[Any, Any]] = {}

    async def _ensure_ready(self, deadline: Deadline) -> None:
        _require_timely_result(deadline, "DOM change observer setup")
        if self._ready:
            return
        remaining = deadline.remaining()
        if remaining <= 0:
            raise PageStateTimeout(
                "DOM change observer setup was not started because its semantic "
                "deadline expired"
            )
        try:
            async with asyncio.timeout(remaining):
                await self._setup_lock.acquire()
        except TimeoutError as error:
            raise PageStateTimeout(
                "DOM change observer setup exceeded its semantic deadline while "
                "waiting for setup ownership"
            ) from error
        try:
            _require_timely_result(deadline, "DOM change observer setup")
            if self._ready:
                # Another setup coroutine may finish while this task awaits the lock.
                return  # type: ignore[unreachable]
            await self._install(deadline)
            _require_timely_result(deadline, "DOM change observer setup")
        finally:
            self._setup_lock.release()

    async def _install(self, deadline: Deadline) -> None:
        """Install the CDP subscriptions shared by short-lived local handlers."""

        page_enable_timeout = deadline.command_timeout(
            "Page lifecycle event subscription"
        )
        await wait_for_zendriver(
            self._page.send(cdp.page.enable()),
            timeout=page_enable_timeout,
            owner=self._page,
        )
        _require_timely_result(deadline, "Page lifecycle event subscription")
        binding_timeout = deadline.command_timeout("DOM change binding registration")
        await wait_for_zendriver(
            self._page.send(cdp.runtime.add_binding(_DOM_CHANGE_BINDING)),
            timeout=binding_timeout,
            owner=self._page,
        )
        _require_timely_result(deadline, "DOM change binding registration")
        self._ready = True
        _require_timely_result(deadline, "DOM change observer setup")

    async def arm(
        self,
        deadline: Deadline,
    ) -> tuple[str, asyncio.Future[None]]:
        await self._ensure_ready(deadline)
        token = uuid4().hex
        future = asyncio.get_running_loop().create_future()

        async def binding_called(event: cdp.runtime.BindingCalled) -> None:
            if (
                event.name == _DOM_CHANGE_BINDING
                and event.payload == _DOM_CHANGE_PAYLOAD
                and not future.done()
            ):
                future.set_result(None)

        async def lifecycle_changed(_event: object) -> None:
            if not future.done():
                future.set_result(None)

        self._waiters[token] = future
        self._handlers[token] = (binding_called, lifecycle_changed)
        self._page.add_handler(cdp.runtime.BindingCalled, binding_called)
        self._page.add_handler(cdp.page.FrameNavigated, lifecycle_changed)
        self._page.add_handler(cdp.page.DomContentEventFired, lifecycle_changed)
        self._page.add_handler(cdp.page.LoadEventFired, lifecycle_changed)
        binding = json.dumps(_DOM_CHANGE_BINDING)
        observer_attribute = json.dumps(_DOM_OBSERVER_ATTRIBUTE)
        payload_json = json.dumps(_DOM_CHANGE_PAYLOAD)
        cleanup_milliseconds = max(1, math.ceil(deadline.remaining() * 1000))
        try:
            installed = await evaluate_page(
                self._page,
                f"""
/* hvbrowser:arm-dom-observer */
(() => {{
    const binding = window[{binding}];
    if (typeof binding !== "function" || !document.documentElement) return false;
    const previous = window[{observer_attribute}];
    if (previous) {{
        clearTimeout(previous.timer);
        previous.observer.disconnect();
    }}
    const observer = new MutationObserver(() => {{
        clearTimeout(timer);
        observer.disconnect();
        window[{observer_attribute}] = null;
        binding({payload_json});
    }});
    const timer = setTimeout(() => {{
        observer.disconnect();
        window[{observer_attribute}] = null;
    }}, {cleanup_milliseconds});
    window[{observer_attribute}] = {{observer, timer}};
    observer.observe(document.documentElement, {{
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true,
    }});
    return true;
}})()
""",
                deadline=deadline,
            )
        except BaseException:
            self.disarm(token)
            raise
        if installed is not True:
            self.disarm(token)
            raise RuntimeError("Unable to install HentaiVerse DOM state observer")
        return token, future

    def disarm(self, token: str) -> None:
        self._waiters.pop(token, None)
        handlers = self._handlers.pop(token, None)
        if handlers is None:
            return
        binding_called, lifecycle_changed = handlers
        self._page.remove_handlers(cdp.runtime.BindingCalled, binding_called)
        self._page.remove_handlers(cdp.page.FrameNavigated, lifecycle_changed)
        self._page.remove_handlers(
            cdp.page.DomContentEventFired,
            lifecycle_changed,
        )
        self._page.remove_handlers(cdp.page.LoadEventFired, lifecycle_changed)


def _dom_change_hub(page: Any) -> _DomChangeHub:
    hub = getattr(page, _DOM_HUB_ATTRIBUTE, None)
    if isinstance(hub, _DomChangeHub):
        return hub
    hub = _DomChangeHub(page)
    setattr(page, _DOM_HUB_ATTRIBUTE, hub)
    return hub


async def wait_for_page_state[StateT](
    page: Any,
    *,
    snapshot_expression: str,
    decode: Callable[[object], StateT],
    accept: Callable[[StateT], bool],
    deadline: Deadline,
    description: str,
) -> StateT:
    """Wait for a DOM-backed state without fixed sleeps or stacked deadlines.

    The snapshot is checked before waiting.  If it is not accepted, a page-side
    observer is installed by short acknowledgement-only commands and wakes a
    local Python future through ``Runtime.bindingCalled``.  Only that local
    future consumes the semantic deadline; there is no long-lived CDP command.
    """

    def require_live_deadline() -> None:
        if deadline.remaining() <= 0:
            raise PageStateTimeout(
                f"{description} was not observed before its semantic deadline"
            )

    async def read_checked_state() -> tuple[StateT, bool]:
        raw_state = await evaluate_page(
            page,
            snapshot_expression,
            deadline=deadline,
        )
        # A protocol result can be delivered just after its remaining budget.
        # Never let event-loop scheduling, decoding, or policy evaluation turn
        # that late result into a successful semantic receipt.
        require_live_deadline()
        state = decode(raw_state)
        require_live_deadline()
        accepted = accept(state)
        require_live_deadline()
        return state, accepted

    while True:
        state, accepted = await read_checked_state()
        if accepted:
            return state

        hub = _dom_change_hub(page)
        token, changed = await hub.arm(deadline)
        try:
            # Close the gap between the first snapshot and observer setup.
            state, accepted = await read_checked_state()
            if accepted:
                return state
            remaining = deadline.remaining()
            if remaining <= 0:
                raise PageStateTimeout(
                    f"{description} was not observed before its semantic deadline"
                )
            await asyncio.wait(
                (changed,),
                timeout=min(
                    remaining,
                    _STATE_RECONCILIATION_INTERVAL_SECONDS,
                ),
            )
        finally:
            hub.disarm(token)
        if deadline.remaining() <= 0:
            raise PageStateTimeout(
                f"{description} was not observed before its semantic deadline"
            )


def json_expression(value: object) -> str:
    """Serialize trusted local data for embedding in a browser expression."""

    return json.dumps(value, ensure_ascii=True)


__all__ = [
    "LOCAL_DOM_STATE_TIMEOUT_SECONDS",
    "PROTOCOL_COMMAND_TIMEOUT_SECONDS",
    "SERVER_STATE_RECEIPT_TIMEOUT_SECONDS",
    "BrowserOperationDeadline",
    "Deadline",
    "ElementAction",
    "LogPersistenceError",
    "OwnedProcess",
    "PageStateTimeout",
    "ProcessOwnershipError",
    "ZendriverOperationTimeout",
    "close_forwarded_logging",
    "configure_forwarded_logging",
    "evaluate_page",
    "invoke_mutation",
    "is_browser_generation_error",
    "json_expression",
    "log_context",
    "notify",
    "query_page",
    "start_owned_process",
    "wait_for_page_state",
    "wait_for_zendriver",
]
