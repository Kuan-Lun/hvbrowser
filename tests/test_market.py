import asyncio
import unittest
from collections.abc import Callable
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from hvbrowser.market import (
    ISEKAI_MARKET_CATEGORIES,
    PERSISTENT_MARKET_CATEGORIES,
    MarketCategory,
    MarketClient,
    MarketItem,
    MarketPageError,
    MarketSalePlan,
    MarketSaleReport,
    MarketSaleRequest,
    MarketSubmissionError,
    market_browse_url,
    market_item_url,
    parse_market_item_id,
    parse_market_sell_order_id,
    parse_market_stock,
)
from hvbrowser.realm import Realm
from hvbrowser.runtime import ZendriverOperationTimeout


class _FakeElement:
    def __init__(
        self,
        text: str = "",
        *,
        attrs: dict[str, str] | None = None,
        on_click: Callable[[], None] | None = None,
    ) -> None:
        self.text = text
        self.attrs = attrs or {}
        self.click_count = 0
        self._on_click = on_click

    async def click(self) -> None:
        self.click_count += 1
        if self._on_click:
            self._on_click()


class _FakeRow(_FakeElement):
    def __init__(self, item_id: int, name: str, stock: str) -> None:
        super().__init__(attrs={"onclick": f"select_market_item({item_id})"})
        self._cells = [_FakeElement(name), _FakeElement(stock)]

    async def query_selector_all(self, selector: str) -> list[_FakeElement]:
        if selector != "td":
            raise AssertionError(f"Unexpected row selector: {selector}")
        return self._cells


class _FakeItemList:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    async def query_selector_all(self, selector: str) -> list[_FakeRow]:
        if selector != "table > tbody > tr[onclick]":
            raise AssertionError(f"Unexpected item-list selector: {selector}")
        return self._rows


class _FakePage:
    def __init__(self) -> None:
        self.rows: list[_FakeRow] = []
        self.fail_market_root = False
        self.has_sell_order = True
        self.error_text: str | None = None
        self.evaluated: list[str] = []
        self.waited: list[int] = []
        self.submitted = False
        self.stock_after_submit = "0"
        self.fail_stock_confirmation = False
        self.stock_control = _FakeElement("15")
        self.update_button = _FakeElement(on_click=self._mark_submitted)

    def _mark_submitted(self) -> None:
        self.submitted = True
        self.stock_control.text = self.stock_after_submit

    async def select(self, selector: str, timeout: int) -> object:
        if selector == "#market_itemlist" and timeout == 5:
            if self.fail_market_root:
                raise TimeoutError
            return _FakeItemList(self.rows)
        if selector == "#sell_order_stock_field > span" and timeout == 5:
            if self.submitted and self.fail_stock_confirmation:
                raise TimeoutError
            return self.stock_control
        if selector == "#sellorder_update" and timeout == 5:
            return self.update_button
        if selector == "#messagebox_inner p.messagebox_error" and timeout == 1:
            if self.error_text is None:
                raise TimeoutError
            return _FakeElement(self.error_text)
        raise AssertionError(f"Unexpected page selection: {selector}, {timeout}")

    async def xpath(self, selector: str, timeout: int) -> list[_FakeElement]:
        expected = (
            "//*[@id='market_itemsell']"
            "//td[contains(@onclick, 'autofill_from_sell_order')]"
        )
        if selector != expected or timeout != 5:
            raise AssertionError(f"Unexpected XPath: {selector}, {timeout}")
        if not self.has_sell_order:
            return []
        return [_FakeElement(attrs={"onclick": "autofill_from_sell_order(987, 0, 0)"})]

    async def evaluate(self, expression: str) -> None:
        self.evaluated.append(expression)

    async def wait(self, seconds: int) -> None:
        self.waited.append(seconds)


class _FakeDriver:
    def __init__(
        self,
        rows_by_filter: dict[str, list[_FakeRow]],
        *,
        post_submit_rows_by_filter: dict[str, list[_FakeRow]] | None = None,
    ) -> None:
        self.page = _FakePage()
        self.rows_by_filter = rows_by_filter
        self.post_submit_rows_by_filter = post_submit_rows_by_filter or rows_by_filter
        self.visited: list[str] = []

    async def get(self, url: str) -> None:
        self.visited.append(url)
        query = parse_qs(urlsplit(url).query)
        if "itemid" in query:
            return
        filter_code = query["filter"][0]
        rows = (
            self.post_submit_rows_by_filter
            if self.page.submitted
            else self.rows_by_filter
        )
        self.page.rows = rows.get(filter_code, [])


class _FakeRealmNavigator:
    def __init__(self, realm: Realm) -> None:
        self.realm = realm
        self.current_calls = 0

    async def current(self) -> Realm:
        self.current_calls += 1
        return self.realm


def _client(
    driver: _FakeDriver,
    realm: Realm = Realm.PERSISTENT,
) -> MarketClient:
    return MarketClient(driver, _FakeRealmNavigator(realm))  # type: ignore[arg-type]


