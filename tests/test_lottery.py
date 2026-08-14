import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from hvbrowser import (
    LotteryClient,
    LotteryKind,
    LotteryPageError,
    LotterySnapshot,
    LotteryStateChangedError,
    LotterySubmissionError,
)


def _client(driver: object, **kwargs: Any) -> LotteryClient:
    navigation = SimpleNamespace(select_bazaar=AsyncMock())
    return LotteryClient(driver, navigation, **kwargs)  # type: ignore[arg-type]


class LotteryClientTests(unittest.IsolatedAsyncioTestCase):
    def test_constructor_rejects_invalid_confirmation_settings(self) -> None:
        driver = SimpleNamespace(page=SimpleNamespace())

        with self.assertRaisesRegex(ValueError, "at least 1"):
            _client(driver, confirmation_checks=0)
        with self.assertRaisesRegex(ValueError, "at least 1"):
            _client(driver, confirmation_checks=True)
        with self.assertRaisesRegex(ValueError, "finite non-negative"):
            _client(driver, confirmation_interval=float("nan"))

    async def test_inspect_parses_gp_and_ticket_counts(self) -> None:
        page = SimpleNamespace()
        page.xpath = AsyncMock(
            side_effect=[
                [SimpleNamespace(text="You currently have 1,600,000 GP")],
                [SimpleNamespace(text="You hold 200 tickets")],
            ]
        )
        driver = SimpleNamespace(page=page)
        client = _client(driver)
        client._navigate = AsyncMock()  # type: ignore[method-assign]

        snapshot = await client.inspect(LotteryKind.WEAPON)

        self.assertEqual(snapshot, LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200))

    async def test_inspect_rejects_invalid_kind_before_navigation(self) -> None:
        driver = SimpleNamespace(page=SimpleNamespace())
        client = _client(driver)
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
        client = _client(SimpleNamespace(page=page))
        client._navigate = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(LotteryPageError, "GP balance"):
            await client.inspect(LotteryKind.ARMOR)

    async def test_purchase_rejects_invalid_amount_before_inspection(self) -> None:
        client = _client(SimpleNamespace(page=SimpleNamespace()))
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
        client = _client(SimpleNamespace(page=page))
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
        client = _client(SimpleNamespace(page=SimpleNamespace()))
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
        client = _client(SimpleNamespace(page=page))
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
        client = _client(SimpleNamespace(page=page))
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
        client = _client(SimpleNamespace(page=page))
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
        client = _client(SimpleNamespace(page=page))
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
        client = _client(driver, confirmation_checks=1, confirmation_interval=0)
        before = LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200)
        after = LotterySnapshot(LotteryKind.WEAPON, 800_000, 1_000)
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]
        client._inspect_current = AsyncMock(  # type: ignore[method-assign]
            return_value=after
        )

        with self.assertLogs("hvbrowser.lottery", level="DEBUG") as captured:
            report = await client.purchase(
                LotteryKind.WEAPON,
                800,
                expected_before=before,
            )

        self.assertEqual(report.purchased, 800)
        self.assertEqual(report.spent_gp, 800_000)
        self.assertEqual(report.after, after)
        self.assertEqual(len(captured.output), 1)
        self.assertTrue(captured.output[0].startswith("DEBUG:hvbrowser.lottery:"))
        self.assertIn("Purchased 800 Weapon Lottery tickets", captured.output[0])
        ticket_input.send_keys.assert_awaited_once_with("800")
        self.assertEqual(
            page.evaluate.await_args_list,
            [
                unittest.mock.call("typeof submit_buy === 'function'"),
                unittest.mock.call("submit_buy()"),
            ],
        )

    async def test_purchase_warns_once_after_confirmation_read_recovers(
        self,
    ) -> None:
        ticket_input = SimpleNamespace(
            clear_input=AsyncMock(),
            send_keys=AsyncMock(),
        )
        page = SimpleNamespace(
            select=AsyncMock(return_value=ticket_input),
            evaluate=AsyncMock(side_effect=[True, None]),
        )
        client = _client(
            SimpleNamespace(page=page),
            confirmation_checks=2,
            confirmation_interval=0,
        )
        before = LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200)
        after = LotterySnapshot(LotteryKind.WEAPON, 800_000, 1_000)
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]
        client._inspect_current = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                LotteryPageError("private detail\nsecond line"),
                after,
            ]
        )

        with self.assertLogs("hvbrowser.lottery", level="WARNING") as captured:
            report = await client.purchase(
                LotteryKind.WEAPON,
                800,
                expected_before=before,
            )

        self.assertEqual(report.after, after)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("confirmed_attempt=2/2", captured.output[0])
        self.assertIn("error_count=1", captured.output[0])
        self.assertIn("last_error_type=LotteryPageError", captured.output[0])
        self.assertNotIn("private detail", captured.output[0])

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
        client = _client(driver, confirmation_checks=1, confirmation_interval=0)
        before = LotterySnapshot(LotteryKind.ARMOR, 800_000, 100)
        unchanged = LotterySnapshot(LotteryKind.ARMOR, 800_000, 100)
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]
        client._inspect_current = AsyncMock(  # type: ignore[method-assign]
            return_value=unchanged
        )

        with self.assertRaises(LotterySubmissionError):
            await client.purchase(LotteryKind.ARMOR, 800)


if __name__ == "__main__":
    unittest.main()
