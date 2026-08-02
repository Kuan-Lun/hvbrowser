"""Credential-safe, read-only smoke checks for a configured account."""

import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .hv import HVDriver
from .market import MarketCategory, MarketClient, MarketPageError


class LiveProbeRefused(RuntimeError):
    """A live probe was stopped before browser construction."""


@dataclass(frozen=True)
class MarketCategorySummary:
    category: MarketCategory
    item_types: int
    stocked_item_types: int


@dataclass(frozen=True)
class MarketQuoteSummary:
    category: MarketCategory
    item_id: int
    stock: int
    sell_order_id: int
    order_text: str


@dataclass(frozen=True)
class LiveProbeResult:
    stamina: int
    is_isekai: bool
    market: tuple[MarketCategorySummary, ...]
    quote: MarketQuoteSummary | None


def validate_live_environment(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Require credential indirection without selecting an account profile."""
    current = os.environ if environment is None else environment
    missing = [name for name in ("EH_USERNAME", "EH_PASSWORD") if not current.get(name)]
    if missing:
        raise LiveProbeRefused(
            "Missing credential environment variables: " + ", ".join(missing)
        )


async def run_live_probe(
    *,
    inspect_market_form: bool = False,
    driver_factory: Callable[[], HVDriver] | None = None,
    environment: Mapping[str, str] | None = None,
) -> LiveProbeResult:
    """Log in and inspect non-battle state without performing any mutation."""
    validate_live_environment(environment)
    create_driver = driver_factory or (lambda: HVDriver(headless=True))

    async with create_driver() as driver:
        battle_markers = await driver.page.xpath("//*[@id='battle_main']", timeout=2)
        if battle_markers:
            raise LiveProbeRefused(
                "An active battle was detected; the read-only probe stopped"
            )

        stamina = await driver.get_stamina()
        is_isekai = await driver.is_isekai
        market_client = MarketClient(driver)
        snapshot = await market_client.inspect(is_isekai=is_isekai)

        item_counts = Counter(item.category for item in snapshot.items)
        stocked_counts = Counter(
            item.category for item in snapshot.items if item.stock > 0
        )
        categories = tuple(
            MarketCategorySummary(
                category=category,
                item_types=item_counts[category],
                stocked_item_types=stocked_counts[category],
            )
            for category in MarketCategory
            if item_counts[category]
        )

        quote_summary: MarketQuoteSummary | None = None
        if inspect_market_form:
            for item in (item for item in snapshot.items if item.stock > 0):
                try:
                    quote = await market_client.inspect_sale_quote(
                        item, is_isekai=is_isekai
                    )
                except MarketPageError:
                    continue
                quote_summary = MarketQuoteSummary(
                    category=item.category,
                    item_id=item.item_id,
                    stock=quote.current_stock,
                    sell_order_id=quote.sell_order_id,
                    order_text=quote.order_text,
                )
                break

        return LiveProbeResult(
            stamina=stamina,
            is_isekai=is_isekai,
            market=categories,
            quote=quote_summary,
        )
