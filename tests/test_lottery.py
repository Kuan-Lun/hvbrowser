import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hvbrowser import (
    HVDriver,
    LotteryClient,
    LotteryKind,
    LotteryPageError,
    LotteryPurchaseReport,
    LotterySnapshot,
    LotteryStateChangedError,
    LotterySubmissionError,
)


class LotteryClientTests(unittest.IsolatedAsyncioTestCase):
    def test_constructor_rejects_invalid_confirmation_settings(self) -> None:
        driver = SimpleNamespace(page=SimpleNamespace())

        with self.assertRaisesRegex(ValueError, "at least 1"):
            LotteryClient(driver, confirmation_checks=0)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "at least 1"):
            LotteryClient(driver, confirmation_checks=True)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "finite non-negative"):
            LotteryClient(  # type: ignore[arg-type]
                driver, confirmation_interval=float("nan")
            )

    async def test_inspect_parses_gp_and_ticket_counts(self) -> None:
        page = SimpleNamespace()
        page.xpath = AsyncMock(
            side_effect=[
                [SimpleNamespace(text="You currently have 1,600,000 GP")],
                [SimpleNamespace(text="You hold 200 tickets")],
            ]
        )
        driver = SimpleNamespace(page=page)
        client = LotteryClient(driver)  # type: ignore[arg-type]
        client._navigate = AsyncMock()  # type: ignore[method-assign]

        snapshot = await client.inspect(LotteryKind.WEAPON)

        self.assertEqual(snapshot, LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200))

    async def test_inspect_rejects_invalid_kind_before_navigation(self) -> None:
        driver = SimpleNamespace(page=SimpleNamespace())
        client = LotteryClient(driver)  # type: ignore[arg-type]
        client._navigate = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(TypeError, "LotteryKind"):
            await client.inspect("Weapon Lottery")  # type: ignore[arg-type]

        client._navigate.assert_not_awaited()

    async def test_inspect_rejects_unparseable_page_values(self) -> None:
        page = SimpleNamespace(
            xpath=AsyncMock(
                side_effect=[
                    [SimpleNamespace(text="You currently have no GP")],
                    [SimpleNamespace(text="You hold no tickets")],
                ]
            )
        )
        client = LotteryClient(SimpleNamespace(page=page))  # type: ignore[arg-type]
        client._navigate = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(LotteryPageError, "GP balance"):
            await client.inspect(LotteryKind.ARMOR)

    async def test_purchase_rejects_invalid_amount_before_inspection(self) -> None:
        client = LotteryClient(  # type: ignore[arg-type]
            SimpleNamespace(page=SimpleNamespace())
        )
        client.inspect = AsyncMock()  # type: ignore[method-assign]

        for invalid in (0, -1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    await client.purchase(  # type: ignore[arg-type]
                        LotteryKind.WEAPON, invalid
                    )

        client.inspect.assert_not_awaited()

    async def test_purchase_rejects_insufficient_gp_before_form_interaction(
        self,
    ) -> None:
        page = SimpleNamespace(
            select=AsyncMock(),
            evaluate=AsyncMock(),
        )
        client = LotteryClient(SimpleNamespace(page=page))  # type: ignore[arg-type]
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.ARMOR, 799_999, 100)
        )

        with self.assertRaisesRegex(ValueError, "Insufficient GP"):
            await client.purchase(LotteryKind.ARMOR, 800)

        page.select.assert_not_awaited()
        page.evaluate.assert_not_awaited()

    async def test_purchase_rejects_invalid_expected_snapshot_before_inspection(
        self,
    ) -> None:
        client = LotteryClient(  # type: ignore[arg-type]
            SimpleNamespace(page=SimpleNamespace())
        )
        client.inspect = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(TypeError, "LotterySnapshot or None"):
            await client.purchase(
                LotteryKind.WEAPON,
                1,
                expected_before=object(),  # type: ignore[arg-type]
            )

        client.inspect.assert_not_awaited()

    async def test_purchase_rejects_changed_state_before_form_interaction(
        self,
    ) -> None:
        page = SimpleNamespace(select=AsyncMock(), evaluate=AsyncMock())
        expected = LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200)
        changed = LotterySnapshot(LotteryKind.WEAPON, 1_500_000, 300)
        client = LotteryClient(SimpleNamespace(page=page))  # type: ignore[arg-type]
        client.inspect = AsyncMock(return_value=changed)  # type: ignore[method-assign]

        with self.assertRaisesRegex(LotteryStateChangedError, "plan again"):
            await client.purchase(
                LotteryKind.WEAPON,
                800,
                expected_before=expected,
            )

        page.select.assert_not_awaited()
        page.evaluate.assert_not_awaited()

    async def test_purchase_missing_input_is_a_page_error(self) -> None:
        page = SimpleNamespace(
            select=AsyncMock(return_value=None),
            evaluate=AsyncMock(),
        )
        client = LotteryClient(SimpleNamespace(page=page))  # type: ignore[arg-type]
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )

        with self.assertRaisesRegex(LotteryPageError, "input is missing"):
            await client.purchase(LotteryKind.WEAPON, 1)

        page.evaluate.assert_not_awaited()

    async def test_purchase_missing_submit_api_is_a_page_error(self) -> None:
        ticket_input = SimpleNamespace(
            clear_input=AsyncMock(),
            send_keys=AsyncMock(),
        )
        page = SimpleNamespace(
            select=AsyncMock(return_value=ticket_input),
            evaluate=AsyncMock(return_value=False),
        )
        client = LotteryClient(SimpleNamespace(page=page))  # type: ignore[arg-type]
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )

        with self.assertRaisesRegex(LotteryPageError, "API is missing"):
            await client.purchase(LotteryKind.WEAPON, 1)

        page.evaluate.assert_awaited_once_with("typeof submit_buy === 'function'")

    async def test_purchase_evaluation_failure_has_unknown_outcome(self) -> None:
        ticket_input = SimpleNamespace(
            clear_input=AsyncMock(),
            send_keys=AsyncMock(),
        )
        page = SimpleNamespace(
            select=AsyncMock(return_value=ticket_input),
            evaluate=AsyncMock(side_effect=[True, RuntimeError("disconnected")]),
        )
        client = LotteryClient(SimpleNamespace(page=page))  # type: ignore[arg-type]
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=LotterySnapshot(LotteryKind.WEAPON, 1_000, 0)
        )

        with self.assertRaisesRegex(LotterySubmissionError, "outcome is unknown"):
            await client.purchase(LotteryKind.WEAPON, 1)

    async def test_purchase_requires_exact_post_submit_confirmation(self) -> None:
        ticket_input = SimpleNamespace(
            clear_input=AsyncMock(),
            send_keys=AsyncMock(),
        )
        page = SimpleNamespace(
            select=AsyncMock(return_value=ticket_input),
            evaluate=AsyncMock(side_effect=[True, None]),
        )
        driver = SimpleNamespace(page=page)
        client = LotteryClient(  # type: ignore[arg-type]
            driver, confirmation_checks=1, confirmation_interval=0
        )
        before = LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200)
        after = LotterySnapshot(LotteryKind.WEAPON, 800_000, 1_000)
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]
        client._inspect_current = AsyncMock(  # type: ignore[method-assign]
            return_value=after
        )

        report = await client.purchase(
            LotteryKind.WEAPON,
            800,
            expected_before=before,
        )

        self.assertEqual(report.purchased, 800)
        self.assertEqual(report.spent_gp, 800_000)
        self.assertEqual(report.after, after)
        ticket_input.send_keys.assert_awaited_once_with("800")
        self.assertEqual(
            page.evaluate.await_args_list,
            [
                unittest.mock.call("typeof submit_buy === 'function'"),
                unittest.mock.call("submit_buy()"),
            ],
        )

    async def test_purchase_rejects_unconfirmed_result(self) -> None:
        ticket_input = SimpleNamespace(
            clear_input=AsyncMock(),
            send_keys=AsyncMock(),
        )
        page = SimpleNamespace(
            select=AsyncMock(return_value=ticket_input),
            evaluate=AsyncMock(side_effect=[True, None]),
        )
        driver = SimpleNamespace(page=page)
        client = LotteryClient(  # type: ignore[arg-type]
            driver, confirmation_checks=1, confirmation_interval=0
        )
        before = LotterySnapshot(LotteryKind.ARMOR, 800_000, 100)
        unchanged = LotterySnapshot(LotteryKind.ARMOR, 800_000, 100)
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]
        client._inspect_current = AsyncMock(  # type: ignore[method-assign]
            return_value=unchanged
        )

        with self.assertRaises(LotterySubmissionError):
            await client.purchase(LotteryKind.ARMOR, 800)

    async def test_compatibility_workflow_allocates_shared_gp_in_order(self) -> None:
        driver = object.__new__(HVDriver)
        client = SimpleNamespace(
            inspect=AsyncMock(
                side_effect=[
                    LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200),
                    LotterySnapshot(LotteryKind.ARMOR, 800_000, 100),
                ]
            ),
            purchase=AsyncMock(),
        )

        with patch("hvbrowser.hv.LotteryClient", return_value=client):
            await HVDriver.loetterycheck(driver, 1_000)

        self.assertEqual(
            client.purchase.await_args_list,
            [
                unittest.mock.call(
                    LotteryKind.WEAPON,
                    800,
                    expected_before=LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200),
                ),
                unittest.mock.call(
                    LotteryKind.ARMOR,
                    800,
                    expected_before=LotterySnapshot(LotteryKind.ARMOR, 800_000, 100),
                ),
            ],
        )

    async def test_hvdriver_exposes_explicit_lottery_operations(self) -> None:
        driver = object.__new__(HVDriver)
        before = LotterySnapshot(LotteryKind.ARMOR, 10_000, 10)
        after = LotterySnapshot(LotteryKind.ARMOR, 9_000, 11)
        report = LotteryPurchaseReport(before, 1, after)
        client = SimpleNamespace(
            inspect=AsyncMock(return_value=before),
            purchase=AsyncMock(return_value=report),
        )

        with patch("hvbrowser.hv.LotteryClient", return_value=client):
            inspected = await HVDriver.inspect_lottery(driver, LotteryKind.ARMOR)
            purchased = await HVDriver.purchase_lottery_tickets(
                driver,
                LotteryKind.ARMOR,
                1,
                expected_before=before,
            )

        self.assertIs(inspected, before)
        self.assertIs(purchased, report)
        client.inspect.assert_awaited_once_with(LotteryKind.ARMOR)
        client.purchase.assert_awaited_once_with(
            LotteryKind.ARMOR,
            1,
            expected_before=before,
        )

    async def test_compatibility_workflow_rejects_invalid_target(self) -> None:
        driver = object.__new__(HVDriver)

        for invalid in (-1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "non-negative integer"):
                    await HVDriver.loetterycheck(  # type: ignore[arg-type]
                        driver, invalid
                    )


if __name__ == "__main__":
    unittest.main()