class MarketParsingTests(unittest.TestCase):
    def test_market_urls_are_realm_specific(self) -> None:
        self.assertEqual(
            market_browse_url(MarketCategory.MATERIALS, realm=Realm.PERSISTENT),
            "https://hentaiverse.org/?s=Bazaar&ss=mk&screen=browseitems&filter=ma",
        )
        self.assertEqual(
            market_item_url(MarketCategory.MATERIALS, 123, realm=Realm.ISEKAI),
            "https://hentaiverse.org/isekai/"
            "?s=Bazaar&ss=mk&screen=browseitems&filter=ma&itemid=123",
        )

    def test_isekai_rejects_unavailable_category(self) -> None:
        with self.assertRaisesRegex(ValueError, "unavailable in Isekai"):
            market_browse_url(MarketCategory.ARTIFACTS, realm=Realm.ISEKAI)

    def test_parse_item_id_from_supported_actions(self) -> None:
        self.assertEqual(parse_market_item_id("select_market_item(123)"), 123)
        self.assertEqual(
            parse_market_item_id("common.goto_url('?s=Bazaar&ss=mk&itemid=456')"),
            456,
        )

    def test_parse_item_id_fails_closed(self) -> None:
        with self.assertRaises(MarketPageError):
            parse_market_item_id("do_something_without_an_item()")
        with self.assertRaises(MarketPageError):
            parse_market_item_id("unrelated_action(123)")

    def test_parse_stock(self) -> None:
        self.assertEqual(parse_market_stock(""), 0)
        self.assertEqual(parse_market_stock(" 1,234 "), 1234)
        self.assertEqual(parse_market_stock("Stock: 75"), 75)

    def test_parse_stock_fails_closed(self) -> None:
        with self.assertRaises(MarketPageError):
            parse_market_stock("not available")

    def test_parse_sell_order_id(self) -> None:
        self.assertEqual(
            parse_market_sell_order_id("autofill_from_sell_order(1234, 0, 0)"),
            1234,
        )
        with self.assertRaises(MarketPageError):
            parse_market_sell_order_id("unrelated_action()")


