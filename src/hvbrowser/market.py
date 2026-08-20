import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .realm import Realm, RealmNavigator
from .runtime import ZendriverOperationTimeout, wait_for_zendriver
from .urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL

MARKET_ROOT_URL = f"{HENTAIVERSE_ROOT_URL}/"
ISEKAI_MARKET_ROOT_URL = HENTAIVERSE_ISEKAI_ROOT_URL

_READ_TIMEOUT_SECONDS = 8.0
_MUTATION_TIMEOUT_SECONDS = 15.0
_SELECTOR_INNER_TIMEOUT_SECONDS = 5.0
_SELECTOR_OUTER_TIMEOUT_SECONDS = 7.0
_SHORT_SELECTOR_INNER_TIMEOUT_SECONDS = 1.0
_SHORT_SELECTOR_OUTER_TIMEOUT_SECONDS = 3.0


class MarketCategory(StrEnum):
    CONSUMABLES = "co"
    MATERIALS = "ma"
    TROPHIES = "tr"
    ARTIFACTS = "ar"
    FIGURES = "fi"
    MONSTER_ITEMS = "mo"


_CATEGORY_LABELS: dict[MarketCategory, str] = {
    MarketCategory.CONSUMABLES: "Consumables",
    MarketCategory.MATERIALS: "Materials",
    MarketCategory.TROPHIES: "Trophies",
    MarketCategory.ARTIFACTS: "Artifacts",
    MarketCategory.FIGURES: "Figures",
    MarketCategory.MONSTER_ITEMS: "Monster Items",
}

PERSISTENT_MARKET_CATEGORIES = tuple(MarketCategory)
ISEKAI_MARKET_CATEGORIES = (
    MarketCategory.CONSUMABLES,
    MarketCategory.MATERIALS,
    MarketCategory.TROPHIES,
)

# The selectors and stock-confirmation path are covered offline, but the live
# meaning of the selected sell-side quote has not yet been confirmed against
# the current authenticated Market DOM.  Keep every public submission entry
# point fail-closed until that read-only verification has happened.
_MARKET_SUBMISSION_VERIFIED = False


@dataclass(frozen=True)
class MarketItem:
    category: MarketCategory
    item_id: int
    name: str
    stock: int


@dataclass(frozen=True)
class MarketSnapshot:
    realm: Realm
    items: tuple[MarketItem, ...]

    def items_in(self, category: MarketCategory) -> tuple[MarketItem, ...]:
        return tuple(item for item in self.items if item.category is category)


@dataclass(frozen=True)
class MarketSalePlan:
    """A read-only snapshot of inventory selected for sale."""

    realm: Realm
    items: tuple[MarketItem, ...]

    @property
    def total_units(self) -> int:
        return sum(item.stock for item in self.items)


@dataclass(frozen=True)
class MarketSaleQuote:
    """Read-only pricing evidence from the first visible sell-side order."""

    item: MarketItem
    sell_order_id: int
    order_text: str
    current_stock: int


@dataclass(frozen=True)
class MarketSale:
    item: MarketItem
    remaining_stock: int


@dataclass(frozen=True)
class MarketSaleReport:
    realm: Realm
    sales: tuple[MarketSale, ...]


@dataclass(frozen=True, slots=True)
class MarketSaleRequest:
    """Item names selected for a read-only Market sale plan."""

    consumables: tuple[str, ...] = ()
    materials: tuple[str, ...] = ()
    trophies: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    figures: tuple[str, ...] = ()
    monster_items: tuple[str, ...] = ()

    def names_by_category(self) -> dict[MarketCategory, tuple[str, ...]]:
        return {
            MarketCategory.CONSUMABLES: self.consumables,
            MarketCategory.MATERIALS: self.materials,
            MarketCategory.TROPHIES: self.trophies,
            MarketCategory.ARTIFACTS: self.artifacts,
            MarketCategory.FIGURES: self.figures,
            MarketCategory.MONSTER_ITEMS: self.monster_items,
        }


class MarketPageError(RuntimeError):
    """The Market page did not expose the expected read-only structure."""


class MarketSubmissionError(RuntimeError):
    """The Market rejected a sale or did not confirm the inventory change."""


class _MarketDriver(Protocol):
    page: Any

    async def get(self, url: str) -> None: ...


@dataclass(frozen=True)
class _SaleForm:
    sell_order_id: int
    order_text: str
    current_stock: int
    stock_control: Any
    update_button: Any


def market_browse_url(category: MarketCategory, *, realm: Realm) -> str:
    if not isinstance(realm, Realm):
        raise TypeError("realm must be a Realm")
    if realm is Realm.ISEKAI and category not in ISEKAI_MARKET_CATEGORIES:
        raise ValueError(f"{market_category_label(category)} is unavailable in Isekai")
    root = ISEKAI_MARKET_ROOT_URL if realm is Realm.ISEKAI else MARKET_ROOT_URL
    return f"{root}?s=Bazaar&ss=mk&screen=browseitems&filter={category.value}"


