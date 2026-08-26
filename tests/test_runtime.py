import asyncio
import unittest
from collections import defaultdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from hbrowser import BrowserMutationOutcomeUnknownError
from hbrowser.gallery.utils import (
    ZendriverOperationTimeout as HbrowserZendriverOperationTimeout,
)
from hbrowser.gallery.utils import (
    is_browser_generation_error as hbrowser_is_browser_generation_error,
)
from hbrowser.gallery.utils import wait_for_zendriver as hbrowser_wait_for_zendriver
from zendriver import cdp

import hvbrowser.runtime as runtime
from hvbrowser.runtime import (
    Deadline,
    PageStateTimeout,
    ZendriverOperationTimeout,
    evaluate_page,
    invoke_mutation,
    is_browser_generation_error,
    query_page,
    wait_for_page_state,
    wait_for_zendriver,
)


class RuntimeBoundaryTests(unittest.TestCase):
    def test_reexports_zendriver_timeout_primitives(self) -> None:
        self.assertIs(ZendriverOperationTimeout, HbrowserZendriverOperationTimeout)
        self.assertIs(
            is_browser_generation_error,
            hbrowser_is_browser_generation_error,
        )
        self.assertIs(wait_for_zendriver, hbrowser_wait_for_zendriver)

    def test_runtime_does_not_export_blanket_connection_error_classifier(self) -> None:
        self.assertFalse(hasattr(runtime, "is_connection_error"))


class _EventPage:
    def __init__(self) -> None:
        self.state = 0
        self.observer_armed = asyncio.Event()
        self.handlers: dict[type[Any], list[Any]] = defaultdict(list)
        self.removed: list[tuple[type[Any], Any]] = []
        self.fail_send = False
        self.snapshot_calls = 0

    def add_handler(self, event_type: type[Any], handler: Any) -> None:
        self.handlers[event_type].append(handler)

    def remove_handlers(self, event_type: type[Any], handler: Any) -> None:
        self.removed.append((event_type, handler))
        if handler in self.handlers[event_type]:
            self.handlers[event_type].remove(handler)

    async def send(self, _command: object) -> None:
        if self.fail_send:
            raise RuntimeError("subscription failed")

    async def evaluate(self, expression: str) -> object:
        if "hvbrowser:arm-dom-observer" in expression:
            self.observer_armed.set()
            return True
        if expression == "snapshot":
            self.snapshot_calls += 1
            return self.state
        raise AssertionError(f"unexpected expression: {expression}")

    async def query_selector(self, _selector: str) -> object | None:
        return None

    async def emit_dom_change(self) -> None:
        event = SimpleNamespace(
            name="__hvbrowser_dom_changed__",
            payload="changed",
        )
        for handler in tuple(self.handlers[cdp.runtime.BindingCalled]):
            await handler(event)


class RuntimeStateWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_budget_is_capped_by_shared_deadline(self) -> None:
        page = _EventPage()
        observed: list[float] = []

        async def record(
            awaitable: Any,
            *,
            timeout: float,
            owner: Any,
        ) -> Any:
            del owner
            observed.append(timeout)
            return await awaitable

        with patch("hvbrowser.runtime.wait_for_zendriver", side_effect=record):
            await evaluate_page(page, "snapshot", deadline=Deadline.after(0.2))

        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        self.assertLessEqual(observed[0], 0.2)

    async def test_late_read_results_are_rejected_by_shared_deadline(self) -> None:
        page = _EventPage()

        async def complete(awaitable: Any, **_kwargs: Any) -> object:
            return await awaitable

        for operation in (
            lambda deadline: evaluate_page(page, "snapshot", deadline=deadline),
            lambda deadline: query_page(page, "#stamina", deadline=deadline),
        ):
            with self.subTest(operation=operation):
                deadline = Mock(spec=Deadline)
                deadline.command_timeout.return_value = 1.0
                deadline.remaining.return_value = 0.0
                with (
                    patch(
                        "hvbrowser.runtime.wait_for_zendriver",
                        side_effect=complete,
                    ),
                    self.assertRaisesRegex(PageStateTimeout, "completed after"),
                ):
                    await operation(deadline)

    async def test_late_mutation_acknowledgement_is_outcome_unknown(self) -> None:
        owner = SimpleNamespace()
        deadline = Mock(spec=Deadline)
        deadline.command_timeout.return_value = 1.0
        deadline.expires_at = asyncio.get_running_loop().time() - 1

        with self.assertRaises(BrowserMutationOutcomeUnknownError):
            await invoke_mutation(
                lambda: asyncio.sleep(0),
                owner=owner,
                operation="late mutation",
                deadline=deadline,
            )

        self.assertTrue(
            is_browser_generation_error(BrowserMutationOutcomeUnknownError())
        )

    async def test_expired_deadline_does_not_invoke_mutation_factory(self) -> None:
        invoked = False

        def mutation() -> Any:
            nonlocal invoked
            invoked = True
            raise AssertionError("mutation must not be invoked")

        deadline = Deadline(asyncio.get_running_loop().time() - 1)
        with self.assertRaises(PageStateTimeout):
            await invoke_mutation(
                mutation,
                owner=object(),
                operation="expired mutation",
                deadline=deadline,
            )

        self.assertFalse(invoked)

    async def test_immediate_state_never_installs_observer(self) -> None:
        page = _EventPage()
        page.state = 1

        result = await wait_for_page_state(
            page,
            snapshot_expression="snapshot",
            decode=int,
            accept=lambda state: state == 1,
            deadline=Deadline.after(1),
            description="state one",
        )

        self.assertEqual(result, 1)
        self.assertEqual(page.handlers, {})
        self.assertFalse(page.observer_armed.is_set())

    async def test_dom_hub_setup_lock_wait_uses_semantic_deadline(self) -> None:
        page = _EventPage()
        hub = runtime._DomChangeHub(page)
        await hub._setup_lock.acquire()
        try:
            with self.assertRaisesRegex(PageStateTimeout, "setup ownership"):
                await hub._ensure_ready(Deadline.after(0.01))
        finally:
            hub._setup_lock.release()

        self.assertEqual(page.handlers, {})

    async def test_ready_dom_hub_rejects_an_expired_caller(self) -> None:
        page = _EventPage()
        hub = runtime._DomChangeHub(page)
        hub._ready = True

        with self.assertRaisesRegex(PageStateTimeout, "completed after"):
            await hub._ensure_ready(Deadline(asyncio.get_running_loop().time() - 1))

    async def test_late_page_subscription_does_not_register_binding(self) -> None:
        page = _EventPage()
        hub = runtime._DomChangeHub(page)
        deadline = Mock(spec=Deadline)
        deadline.remaining.side_effect = (1.0, 1.0, 1.0, 0.0)
        deadline.command_timeout.return_value = 1.0
        send_count = 0

        async def finish_late(awaitable: Any, **_kwargs: Any) -> object:
            nonlocal send_count
            send_count += 1
            return await awaitable

        with (
            patch(
                "hvbrowser.runtime.wait_for_zendriver",
                side_effect=finish_late,
            ),
            self.assertRaisesRegex(PageStateTimeout, "completed after"),
        ):
            await hub._ensure_ready(deadline)

        self.assertEqual(send_count, 1)
        self.assertFalse(hub._ready)

    async def test_late_snapshot_is_not_accepted_after_semantic_deadline(
        self,
    ) -> None:
        page = _EventPage()
        accept = Mock(return_value=True)
        deadline = Mock(spec=Deadline)
        deadline.remaining.return_value = 0.0

        with (
            patch(
                "hvbrowser.runtime.evaluate_page",
                new=AsyncMock(return_value=1),
            ),
            self.assertRaisesRegex(PageStateTimeout, "semantic deadline"),
        ):
            await wait_for_page_state(
                page,
                snapshot_expression="snapshot",
                decode=int,
                accept=accept,
                deadline=deadline,
                description="state one",
            )

        accept.assert_not_called()

    async def test_decode_or_accept_cannot_finish_after_semantic_deadline(
        self,
    ) -> None:
        page = _EventPage()
        for expiry_stage, remaining_values, expected_accept_calls in (
            ("decode", (1.0, 0.0), 0),
            ("accept", (1.0, 1.0, 0.0), 1),
        ):
            with self.subTest(expiry_stage=expiry_stage):
                accept = Mock(return_value=True)
                deadline = Mock(spec=Deadline)
                deadline.remaining.side_effect = remaining_values
                with (
                    patch(
                        "hvbrowser.runtime.evaluate_page",
                        new=AsyncMock(return_value=1),
                    ),
                    self.assertRaisesRegex(PageStateTimeout, "semantic deadline"),
                ):
                    await wait_for_page_state(
                        page,
                        snapshot_expression="snapshot",
                        decode=int,
                        accept=accept,
                        deadline=deadline,
                        description="state one",
                    )

                self.assertEqual(accept.call_count, expected_accept_calls)

    async def test_delayed_dom_event_wakes_local_future(self) -> None:
        page = _EventPage()
        waiter = asyncio.create_task(
            wait_for_page_state(
                page,
                snapshot_expression="snapshot",
                decode=int,
                accept=lambda state: state == 1,
                deadline=Deadline.after(1),
                description="state one",
            )
        )
        await asyncio.wait_for(page.observer_armed.wait(), timeout=1)
        page.state = 1
        await page.emit_dom_change()

        self.assertEqual(await waiter, 1)
        hub = page._hvbrowser_dom_change_hub
        self.assertEqual(hub._waiters, {})

    async def test_concurrent_waiters_share_one_handler_subscription(self) -> None:
        page = _EventPage()
        deadline = Deadline.after(1)
        waiters = [
            asyncio.create_task(
                wait_for_page_state(
                    page,
                    snapshot_expression="snapshot",
                    decode=int,
                    accept=lambda state: state == 1,
                    deadline=deadline,
                    description="state one",
                )
            )
            for _ in range(2)
        ]
        while not hasattr(page, "_hvbrowser_dom_change_hub"):
            await asyncio.sleep(0)
        while len(page._hvbrowser_dom_change_hub._waiters) < 2:
            await asyncio.sleep(0)
        page.state = 1
        await page.emit_dom_change()

        self.assertEqual(await asyncio.gather(*waiters), [1, 1])
        self.assertTrue(all(not handlers for handlers in page.handlers.values()))

    async def test_reconciliation_snapshot_handles_property_only_change(self) -> None:
        page = _EventPage()
        asyncio.get_running_loop().call_later(0.005, setattr, page, "state", 1)

        with patch(
            "hvbrowser.runtime._STATE_RECONCILIATION_INTERVAL_SECONDS",
            0.01,
        ):
            result = await wait_for_page_state(
                page,
                snapshot_expression="snapshot",
                decode=int,
                accept=lambda state: state == 1,
                deadline=Deadline.after(1),
                description="property-only state one",
            )

        self.assertEqual(result, 1)
        self.assertGreaterEqual(page.snapshot_calls, 3)
        self.assertTrue(all(not handlers for handlers in page.handlers.values()))

    async def test_cancellation_removes_short_lived_handlers(self) -> None:
        page = _EventPage()
        waiter = asyncio.create_task(
            wait_for_page_state(
                page,
                snapshot_expression="snapshot",
                decode=int,
                accept=lambda state: state == 1,
                deadline=Deadline.after(1),
                description="state one",
            )
        )
        await asyncio.wait_for(page.observer_armed.wait(), timeout=1)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        self.assertTrue(all(not handlers for handlers in page.handlers.values()))

    async def test_semantic_timeout_cleans_local_waiter(self) -> None:
        page = _EventPage()

        with self.assertRaises(PageStateTimeout):
            await wait_for_page_state(
                page,
                snapshot_expression="snapshot",
                decode=int,
                accept=lambda state: state == 1,
                deadline=Deadline.after(0.01),
                description="state one",
            )

        hub = page._hvbrowser_dom_change_hub
        self.assertEqual(hub._waiters, {})

    async def test_subscription_failure_registers_no_local_handler(self) -> None:
        page = _EventPage()
        page.fail_send = True

        with self.assertRaises(RuntimeError):
            await wait_for_page_state(
                page,
                snapshot_expression="snapshot",
                decode=int,
                accept=lambda state: state == 1,
                deadline=Deadline.after(1),
                description="state one",
            )

        self.assertTrue(all(not handlers for handlers in page.handlers.values()))
        self.assertEqual(page.removed, [])


if __name__ == "__main__":
    unittest.main()