class MarketClientTests(unittest.IsolatedAsyncioTestCase):
    async def _submit_with_verified_live_semantics(
        self, client: MarketClient, plan: MarketSalePlan
    ) -> MarketSaleReport:
        with patch("hvbrowser.market._MARKET_SUBMISSION_VERIFIED", True):
            return await client.submit_sales(plan)

    async def test_inspect_persistent_market_is_read_only_and_scoped(self) -> None:
        driver = _FakeDriver(
            {
                "co": [_FakeRow(101, "Health Draught", "15")],
                "ma": [_FakeRow(202, "Low-Grade Metals", "1,200")],
            }
        )

        snapshot = await _client(driver).inspect()

        self.assertIs(snapshot.realm, Realm.PERSISTENT)
        self.assertEqual(
            driver.visited,
            [
                market_browse_url(category, realm=Realm.PERSISTENT)
                for category in PERSISTENT_MARKET_CATEGORIES
            ],
        )
        self.assertEqual(len(snapshot.items), 2)
        self.assertEqual(snapshot.items[0].item_id, 101)
        self.assertEqual(snapshot.items[1].stock, 1200)
        self.assertEqual(
            snapshot.items_in(MarketCategory.CONSUMABLES),
            (snapshot.items[0],),
        )

    async def test_inspect_isekai_uses_only_available_categories(self) -> None:
        driver = _FakeDriver({})

        snapshot = await _client(driver, Realm.ISEKAI).inspect()

        self.assertIs(snapshot.realm, Realm.ISEKAI)
        self.assertEqual(
            driver.visited,
            [
                market_browse_url(category, realm=Realm.ISEKAI)
                for category in ISEKAI_MARKET_CATEGORIES
            ],
        )
        self.assertTrue(
            all(
                url.startswith("https://hentaiverse.org/isekai/")
                for url in driver.visited
            )
        )

    async def test_missing_market_root_fails_closed(self) -> None:
        driver = _FakeDriver({})
        driver.page.fail_market_root = True

        with self.assertRaisesRegex(MarketPageError, "Market item list is missing"):
            await _client(driver).inspect()

    async def test_plan_sales_is_read_only_and_ignores_empty_stock(self) -> None:
        driver = _FakeDriver(
            {
                "co": [
                    _FakeRow(101, "Health Draught", "15"),
                    _FakeRow(102, "Mana Draught", ""),
                ]
            }
        )

        plan = await _client(driver).plan_sales(
            MarketSaleRequest(
                consumables=("health draught", "Mana Draught"),
            )
        )

        self.assertIs(plan.realm, Realm.PERSISTENT)
        self.assertEqual([item.item_id for item in plan.items], [101])
        self.assertEqual(plan.total_units, 15)
        self.assertEqual(driver.page.stock_control.click_count, 0)
        self.assertEqual(driver.page.update_button.click_count, 0)

    async def test_validate_sale_forms_does_not_click_or_evaluate(self) -> None:
        driver = _FakeDriver({})
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)
        plan = MarketSalePlan(realm=Realm.PERSISTENT, items=(item,))

        await _client(driver).validate_sale_forms(plan)

        self.assertEqual(
            driver.visited,
            [
                market_item_url(
                    MarketCategory.CONSUMABLES,
                    101,
                    realm=Realm.PERSISTENT,
                )
            ],
        )
        self.assertEqual(driver.page.stock_control.click_count, 0)
        self.assertEqual(driver.page.update_button.click_count, 0)
        self.assertEqual(driver.page.evaluated, [])

    async def test_validate_requires_existing_order_for_pricing(self) -> None:
        driver = _FakeDriver({})
        driver.page.has_sell_order = False
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with self.assertRaisesRegex(MarketPageError, "no existing sell order"):
            await _client(driver).validate_sale_forms(
                MarketSalePlan(realm=Realm.PERSISTENT, items=(item,))
            )

    async def test_submit_uses_verified_selectors_and_confirms_stock(self) -> None:
        driver = _FakeDriver(
            {},
            post_submit_rows_by_filter={"co": [_FakeRow(101, "Health Draught", "0")]},
        )
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        report = await self._submit_with_verified_live_semantics(
            _client(driver), MarketSalePlan(realm=Realm.PERSISTENT, items=(item,))
        )

        self.assertEqual(report.sales[0].remaining_stock, 0)
        self.assertEqual(
            driver.page.evaluated,
            ["autofill_from_sell_order(987,0,0);"],
        )
        self.assertEqual(driver.page.stock_control.click_count, 1)
        self.assertEqual(driver.page.update_button.click_count, 1)
        self.assertEqual(driver.page.waited, [1])

    async def test_submit_surfaces_market_error(self) -> None:
        driver = _FakeDriver({})
        driver.page.error_text = "Insufficient stock"
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with self.assertRaisesRegex(MarketSubmissionError, "Insufficient stock"):
            await self._submit_with_verified_live_semantics(
                _client(driver),
                MarketSalePlan(realm=Realm.PERSISTENT, items=(item,)),
            )

    async def test_submit_click_hang_is_terminal_without_stock_probe(self) -> None:
        release = asyncio.Event()

        async def hang() -> None:
            await release.wait()

        driver = _FakeDriver({})
        driver.page.update_button.click = hang  # type: ignore[method-assign]
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with (
            patch("hvbrowser.market._MUTATION_TIMEOUT_SECONDS", 0.01),
            self.assertRaises(ZendriverOperationTimeout) as raised,
        ):
            await self._submit_with_verified_live_semantics(
                _client(driver),
                MarketSalePlan(realm=Realm.PERSISTENT, items=(item,)),
            )

        self.assertEqual(raised.exception.timeout_seconds, 0.01)
        self.assertEqual(
            driver.visited,
            [market_item_url(item.category, item.item_id, realm=Realm.PERSISTENT)],
        )
        self.assertEqual(driver.page.waited, [])
        self.assertFalse(driver.page.submitted)
        release.set()
        await asyncio.sleep(0)

    async def test_submit_rejects_unchanged_stock(self) -> None:
        driver = _FakeDriver({})
        driver.page.stock_after_submit = "15"
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with self.assertRaisesRegex(MarketSubmissionError, "did not sell all"):
            await self._submit_with_verified_live_semantics(
                _client(driver),
                MarketSalePlan(realm=Realm.PERSISTENT, items=(item,)),
            )

    async def test_submit_rejects_missing_post_submit_item(self) -> None:
        driver = _FakeDriver({})
        driver.page.fail_stock_confirmation = True
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with self.assertRaisesRegex(MarketSubmissionError, "confirmation is missing"):
            await self._submit_with_verified_live_semantics(
                _client(driver),
                MarketSalePlan(realm=Realm.PERSISTENT, items=(item,)),
            )

    async def test_submit_rejects_blank_stock_confirmation(self) -> None:
        driver = _FakeDriver({})
        driver.page.stock_after_submit = ""
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with self.assertRaisesRegex(MarketSubmissionError, "confirmation is blank"):
            await self._submit_with_verified_live_semantics(
                _client(driver),
                MarketSalePlan(realm=Realm.PERSISTENT, items=(item,)),
            )

    async def test_submit_rejects_stale_plan_before_click(self) -> None:
        driver = _FakeDriver({})
        driver.page.stock_control.text = "14"
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with self.assertRaisesRegex(MarketSubmissionError, "plan is stale"):
            await self._submit_with_verified_live_semantics(
                _client(driver),
                MarketSalePlan(realm=Realm.PERSISTENT, items=(item,)),
            )

        self.assertEqual(driver.page.stock_control.click_count, 0)
        self.assertEqual(driver.page.update_button.click_count, 0)

    async def test_public_submission_is_disabled_before_live_verification(
        self,
    ) -> None:
        driver = _FakeDriver({})
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)

        with self.assertRaisesRegex(MarketSubmissionError, "submission is disabled"):
            await _client(driver).submit_sales(
                MarketSalePlan(realm=Realm.PERSISTENT, items=(item,))
            )

        self.assertEqual(driver.visited, [])
        self.assertEqual(driver.page.evaluated, [])
        self.assertEqual(driver.page.stock_control.click_count, 0)
        self.assertEqual(driver.page.update_button.click_count, 0)


if __name__ == "__main__":
    unittest.main()
