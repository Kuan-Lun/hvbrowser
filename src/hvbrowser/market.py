import re
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any, Protocol, cast

from .realm import Realm, RealmNavigator
from .runtime import (
    SERVER_STATE_RECEIPT_TIMEOUT_SECONDS,
    Deadline,
    PageStateTimeout,
    evaluate_page,
    invoke_mutation,
    is_browser_generation_error,
    query_page,
    wait_for_page_state,
)
from .urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL

MARKET_ROOT_URL = f"{HENTAIVERSE_ROOT_URL}/"
ISEKAI_MARKET_ROOT_URL = HENTAIVERSE_ISEKAI_ROOT_URL

_MARKET_CATEGORY_STATE_SCRIPT = r"""
(() => {
    const root = document.getElementById("market_itemlist");
    return {
        hasItemList: Boolean(root),
        rows: root ? Array.from(
            root.querySelectorAll("table > tbody > tr[onclick]")
        ).map((row) => ({
            onclick: row.getAttribute("onclick") || "",
            cells: Array.from(row.querySelectorAll("td"), (cell) =>
                cell.textContent || ""
            ),
        })) : [],
    };
})()
"""
_MARKET_SALE_STATE_SCRIPT = r"""
(() => {
    const stock = document.querySelector("#sell_order_stock_field > span");
    const error = document.querySelector("#messagebox_inner p.messagebox_error");
    return {
        sellOrders: Array.from(document.querySelectorAll(
            '#market_itemsell td[onclick*="autofill_from_sell_order"]'
        ), (cell) => ({
            onclick: cell.getAttribute("onclick") || "",
            text: cell.textContent || "",
        })),
        hasStockControl: Boolean(stock),
        stockText: stock ? stock.textContent : null,
        hasUpdateButton: Boolean(document.getElementById("sellorder_update")),
        errorText: error ? error.textContent : null,
    };
})()
"""


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


@dataclass(frozen=True, slots=True)
class _MarketCategoryRow:
    onclick: str
    cells: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _MarketSaleState:
    sell_orders: tuple[tuple[str, str], ...]
    has_stock_control: bool
    stock_text: str | None
    has_update_button: bool
    error_text: str | None


def _decode_market_category(raw: object) -> tuple[_MarketCategoryRow, ...]:
    if not isinstance(raw, dict):
        raise MarketPageError("Market category state is invalid")
    payload = cast(dict[object, object], raw)
    if payload.get("hasItemList") is not True:
        raise MarketPageError("Market item list is missing")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise MarketPageError("Market item rows are invalid")
    decoded: list[_MarketCategoryRow] = []
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise MarketPageError("Market item row is invalid")
        row = cast(dict[object, object], raw_row)
        onclick = row.get("onclick")
        cells = row.get("cells")
        if (
            not isinstance(onclick, str)
            or not isinstance(cells, list)
            or not all(isinstance(cell, str) for cell in cells)
        ):
            raise MarketPageError("Market item row is invalid")
        decoded.append(_MarketCategoryRow(onclick, tuple(cells)))
    return tuple(decoded)


