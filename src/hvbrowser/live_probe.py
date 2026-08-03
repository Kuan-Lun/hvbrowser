"""Credential-safe, read-only smoke checks for a configured account."""

import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .hv import HVDriver
from .lottery import LotteryClient, LotteryKind
from .market import MarketCategory, MarketClient, MarketPageError
from .monster_lab import MonsterLabClient, MonsterLabFeed


class LiveProbeRefused(RuntimeError):
    """A live probe was stopped before browser construction."""


_ACTIVE_BATTLE_XPATH = "//*[@id='battle_main' or @id='riddlesubmit' or @id='btcp']"


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
class LotterySummary:
    kind: LotteryKind
    tickets: int
    gp_balance: int
    ticket_price_gp: int


@dataclass(frozen=True)
class MonsterLabSummary:
    food_available: bool
    drugs_available: bool


@dataclass(frozen=True)
class LiveProbeResult:
    stamina: int
    is_isekai: bool
    market: tuple[MarketCategorySummary, ...]
    quote: MarketQuoteSummary | None
    lotteries: tuple[LotterySummary, ...]
    monster_lab: MonsterLabSummary | None


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
    inspect_market: bool = True,
    inspect_market_form: bool = False,
    inspect_lotteries: bool = False,
    inspect_monster_lab: bool = False,
    driver_factory: Callable[[], HVDriver] | None = None,
    environment: Mapping[str, str] | None = None,
) -> LiveProbeResult:
    """Log in and inspect non-battle state without performing any mutation."""
    if inspect_market_form and not inspect_market:
        raise ValueError("inspect_market_form requires inspect_market to be enabled")
    validate_live_environment(environment)
    create_driver = driver_factory or (lambda: HVDriver(headless=True))

    async with create_driver() as driver:
        battle_markers = await driver.page.xpath(_ACTIVE_BATTLE_XPATH, timeout=2)
        if battle_markers:
            raise LiveProbeRefused(
                "An active battle was detected; the read-only probe stopped"
            )

        stamina = await driver.get_stamina()
        is_isekai = await driver.is_isekai

        lottery_summaries: tuple[LotterySummary, ...] = ()
        if inspect_lotteries and not is_isekai:
            lottery_client = LotteryClient(driver)
            lottery_summaries = tuple(
                LotterySummary(
                    kind=snapshot.kind,
                    tickets=snapshot.tickets,
                    gp_balance=snapshot.gp_balance,
                    ticket_price_gp=snapshot.ticket_price_gp,
                )
                for snapshot in [
                    await lottery_client.inspect(kind) for kind in LotteryKind
                ]
            )

        monster_lab_summary: MonsterLabSummary | None = None
        if inspect_monster_lab and not is_isekai:
            monster_snapshot = await MonsterLabClient(driver).inspect()
            monster_lab_summary = MonsterLabSummary(
                food_available=(
                    MonsterLabFeed.FOOD in monster_snapshot.available_feed_all
                ),
                drugs_available=(
                    MonsterLabFeed.DRUGS in monster_snapshot.available_feed_all
                ),
            )

        categories: tuple[MarketCategorySummary, ...] = ()
        quote_summary: MarketQuoteSummary | None = None
        if inspect_market:
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
            lotteries=lottery_summaries,
            monster_lab=monster_lab_summary,
        )
