import unittest
from collections.abc import Callable
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from hbrowser import BrowserMutationOutcomeUnknownError

from hvbrowser import (
    MarketCategory,
    MarketClient,
    MarketItem,
    MarketPageError,
    MarketSalePlan,
    MarketSaleRequest,
    MarketSubmissionError,
)
from hvbrowser.market import (
    _MarketSaleState,
    market_browse_url,
    parse_market_item_id,
    parse_market_sell_order_id,
    parse_market_stock,
)
from hvbrowser.realm import Realm
from hvbrowser.runtime import PageStateTimeout


class _Element:
    def __init__(self, action: Callable[[], None] | None = None) -> None:
        self._action = action
        self.clicks = 0

    async def click(self) -> None:
        self.clicks += 1
        if self._action is not None:
            self._action()


class _Page:
    def __init__(self) -> None:
        self.current_url = ""
        self.category_rows: dict[MarketCategory, list[dict[str, object]]] = {
            category: [] for category in MarketCategory
        }
        self.stock: int | str = 15
        self.order_onclick = "autofill_from_sell_order(987,0,0)"
        self.order_text = "100 C"
        self.error: str | None = None
        self.has_stock_control = True
        self.has_update_button = True
        self.keep_after_submit = False
        self.update_error: Exception | None = None
        self.stock_control = _Element()
        self.update_button = _Element(self._update)
        self.expressions: list[str] = []

    def _update(self) -> None:
        if self.update_error is not None:
            raise self.update_error
        if not self.keep_after_submit:
            self.stock = 0

    def sale_state(self) -> dict[str, object]:
        return {
            "sellOrders": [{"onclick": self.order_onclick, "text": self.order_text}],
            "hasStockControl": self.has_stock_control,
            "stockText": str(self.stock),
            "hasUpdateButton": self.has_update_button,
            "errorText": self.error,
        }

    async def evaluate(self, expression: str) -> object:
        self.expressions.append(expression)
        if "hasItemList" in expression:
            query = parse_qs(urlsplit(self.current_url).query)
            category = MarketCategory(query["filter"][0])
            return {
                "hasItemList": True,
                "rows": self.category_rows[category],
            }
        if "sellOrders" in expression:
            return self.sale_state()
        if expression.startswith("autofill_from_sell_order"):
            return None
        raise AssertionError(f"unexpected expression: {expression}")

    async def query_selector(self, selector: str) -> _Element | None:
        if selector == "#sell_order_stock_field > span":
            return self.stock_control
        if selector == "#sellorder_update":
            return self.update_button
        raise AssertionError(f"unexpected selector: {selector}")


class _Driver:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.get_calls: list[str] = []

    async def get(self, url: str) -> None:
        self.get_calls.append(url)
        self.page.current_url = url


def _client(page: _Page, realm: Realm = Realm.PERSISTENT) -> MarketClient:
    navigator = type(
        "RealmNavigator",
        (),
        {"current": AsyncMock(return_value=realm)},
    )()
    return MarketClient(_Driver(page), navigator)  # type: ignore[arg-type]