def market_item_url(category: MarketCategory, item_id: int, *, realm: Realm) -> str:
    return f"{market_browse_url(category, realm=realm)}&itemid={item_id}"


def market_category_label(category: MarketCategory) -> str:
    return _CATEGORY_LABELS[category]


def parse_market_item_id(onclick: str) -> int:
    patterns = (
        r"[?&]itemid=(\d+)",
        r"\bitemid\D+(\d+)",
        r"\bselect_market_item\(\s*(\d+)\s*\)",
    )
    for pattern in patterns:
        match = re.search(pattern, onclick)
        if match:
            return int(match.group(1))
    raise MarketPageError(
        f"Unable to parse Market item id from row action: {onclick!r}"
    )


def parse_market_stock(text: str) -> int:
    normalized = text.strip().replace(",", "")
    if not normalized:
        return 0
    match = re.search(r"\d+", normalized)
    if not match:
        raise MarketPageError(f"Unable to parse Market stock value: {text!r}")
    return int(match.group(0))


def parse_market_sell_order_id(onclick: str) -> int:
    match = re.search(
        r"\bautofill_from_sell_order\(\s*(\d+)\s*,\s*0\s*,\s*0\s*\)",
        onclick,
    )
    if not match:
        raise MarketPageError(
            f"Unable to parse Market sell-order id from action: {onclick!r}"
        )
    return int(match.group(1))


