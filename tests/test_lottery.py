import unittest
from collections.abc import Callable
from typing import Any, cast
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
        self.clear_calls = 0
        self.send_calls = 0

    async def clear_input(self) -> None:
        self.clear_calls += 1
        self.value = ""

    async def send_keys(self, value: str) -> None:
        self.send_calls += 1
        self.value = value


class _Page:
    def __init__(self, *, gp: int = 1_600_000, tickets: int = 200) -> None:
        self.gp = gp
        self.tickets = tickets
        self.sold_tickets = max(tickets, 3_453)
        self.has_input = True
        self.can_submit = True
        self.error: str | None = None
        self.input = _TicketInput()
        self.submit_error: Exception | None = None
        self.preserve_after_submit = False
        self.server_rejection: str | None = None
        self.server_rejection_page_text: str | None = None
        self.submissions = 0
        self.expressions: list[str] = []
        self.page_text_override: str | None = None

    def state(self) -> dict[str, object]:
        return {
            "pageText": (
                self.page_text_override
                if self.page_text_override is not None
                else "\n".join(
                    (
                        f"You currently have {self.gp:,} GP.",
                        "You hold "
                        f"{self.tickets:,} of {self.sold_tickets:,} sold tickets.",
                        "Each ticket costs 1,000 GP.",
                    )
                )
            ),
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
                if self.server_rejection_page_text is not None:
                    self.page_text_override = self.server_rejection_page_text
                return None
            amount = int(self.input.value)
            if not self.preserve_after_submit:
                self.tickets += amount
                self.sold_tickets += amount
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
        state_expression = page.expressions[0]
        self.assertIn("hvbrowser-lottery-state-v2", state_expression)
        self.assertEqual(state_expression.count("document.body.innerText"), 1)
        self.assertNotIn("querySelectorAll", state_expression)
        self.assertNotIn(".textContent", state_expression)

    async def test_inspect_parses_realistic_wrapper_text_without_ancestor_bias(
        self,
    ) -> None:
        page = _Page(gp=8_077_830, tickets=87)
        page.page_text_override = """
            Weapon Lottery
            Drawing 2026
            You hold 87 of 3,453 sold tickets.
            Prizes Remaining: 14
            You currently have 8,077,830 GP.
            Each ticket costs 1,000 GP.
        """
        client = _client(page)
        client._navigate = AsyncMock()  # type: ignore[method-assign]

        snapshot = await client.inspect(
            LotteryKind.WEAPON,
            context=MaintenanceNavigationContext.ORDINARY,
        )

        self.assertEqual(
            snapshot,
            LotterySnapshot(LotteryKind.WEAPON, 8_077_830, 87, 1_000),
        )

    async def test_inspect_ignores_unrelated_numbers_and_field_order(self) -> None:
        page = _Page()
        page.page_text_override = """
            Round 19
            Each ticket costs 1,000 GP.
            You currently have 8,077,830 GP.
            Jackpot 25,000 GP
            You hold 87 of 3,453 sold tickets.
        """
        client = _client(page)
        client._navigate = AsyncMock()  # type: ignore[method-assign]

        snapshot = await client.inspect(
            LotteryKind.ARMOR,
            context=MaintenanceNavigationContext.ORDINARY,
        )

        self.assertEqual(snapshot.gp_balance, 8_077_830)
        self.assertEqual(snapshot.tickets, 87)
        self.assertEqual(snapshot.ticket_price_gp, 1_000)

    async def test_inspect_accepts_zero_plain_grouped_and_nonbreaking_spaces(
        self,
    ) -> None:
        cases = (
            ("0", "1", 0, 1),
            ("999", "0", 999, 0),
            ("8,077,830", "12,345", 8_077_830, 12_345),
        )
        for gp_text, ticket_text, expected_gp, expected_tickets in cases:
            with self.subTest(gp=gp_text, tickets=ticket_text):
                page = _Page()
                page.page_text_override = (
                    "You\N{NO-BREAK SPACE}hold "
                    f"{ticket_text} of 12,345 sold tickets.\n"
                    f"You currently have {gp_text} GP.\n"
                    "Each ticket costs 1,000 GP."
                )
                client = _client(page)
                client._navigate = AsyncMock()  # type: ignore[method-assign]

                snapshot = await client.inspect(
                    LotteryKind.WEAPON,
                    context=MaintenanceNavigationContext.ORDINARY,
                )

                self.assertEqual(snapshot.gp_balance, expected_gp)
                self.assertEqual(snapshot.tickets, expected_tickets)

    async def test_inspect_rejects_missing_malformed_or_ambiguous_fields(self) -> None:
        valid = """
            You hold 87 of 3,453 sold tickets.
            You currently have 8,077,830 GP.
            Each ticket costs 1,000 GP.
        """
        invalid_texts = {
            "malformed GP grouping": valid.replace("8,077,830", "8,07,830"),
            "leading-zero tickets": valid.replace("hold 87", "hold 087"),
            "Unicode digits": valid.replace("hold 87", "hold ８７"),
            "malformed ticket grouping": valid.replace("hold 87", "hold 8,7"),
            "missing balance": valid.replace(
                "You currently have 8,077,830 GP.", "Balance unavailable."
            ),
            "duplicate balance": valid + "\nYou currently have 1 GP.",
            "duplicate tickets": valid + "\nYou hold 1 of 3,453 sold tickets.",
            "missing sold total": valid.replace(
                "87 of 3,453 sold tickets", "87 tickets"
            ),
            "malformed sold total": valid.replace("3,453", "3,45"),
            "ticket count exceeds sold total": valid.replace("3,453", "86"),
            "duplicate price": valid + "\nEach ticket costs 1,000 GP.",
            "wrong price": valid.replace("costs 1,000 GP", "costs 2,000 GP"),
        }
        for name, page_text in invalid_texts.items():
            with self.subTest(case=name):
                page = _Page()
                page.page_text_override = page_text
                client = _client(page)
                client._navigate = AsyncMock()  # type: ignore[method-assign]
                client._open_directly = AsyncMock()  # type: ignore[method-assign]

                with self.assertRaises(LotteryPageError):
                    await client.inspect(
                        LotteryKind.WEAPON,
                        context=MaintenanceNavigationContext.ORDINARY,
                    )

                client._open_directly.assert_awaited_once()

    async def test_inspect_retries_direct_url_once_on_unreadable_snapshot(self) -> None:
        page = _Page()
        page.evaluate = AsyncMock(
            side_effect=[
                {
                    **page.state(),
                    "pageText": (
                        "You hold 200 of 3,453 sold tickets.\n"
                        "Each ticket costs 1,000 GP."
                    ),
                },
                {
                    **page.state(),
                    "pageText": (
                        "You hold 200 of 3,453 sold tickets.\n"
                        "Each ticket costs 1,000 GP."
                    ),
                },
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

    async def test_probe_inspection_fails_closed_without_reload(self) -> None:
        page = _Page()
        page.page_text_override = (
            "You hold 200 of 3,453 sold tickets.\nEach ticket costs 1,000 GP."
        )
        client = _client(page)
        client._navigate = AsyncMock()  # type: ignore[method-assign]
        client._open_directly = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(LotteryPageError, "GP balance"):
            await client.inspect_once(
                LotteryKind.WEAPON,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        client._navigate.assert_awaited_once()
        client._open_directly.assert_not_awaited()
        self.assertEqual(len(page.expressions), 1)

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

        self.assertEqual(page.input.clear_calls, 0)
        self.assertEqual(page.input.send_calls, 0)
        self.assertEqual(page.submissions, 0)

    async def test_malformed_fresh_state_stops_before_any_form_mutation(self) -> None:
        page = _Page(gp=8_077_830, tickets=87)
        page.page_text_override = """
            You hold 87 of 3,453 sold tickets.
            You currently have 8,07,830 GP.
            Each ticket costs 1,000 GP.
        """
        client = _client(page)
        before = LotterySnapshot(LotteryKind.WEAPON, 8_077_830, 87)
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]

        with self.assertRaises(LotteryPageError):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.input.clear_calls, 0)
        self.assertEqual(page.input.send_calls, 0)
        self.assertEqual(page.submissions, 0)

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
        page.server_rejection_page_text = "ambiguous receipt without Lottery fields"
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
            "You currently have 0 GP.\n"
            "You hold 1 of 3,454 sold tickets.\n"
            "Each ticket costs 1,000 GP.",
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

    async def test_receipt_accepts_exact_owned_and_gp_with_changed_sold_total(
        self,
    ) -> None:
        page = _Page(gp=1_000, tickets=0)
        page.preserve_after_submit = True
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )
        ambiguous = _LotteryPageState(
            "You hold 1 of 3,454 sold tickets.\n"
            "You currently have 0 GP.\n"
            "You currently have 1,000 GP.\n"
            "Each ticket costs 1,000 GP.",
            True,
            True,
            None,
        )
        exact = _LotteryPageState(
            "You hold 1 of 4,000 sold tickets.\n"
            "You currently have 0 GP.\n"
            "Each ticket costs 1,000 GP.",
            True,
            True,
            None,
        )

        async def wait_for_receipt(
            _page: object,
            **kwargs: object,
        ) -> _LotteryPageState:
            accept = cast(
                Callable[[_LotteryPageState], bool],
                kwargs["accept"],
            )
            self.assertFalse(accept(ambiguous))
            self.assertTrue(accept(exact))
            return exact

        with patch(
            "hvbrowser.lottery.wait_for_page_state",
            new=AsyncMock(side_effect=wait_for_receipt),
        ):
            report = await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(report.after.gp_balance, 0)
        self.assertEqual(report.after.tickets, 1)
        self.assertEqual(page.submissions, 1)

    async def test_persistently_ambiguous_receipt_is_unknown_without_replay(
        self,
    ) -> None:
        page = _Page(gp=1_000, tickets=0)
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )
        ambiguous = _LotteryPageState(
            "You hold 1 of 3,454 sold tickets.\n"
            "You currently have 0 GP.\n"
            "You currently have 1,000 GP.\n"
            "Each ticket costs 1,000 GP.",
            True,
            True,
            None,
        )

        async def reject_receipt(
            _page: object,
            **kwargs: object,
        ) -> _LotteryPageState:
            accept = cast(
                Callable[[_LotteryPageState], bool],
                kwargs["accept"],
            )
            self.assertFalse(accept(ambiguous))
            raise PageStateTimeout("ambiguous")

        with (
            patch(
                "hvbrowser.lottery.wait_for_page_state",
                new=AsyncMock(side_effect=reject_receipt),
            ),
            self.assertRaisesRegex(LotterySubmissionError, "Unable to confirm"),
        ):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.submissions, 1)

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