class MarketClientTests(unittest.IsolatedAsyncioTestCase):
    def test_market_identifier_and_stock_parsers_fail_closed(self) -> None:
        self.assertEqual(parse_market_item_id("select_market_item(123)"), 123)
        self.assertEqual(parse_market_item_id("?itemid=456"), 456)
        self.assertEqual(
            parse_market_sell_order_id("autofill_from_sell_order(987,0,0)"),
            987,
        )
        self.assertEqual(parse_market_stock("1,234"), 1_234)
        for parser, value in (
            (parse_market_item_id, "unknown"),
            (parse_market_sell_order_id, "unknown"),
            (parse_market_stock, "unknown"),
        ):
            with self.subTest(parser=parser.__name__):
                with self.assertRaises(MarketPageError):
                    parser(value)

    def test_isekai_rejects_persistent_only_category(self) -> None:
        with self.assertRaises(ValueError):
            market_browse_url(MarketCategory.ARTIFACTS, realm=Realm.ISEKAI)

    async def test_inspect_uses_one_atomic_snapshot_per_category(self) -> None:
        page = _Page()
        page.category_rows[MarketCategory.CONSUMABLES] = [
            {
                "onclick": "select_market_item(101)",
                "cells": ["Health Draught", "1,234"],
            }
        ]

        snapshot = await _client(page).inspect()

        self.assertEqual(len(snapshot.items), 1)
        self.assertEqual(snapshot.items[0].stock, 1_234)
        category_reads = [
            script for script in page.expressions if "hasItemList" in script
        ]
        self.assertEqual(len(category_reads), len(MarketCategory))

    async def test_isekai_skips_unavailable_categories(self) -> None:
        page = _Page()

        await _client(page, Realm.ISEKAI).inspect()

        self.assertEqual(len(page.expressions), 3)

    async def test_invalid_category_payload_fails_closed(self) -> None:
        page = _Page()
        page.evaluate = AsyncMock(return_value={"hasItemList": False, "rows": []})

        with self.assertRaises(MarketPageError):
            await _client(page).inspect()

    async def test_malformed_category_row_fails_closed(self) -> None:
        page = _Page()
        page.category_rows[MarketCategory.CONSUMABLES] = [
            {"onclick": "?itemid=1", "cells": ["only one cell"]}
        ]

        with self.assertRaisesRegex(MarketPageError, "fewer than two"):
            await _client(page).inspect()

    async def test_plan_sales_is_read_only_and_ignores_empty_stock(self) -> None:
        page = _Page()
        page.category_rows[MarketCategory.CONSUMABLES] = [
            {"onclick": "?itemid=1", "cells": ["Health Draught", "3"]},
            {"onclick": "?itemid=2", "cells": ["Mana Draught", "0"]},
        ]

        plan = await _client(page).plan_sales(
            MarketSaleRequest(consumables=("Health Draught", "Mana Draught"))
        )

        self.assertEqual([item.item_id for item in plan.items], [1])
        self.assertEqual(page.update_button.clicks, 0)

    async def test_quote_uses_atomic_form_state_without_clicking(self) -> None:
        page = _Page()
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        quote = await _client(page).inspect_sale_quote(
            item,
            realm=Realm.PERSISTENT,
        )

        self.assertEqual(quote.sell_order_id, 987)
        self.assertEqual(quote.current_stock, 15)
        self.assertEqual(page.stock_control.clicks, 0)
        self.assertEqual(page.update_button.clicks, 0)

    async def test_quote_requires_order_controls_and_nonblank_stock(self) -> None:
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)
        cases: tuple[tuple[str, object], ...] = (
            ("order_onclick", ""),
            ("has_stock_control", False),
            ("has_update_button", False),
            ("stock", ""),
        )
        for name, value in cases:
            page = _Page()
            setattr(page, name, value)
            with self.subTest(name=name):
                with self.assertRaises(MarketPageError):
                    await _client(page).inspect_sale_quote(
                        item,
                        realm=Realm.PERSISTENT,
                    )

    async def test_public_submission_remains_fail_closed(self) -> None:
        page = _Page()
        plan = MarketSalePlan(Realm.PERSISTENT, ())

        with self.assertRaisesRegex(MarketSubmissionError, "disabled"):
            await _client(page).submit_sales(plan)

    async def test_verified_submission_is_once_and_confirms_zero(self) -> None:
        page = _Page()
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)
        plan = MarketSalePlan(Realm.PERSISTENT, (item,))

        report = await _client(page)._submit_verified_sales(plan)

        self.assertEqual(report.sales[0].remaining_stock, 0)
        self.assertEqual(page.stock_control.clicks, 1)
        self.assertEqual(page.update_button.clicks, 1)
        self.assertEqual(
            len(
                [script for script in page.expressions if script.startswith("autofill")]
            ),
            1,
        )

    async def test_stale_plan_stops_before_mutation(self) -> None:
        page = _Page()
        page.stock = 14
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with self.assertRaisesRegex(MarketSubmissionError, "stale"):
            await _client(page)._submit_verified_sales(
                MarketSalePlan(Realm.PERSISTENT, (item,))
            )

        self.assertEqual(page.update_button.clicks, 0)

    async def test_server_error_is_typed_without_replay(self) -> None:
        page = _Page()
        page.error = "Insufficient stock"
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with self.assertRaisesRegex(MarketSubmissionError, "Insufficient stock"):
            await _client(page)._submit_verified_sales(
                MarketSalePlan(Realm.PERSISTENT, (item,))
            )

        self.assertEqual(page.update_button.clicks, 1)

    async def test_update_failure_is_generation_terminal_without_probe(self) -> None:
        page = _Page()
        page.update_error = RuntimeError("disconnected")
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with self.assertRaises(BrowserMutationOutcomeUnknownError):
            await _client(page)._submit_verified_sales(
                MarketSalePlan(Realm.PERSISTENT, (item,))
            )

        sale_state_reads = [
            script for script in page.expressions if "sellOrders" in script
        ]
        self.assertEqual(len(sale_state_reads), 1)
        self.assertEqual(page.update_button.clicks, 1)

    async def test_delayed_success_does_not_replay_submission(self) -> None:
        page = _Page()
        page.keep_after_submit = True
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)
        confirmed = _MarketSaleState((), True, "0", True, None)

        with patch(
            "hvbrowser.market.wait_for_page_state",
            new=AsyncMock(return_value=confirmed),
        ):
            report = await _client(page)._submit_verified_sales(
                MarketSalePlan(Realm.PERSISTENT, (item,))
            )

        self.assertEqual(report.sales[0].remaining_stock, 0)
        self.assertEqual(page.update_button.clicks, 1)

    async def test_semantic_timeout_is_unknown_without_replay(self) -> None:
        page = _Page()
        page.keep_after_submit = True
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with (
            patch(
                "hvbrowser.market.wait_for_page_state",
                new=AsyncMock(side_effect=PageStateTimeout("unchanged")),
            ),
            self.assertRaisesRegex(MarketSubmissionError, "Unable to confirm"),
        ):
            await _client(page)._submit_verified_sales(
                MarketSalePlan(Realm.PERSISTENT, (item,))
            )

        self.assertEqual(page.update_button.clicks, 1)


if __name__ == "__main__":
    unittest.main()
