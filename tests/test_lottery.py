import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from hbrowser import BrowserMutationOutcomeUnknownError

from hvbrowser import (
    LotteryClient,
    LotteryKind,
    LotteryPageError,
    LotterySnapshot,
    LotteryStateChangedError,
    LotterySubmissionError,
    MaintenanceNavigationContext,
)
from hvbrowser.lottery import _LotteryPageState
from hvbrowser.runtime import PageStateTimeout, ZendriverOperationTimeout


class _TicketInput:
    def __init__(self) -> None:
        self.value = ""

    async def clear_input(self) -> None:
        self.value = ""

    async def send_keys(self, value: str) -> None:
        self.value = value


class _Page:
    def __init__(self, *, gp: int = 1_600_000, tickets: int = 200) -> None:
        self.gp = gp
        self.tickets = tickets
        self.has_input = True
        self.can_submit = True
        self.error: str | None = None
        self.input = _TicketInput()
        self.submit_error: Exception | None = None
        self.preserve_after_submit = False
        self.server_rejection: str | None = None
        self.submissions = 0
        self.expressions: list[str] = []

    def state(self) -> dict[str, object]:
        return {
            "balanceText": f"You currently have {self.gp:,} GP",
            "ticketText": f"You hold {self.tickets:,} tickets",
            "hasTicketInput": self.has_input,
            "canSubmit": self.can_submit,
            "errorText": self.error,
        }

    async def evaluate(self, expression: str) -> object:
        self.expressions.append(expression)
        if expression == "submit_buy()":
            self.submissions += 1
            if self.submit_error is not None:
                raise self.submit_error
            if self.server_rejection is not None:
                self.error = self.server_rejection
                return None
            amount = int(self.input.value)
            if not self.preserve_after_submit:
                self.tickets += amount
                self.gp -= amount * 1_000
            return None
        return self.state()

    async def query_selector(self, selector: str) -> Any:
        if selector != "#ticket_temp":
            raise AssertionError(f"unexpected selector: {selector}")
        return self.input if self.has_input else None


def _client(page: _Page) -> LotteryClient:
    return LotteryClient(type("Driver", (), {"page": page})())


class LotteryClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_parses_one_atomic_snapshot(self) -> None:
        page = _Page()
        client = _client(page)
        client._navigate = AsyncMock()  # type: ignore[method-assign]

        snapshot = await client.inspect(
            LotteryKind.WEAPON,
            context=MaintenanceNavigationContext.ORDINARY,
        )

        self.assertEqual(snapshot, LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200))
        self.assertEqual(len(page.expressions), 1)

    async def test_inspect_retries_direct_url_once_on_unreadable_snapshot(self) -> None:
        page = _Page()
        page.evaluate = AsyncMock(
            side_effect=[
                {**page.state(), "balanceText": "no GP"},
                {**page.state(), "balanceText": "still no GP"},
            ]
        )
        client = _client(page)
        client._navigate = AsyncMock()  # type: ignore[method-assign]
        client._open_directly = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(LotteryPageError, "GP balance"):
            await client.inspect(
                LotteryKind.ARMOR,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        client._open_directly.assert_awaited_once()

    async def test_invalid_amount_is_rejected_before_inspection(self) -> None:
        client = _client(_Page())
        client.inspect = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaises(ValueError):
            await client.purchase(
                LotteryKind.WEAPON,
                0,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        client.inspect.assert_not_awaited()

    async def test_insufficient_gp_stops_before_form_interaction(self) -> None:
        page = _Page(gp=999, tickets=0)
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 999, 0)
        )

        with self.assertRaisesRegex(ValueError, "Insufficient GP"):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.expressions, [])

    async def test_invalid_expected_snapshot_stops_before_inspection(self) -> None:
        client = _client(_Page())
        client.inspect = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaises(TypeError):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
                expected_before=object(),  # type: ignore[arg-type]
            )

        client.inspect.assert_not_awaited()

    async def test_purchase_requires_fresh_form_state(self) -> None:
        page = _Page(gp=1_599_000, tickets=201)
        client = _client(page)
        before = LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200)
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]

        with self.assertRaises(LotteryStateChangedError):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

    async def test_purchase_confirms_exact_gp_and_ticket_change(self) -> None:
        page = _Page()
        client = _client(page)
        before = LotterySnapshot(LotteryKind.WEAPON, page.gp, page.tickets)
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]

        report = await client.purchase(
            LotteryKind.WEAPON,
            800,
            context=MaintenanceNavigationContext.ORDINARY,
            expected_before=before,
        )

        self.assertEqual(report.after.gp_balance, 800_000)
        self.assertEqual(report.after.tickets, 1_000)
        self.assertEqual(report.spent_gp, 800_000)
        self.assertEqual(page.input.value, "800")
        self.assertEqual(page.submissions, 1)

    async def test_server_rejection_is_typed_without_replay(self) -> None:
        page = _Page(gp=1_000, tickets=0)
        page.server_rejection = "Lottery is closed"
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )

        with self.assertRaisesRegex(LotterySubmissionError, "Lottery is closed"):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.submissions, 1)

    async def test_delayed_success_does_not_replay_submission(self) -> None:
        page = _Page(gp=1_000, tickets=0)
        page.preserve_after_submit = True
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )
        confirmed = _LotteryPageState(
            "You currently have 0 GP",
            "You hold 1 tickets",
            True,
            True,
            None,
        )

        state_wait = AsyncMock(return_value=confirmed)
        with patch(
            "hvbrowser.lottery.wait_for_page_state",
            new=state_wait,
        ):
            report = await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(report.after.tickets, 1)
        self.assertEqual(page.submissions, 1)
        receipt_deadline = state_wait.await_args.kwargs["deadline"]
        self.assertGreater(receipt_deadline.remaining(), 6)

    async def test_missing_submit_api_fails_before_input_mutation(self) -> None:
        page = _Page(gp=1_000, tickets=0)
        page.can_submit = False
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )

        with self.assertRaisesRegex(LotteryPageError, "API is missing"):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.input.value, "")

    async def test_missing_ticket_input_fails_before_submission(self) -> None:
        page = _Page(gp=1_000, tickets=0)
        page.has_input = False
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )

        with self.assertRaisesRegex(LotteryPageError, "input is missing"):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.submissions, 0)

    async def test_submission_failure_has_unknown_outcome(self) -> None:
        page = _Page(gp=1_000, tickets=0)
        page.submit_error = RuntimeError("disconnected")
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )

        with self.assertRaises(BrowserMutationOutcomeUnknownError):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.submissions, 1)

    async def test_semantic_timeout_reports_unconfirmed_result(self) -> None:
        page = _Page(gp=1_000, tickets=0)
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )

        with (
            patch(
                "hvbrowser.lottery.wait_for_page_state",
                new=AsyncMock(side_effect=PageStateTimeout("unchanged")),
            ),
            self.assertRaisesRegex(LotterySubmissionError, "Unable to confirm"),
        ):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.submissions, 1)

    async def test_generation_error_during_receipt_is_propagated(self) -> None:
        page = _Page(gp=1_000, tickets=0)
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )
        timeout = ZendriverOperationTimeout(timeout_seconds=5)

        with (
            patch(
                "hvbrowser.lottery.wait_for_page_state",
                new=AsyncMock(side_effect=timeout),
            ),
            self.assertRaises(ZendriverOperationTimeout) as raised,
        ):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertIs(raised.exception, timeout)
        self.assertEqual(page.submissions, 1)


if __name__ == "__main__":
    unittest.main()
