"""Credential-safe, read-only smoke checks for a configured account."""

import os
from collections import Counter
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from .lottery import LotteryKind, LotterySnapshot
from .maintenance_navigation import (
    MaintenanceNavigationContext,
    observe_maintenance_navigation,
)
from .market import MarketCategory, MarketPageError
from .monster_lab import MonsterLabFeed
from .realm import Realm
from .runtime import is_browser_generation_error
from .session import HentaiVerseSession


class LiveProbeRefused(RuntimeError):
    """A live probe was stopped by a fail-closed safety check."""


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
    realm: Realm
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


def _summarize_lottery(snapshot: LotterySnapshot) -> LotterySummary:
    return LotterySummary(
        snapshot.kind,
        snapshot.tickets,
        snapshot.gp_balance,
        snapshot.ticket_price_gp,
    )


async def _inspect_lotteries(
    session: HentaiVerseSession,
) -> tuple[LotterySummary, ...]:
    summaries: list[LotterySummary] = []
    for kind in LotteryKind:
        snapshot = await session.lottery.inspect_once(
            kind,
            context=MaintenanceNavigationContext.ORDINARY,
        )
        summaries.append(_summarize_lottery(snapshot))
    return tuple(summaries)


async def _require_safe_persistent_probe_page(session: HentaiVerseSession) -> None:
    try:
        observation = await observe_maintenance_navigation(session.browser.page)
    except Exception as error:
        if is_browser_generation_error(error):
            raise
        raise LiveProbeRefused(
            "Unable to verify the page before the read-only probe"
        ) from error
    if (
        observation.realm is not Realm.PERSISTENT
        or urlsplit(observation.url).path != "/"
    ):
        raise LiveProbeRefused(
            "The read-only probe requires a trusted Persistent realm page"
        )
    if observation.blocker is not None:
        raise LiveProbeRefused(
            "A battle state was detected; the read-only probe stopped"
        )


@asynccontextmanager
async def _safe_persistent_probe_session(
    session_factory: Callable[[], HentaiVerseSession],
) -> AsyncIterator[HentaiVerseSession]:
    session = session_factory()
    async with AsyncExitStack() as stack:
        await session.start(
            on_persistent_ready=lambda: _require_safe_persistent_probe_page(session)
        )
        stack.push_async_exit(session)
        yield session


async def run_lottery_readonly_probe(
    *,
    session_factory: Callable[[], HentaiVerseSession] | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[LotterySummary, ...]:
    """Inspect both Persistent lotteries once from a trusted non-battle page."""
    validate_live_environment(environment)
    create_session = session_factory or (lambda: HentaiVerseSession(headless=True))

    async with _safe_persistent_probe_session(create_session) as session:
        return await _inspect_lotteries(session)


async def run_live_probe(
    *,
    inspect_market: bool = True,
    inspect_market_form: bool = False,
    inspect_lotteries: bool = False,
    inspect_monster_lab: bool = False,
    session_factory: Callable[[], HentaiVerseSession] | None = None,
    environment: Mapping[str, str] | None = None,
) -> LiveProbeResult:
    """Log in and inspect non-battle state without performing any mutation."""
    if inspect_market_form and not inspect_market:
        raise ValueError("inspect_market_form requires inspect_market to be enabled")
    validate_live_environment(environment)
    create_session = session_factory or (lambda: HentaiVerseSession(headless=True))

    async with _safe_persistent_probe_session(create_session) as session:
        stamina = await session.player.read_stamina()
        realm = await session.realm.current()

        lottery_summaries: tuple[LotterySummary, ...] = ()
        if inspect_lotteries and realm is Realm.PERSISTENT:
            lottery_summaries = await _inspect_lotteries(session)

        monster_lab_summary: MonsterLabSummary | None = None
        if inspect_monster_lab and realm is Realm.PERSISTENT:
            monster_snapshot = await session.monster_lab.inspect(
                context=MaintenanceNavigationContext.ORDINARY
            )
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
            snapshot = await session.market.inspect()

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
                        quote = await session.market.inspect_sale_quote(
                            item,
                            realm=snapshot.realm,
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
            realm=realm,
            market=categories,
            quote=quote_summary,
            lotteries=lottery_summaries,
            monster_lab=monster_lab_summary,
        )