class MarketClient:
    """Market inspection, explicit sale planning, and verified submission."""

    def __init__(self, driver: _MarketDriver, realm: RealmNavigator) -> None:
        self._driver = driver
        self._realm = realm

    async def inspect(self) -> MarketSnapshot:
        realm = await self._realm.current()
        categories = (
            ISEKAI_MARKET_CATEGORIES
            if realm is Realm.ISEKAI
            else PERSISTENT_MARKET_CATEGORIES
        )
        items: list[MarketItem] = []
        for category in categories:
            items.extend(await self._inspect_category(category, realm=realm))
        return MarketSnapshot(realm=realm, items=tuple(items))

    async def plan_sales(
        self,
        request: MarketSaleRequest,
    ) -> MarketSalePlan:
        """Select stocked inventory without changing Market state."""
        if not isinstance(request, MarketSaleRequest):
            raise TypeError("request must be a MarketSaleRequest")
        snapshot = await self.inspect()
        normalized_requests = {
            category: {name.casefold() for name in names}
            for category, names in request.names_by_category().items()
        }
        selected = tuple(
            item
            for item in snapshot.items
            if item.stock > 0
            and item.name.casefold() in normalized_requests.get(item.category, set())
        )
        return MarketSalePlan(realm=snapshot.realm, items=selected)

    async def validate_sale_forms(self, plan: MarketSalePlan) -> None:
        """Verify every planned item form without clicking or submitting it."""
        for item in plan.items:
            await self.inspect_sale_quote(item, realm=plan.realm)

    async def inspect_sale_quote(
        self, item: MarketItem, *, realm: Realm
    ) -> MarketSaleQuote:
        """Inspect the current sell-side pricing source without clicking it."""
        sale_form = await self._open_sale_form(item, realm=realm)
        return MarketSaleQuote(
            item=item,
            sell_order_id=sale_form.sell_order_id,
            order_text=sale_form.order_text,
            current_stock=sale_form.current_stock,
        )

    async def submit_sales(self, plan: MarketSalePlan) -> MarketSaleReport:
        """Submit an explicitly prepared plan and verify each stock change.

        This operation never deposits credits and never touches existing sell
        orders that are not present in ``plan``.
        """
        if not _MARKET_SUBMISSION_VERIFIED:
            raise MarketSubmissionError(
                "Market submission is disabled until the current live quote "
                "and pricing semantics have been verified read-only"
            )

        return await self._submit_verified_sales(plan)

    async def _submit_verified_sales(self, plan: MarketSalePlan) -> MarketSaleReport:
        """Execute the guarded submission algorithm after live verification."""
        sales: list[MarketSale] = []
        for item in plan.items:
            sale_form = await self._open_sale_form(item, realm=plan.realm)
            if sale_form.current_stock != item.stock:
                raise MarketSubmissionError(
                    f"Market sale plan is stale for {item.name!r}: "
                    f"planned={item.stock}, current={sale_form.current_stock}"
                )
            await wait_for_zendriver(
                self._driver.page.evaluate(
                    f"autofill_from_sell_order({sale_form.sell_order_id},0,0);"
                ),
                timeout=_MUTATION_TIMEOUT_SECONDS,
                owner=self._driver.page,
            )
            await wait_for_zendriver(
                sale_form.stock_control.click(),
                timeout=_MUTATION_TIMEOUT_SECONDS,
                owner=sale_form.stock_control,
            )
            await wait_for_zendriver(
                sale_form.update_button.click(),
                timeout=_MUTATION_TIMEOUT_SECONDS,
                owner=sale_form.update_button,
            )
            await wait_for_zendriver(
                self._driver.page.wait(1),
                timeout=_READ_TIMEOUT_SECONDS,
                owner=self._driver.page,
            )
            await self._raise_for_submission_error(item)

            remaining_stock = await self._read_item_stock(item, realm=plan.realm)
            if remaining_stock != 0:
                raise MarketSubmissionError(
                    f"Market did not sell all planned stock for {item.name!r}: "
                    f"before={item.stock}, after={remaining_stock}"
                )
            sales.append(MarketSale(item=item, remaining_stock=remaining_stock))
        return MarketSaleReport(realm=plan.realm, sales=tuple(sales))

    async def _open_sale_form(self, item: MarketItem, *, realm: Realm) -> _SaleForm:
        await self._driver.get(
            market_item_url(item.category, item.item_id, realm=realm)
        )
        try:
            sell_order_cells = await wait_for_zendriver(
                self._driver.page.xpath(
                    "//*[@id='market_itemsell']"
                    "//td[contains(@onclick, 'autofill_from_sell_order')]",
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self._driver.page,
            )
            stock_control = await wait_for_zendriver(
                self._driver.page.select(
                    "#sell_order_stock_field > span",
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self._driver.page,
            )
            update_button = await wait_for_zendriver(
                self._driver.page.select(
                    "#sellorder_update",
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self._driver.page,
            )
        except ZendriverOperationTimeout:
            raise
        except TimeoutError as error:
            raise MarketPageError(
                f"Market sale form is missing for {item.name!r}"
            ) from error
        if not sell_order_cells:
            raise MarketPageError(
                f"Market has no existing sell order to price {item.name!r}"
            )
        onclick = str(sell_order_cells[0].attrs.get("onclick", ""))
        return _SaleForm(
            sell_order_id=parse_market_sell_order_id(onclick),
            order_text=sell_order_cells[0].text.strip(),
            current_stock=parse_market_stock(stock_control.text),
            stock_control=stock_control,
            update_button=update_button,
        )

    async def _read_item_stock(self, item: MarketItem, *, realm: Realm) -> int:
        await self._driver.get(
            market_item_url(item.category, item.item_id, realm=realm)
        )
        try:
            stock_control = await wait_for_zendriver(
                self._driver.page.select(
                    "#sell_order_stock_field > span",
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self._driver.page,
            )
        except ZendriverOperationTimeout:
            raise
        except TimeoutError as error:
            raise MarketSubmissionError(
                f"Market stock confirmation is missing for {item.name!r}"
            ) from error
        stock_text = stock_control.text
        if not stock_text.strip():
            raise MarketSubmissionError(
                f"Market stock confirmation is blank for {item.name!r}"
            )
        try:
            return parse_market_stock(stock_text)
        except MarketPageError as error:
            raise MarketSubmissionError(
                f"Market stock confirmation is invalid for {item.name!r}"
            ) from error

    async def _raise_for_submission_error(self, item: MarketItem) -> None:
        try:
            error_message = await wait_for_zendriver(
                self._driver.page.select(
                    "#messagebox_inner p.messagebox_error",
                    timeout=_SHORT_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SHORT_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self._driver.page,
            )
        except ZendriverOperationTimeout:
            raise
        except TimeoutError:
            return
        message = error_message.text.strip() or "unknown Market error"
        raise MarketSubmissionError(f"Market rejected {item.name!r}: {message}")

    async def _inspect_category(
        self, category: MarketCategory, *, realm: Realm
    ) -> list[MarketItem]:
        await self._driver.get(market_browse_url(category, realm=realm))
        try:
            item_list = await wait_for_zendriver(
                self._driver.page.select(
                    "#market_itemlist",
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self._driver.page,
            )
        except ZendriverOperationTimeout:
            raise
        except TimeoutError as error:
            raise MarketPageError(
                f"Market item list is missing for {market_category_label(category)}"
            ) from error

        rows = await wait_for_zendriver(
            item_list.query_selector_all("table > tbody > tr[onclick]"),
            timeout=_READ_TIMEOUT_SECONDS,
            owner=item_list,
        )
        items: list[MarketItem] = []
        for row in rows:
            cells = await wait_for_zendriver(
                row.query_selector_all("td"),
                timeout=_READ_TIMEOUT_SECONDS,
                owner=row,
            )
            if len(cells) < 2:
                raise MarketPageError(
                    f"Market row has fewer than two cells in "
                    f"{market_category_label(category)}"
                )
            name = cells[0].text.strip()
            onclick = str(row.attrs.get("onclick", ""))
            items.append(
                MarketItem(
                    category=category,
                    item_id=parse_market_item_id(onclick),
                    name=name,
                    stock=parse_market_stock(cells[1].text),
                )
            )
        return items