def _decode_market_sale_state(raw: object) -> _MarketSaleState:
    if not isinstance(raw, dict):
        raise MarketPageError("Market sale state is invalid")
    payload = cast(dict[object, object], raw)
    raw_orders = payload.get("sellOrders")
    has_stock_control = payload.get("hasStockControl")
    stock_text = payload.get("stockText")
    has_update_button = payload.get("hasUpdateButton")
    error_text = payload.get("errorText")
    if (
        not isinstance(raw_orders, list)
        or type(has_stock_control) is not bool
        or (stock_text is not None and not isinstance(stock_text, str))
        or type(has_update_button) is not bool
        or (error_text is not None and not isinstance(error_text, str))
    ):
        raise MarketPageError("Market sale state is invalid")
    orders: list[tuple[str, str]] = []
    for raw_order in raw_orders:
        if not isinstance(raw_order, dict):
            raise MarketPageError("Market sell order is invalid")
        order = cast(dict[object, object], raw_order)
        onclick = order.get("onclick")
        text = order.get("text")
        if not isinstance(onclick, str) or not isinstance(text, str):
            raise MarketPageError("Market sell order is invalid")
        orders.append((onclick, text))
    return _MarketSaleState(
        tuple(orders),
        has_stock_control,
        stock_text,
        has_update_button,
        error_text,
    )


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
        state = await self._open_sale_state(item, realm=realm)
        sell_order_id, order_text, current_stock = self._sale_details(item, state)
        return MarketSaleQuote(
            item=item,
            sell_order_id=sell_order_id,
            order_text=order_text,
            current_stock=current_stock,
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
            sell_order_id = sale_form.sell_order_id
            deadline = Deadline.after(SERVER_STATE_RECEIPT_TIMEOUT_SECONDS)
            try:
                await invoke_mutation(
                    partial(
                        self._driver.page.evaluate,
                        f"autofill_from_sell_order({sell_order_id},0,0);",
                    ),
                    owner=self._driver.page,
                    operation=f"Market autofill for {item.name}",
                    deadline=deadline,
                )
                await invoke_mutation(
                    sale_form.stock_control.click,
                    owner=sale_form.stock_control,
                    operation=f"Market stock selection for {item.name}",
                    deadline=deadline,
                )
                await invoke_mutation(
                    sale_form.update_button.click,
                    owner=sale_form.update_button,
                    operation=f"Market sale submission for {item.name}",
                    deadline=deadline,
                )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                raise MarketSubmissionError(
                    f"Market sale outcome is unknown for {item.name!r}"
                ) from error
            try:
                state = await wait_for_page_state(
                    self._driver.page,
                    snapshot_expression=_MARKET_SALE_STATE_SCRIPT,
                    decode=_decode_market_sale_state,
                    accept=lambda current: (
                        current.error_text is not None
                        or self._sale_state_stock_is_zero(current)
                    ),
                    deadline=deadline,
                    description=f"Market sale result for {item.name}",
                )
            except (PageStateTimeout, MarketPageError) as error:
                raise MarketSubmissionError(
                    f"Unable to confirm Market sale for {item.name!r}"
                ) from error
            if state.error_text is not None:
                message = state.error_text.strip() or "unknown Market error"
                raise MarketSubmissionError(f"Market rejected {item.name!r}: {message}")
            assert state.stock_text is not None
            remaining_stock = parse_market_stock(state.stock_text)
            if remaining_stock != 0:
                raise MarketSubmissionError(
                    f"Market did not sell all planned stock for {item.name!r}: "
                    f"before={item.stock}, after={remaining_stock}"
                )
            sales.append(MarketSale(item=item, remaining_stock=remaining_stock))
        return MarketSaleReport(realm=plan.realm, sales=tuple(sales))

    async def _open_sale_form(self, item: MarketItem, *, realm: Realm) -> _SaleForm:
        state = await self._open_sale_state(item, realm=realm)
        sell_order_id, order_text, current_stock = self._sale_details(item, state)
        try:
            stock_control = await query_page(
                self._driver.page,
                "#sell_order_stock_field > span",
            )
            update_button = await query_page(
                self._driver.page,
                "#sellorder_update",
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise MarketPageError(
                f"Market sale form is missing for {item.name!r}"
            ) from error
        if stock_control is None or update_button is None:
            raise MarketPageError(f"Market sale form is missing for {item.name!r}")
        return _SaleForm(
            sell_order_id=sell_order_id,
            order_text=order_text,
            current_stock=current_stock,
            stock_control=stock_control,
            update_button=update_button,
        )

    async def _open_sale_state(
        self,
        item: MarketItem,
        *,
        realm: Realm,
    ) -> _MarketSaleState:
        await self._driver.get(
            market_item_url(item.category, item.item_id, realm=realm)
        )
        try:
            state = _decode_market_sale_state(
                await evaluate_page(
                    self._driver.page,
                    _MARKET_SALE_STATE_SCRIPT,
                )
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise MarketPageError(
                f"Market sale form is missing for {item.name!r}"
            ) from error
        return state

    @staticmethod
    def _sale_details(
        item: MarketItem,
        state: _MarketSaleState,
    ) -> tuple[int, str, int]:
        if not state.sell_orders:
            raise MarketPageError(
                f"Market has no existing sell order to price {item.name!r}"
            )
        if (
            not state.has_stock_control
            or state.stock_text is None
            or not state.stock_text.strip()
            or not state.has_update_button
        ):
            raise MarketPageError(f"Market sale form is missing for {item.name!r}")
        onclick, order_text = state.sell_orders[0]
        return (
            parse_market_sell_order_id(onclick),
            order_text.strip(),
            parse_market_stock(state.stock_text),
        )

    @staticmethod
    def _sale_state_stock_is_zero(state: _MarketSaleState) -> bool:
        if state.stock_text is None or not state.stock_text.strip():
            return False
        try:
            return parse_market_stock(state.stock_text) == 0
        except MarketPageError:
            return False

    async def _inspect_category(
        self, category: MarketCategory, *, realm: Realm
    ) -> list[MarketItem]:
        await self._driver.get(market_browse_url(category, realm=realm))
        try:
            rows = _decode_market_category(
                await evaluate_page(
                    self._driver.page,
                    _MARKET_CATEGORY_STATE_SCRIPT,
                )
            )
        except Exception as error:
            if is_browser_generation_error(error) or isinstance(error, MarketPageError):
                raise
            raise MarketPageError(
                f"Unable to inspect {market_category_label(category)} Market items"
            ) from error

        items: list[MarketItem] = []
        for row in rows:
            if len(row.cells) < 2:
                raise MarketPageError(
                    f"Market row has fewer than two cells in "
                    f"{market_category_label(category)}"
                )
            name = row.cells[0].strip()
            items.append(
                MarketItem(
                    category=category,
                    item_id=parse_market_item_id(row.onclick),
                    name=name,
                    stock=parse_market_stock(row.cells[1]),
                )
            )
        return items
