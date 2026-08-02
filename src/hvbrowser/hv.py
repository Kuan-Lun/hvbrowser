import re
from abc import ABC
from typing import Any
from urllib.parse import urlsplit

from hbrowser.gallery import EHDriver
from hbrowser.gallery.utils import setup_logger

from .lottery import (
    LotteryClient,
    LotteryKind,
    LotteryPurchaseReport,
    LotterySnapshot,
)
from .market import (
    MarketCategory,
    MarketClient,
    MarketSalePlan,
    MarketSaleReport,
    MarketSnapshot,
)
from .monster_lab import (
    MonsterLabClient,
    MonsterLabFeed,
    MonsterLabFeedReport,
    MonsterLabSnapshot,
)
from .urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL

logger = setup_logger(__name__)


def _is_isekai_url(url: object) -> bool:
    if not isinstance(url, str):
        raise RuntimeError("Unable to determine realm from the current URL")

    parsed = urlsplit(url)
    expected = urlsplit(HENTAIVERSE_ROOT_URL)
    try:
        matches_origin = (
            parsed.scheme.casefold() == expected.scheme
            and parsed.hostname == expected.hostname
            and (parsed.port or 443) == (expected.port or 443)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError as error:
        raise RuntimeError("Unable to determine realm from the current URL") from error
    if not matches_origin:
        raise RuntimeError("Unable to determine realm outside HentaiVerse")

    return parsed.path == "/isekai" or parsed.path.startswith("/isekai/")


def genxpath(imagepath: str) -> str:
    return f'//img[@src="{imagepath}"]'


class BSItems(ABC):
    def __init__(
        self,
        consumables: list[str] | None = None,
        materials: list[str] | None = None,
        trophies: list[str] | None = None,
        artifacts: list[str] | None = None,
        figures: list[str] | None = None,
        monster_items: list[str] | None = None,
    ) -> None:
        self.consumables = consumables or []
        self.materials = materials or []
        self.trophies = trophies or []
        self.artifacts = artifacts or []
        self.figures = figures or []
        self.monster_items = monster_items or []

    def market_names(self) -> dict[MarketCategory, tuple[str, ...]]:
        return {
            MarketCategory.CONSUMABLES: tuple(self.consumables),
            MarketCategory.MATERIALS: tuple(self.materials),
            MarketCategory.TROPHIES: tuple(self.trophies),
            MarketCategory.ARTIFACTS: tuple(self.artifacts),
            MarketCategory.FIGURES: tuple(self.figures),
            MarketCategory.MONSTER_ITEMS: tuple(self.monster_items),
        }


class SellItems(BSItems):
    pass


class BuyItems(BSItems):
    pass


class HVDriver(EHDriver):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.url[self.name] = HENTAIVERSE_ROOT_URL
        self.url["HentaiVerse isekai"] = HENTAIVERSE_ISEKAI_ROOT_URL

    def _setname(self) -> str:
        return "HentaiVerse"

    @property
    async def is_isekai(self) -> bool:
        url = await self.page.evaluate("window.location.href")
        return _is_isekai_url(url)

    async def _get_path_prefix(self) -> str:
        return "/isekai" if await self.is_isekai else ""

    def searchxpath(self, srclist: list[Any] | tuple[Any, ...] | set[Any]) -> str:
        return " | ".join([genxpath(imagepath) for imagepath in srclist])

    async def goisekai(self) -> None:
        logger.info("Navigating to HentaiVerse isekai page")
        await self.get(HENTAIVERSE_ISEKAI_ROOT_URL)

    async def inspect_market(self) -> MarketSnapshot:
        """Return a read-only snapshot of the visible Market inventory."""
        return await MarketClient(self).inspect(is_isekai=await self.is_isekai)

    async def plan_market_sales(self, sellitems: SellItems) -> MarketSalePlan:
        """Build a sale plan without clicking or submitting Market controls."""
        return await MarketClient(self).plan_sales(
            sellitems.market_names(), is_isekai=await self.is_isekai
        )

    async def validate_market_sales(self, plan: MarketSalePlan) -> None:
        """Validate sale-form selectors without changing Market state."""
        await MarketClient(self).validate_sale_forms(plan)

    async def submit_market_sales(self, plan: MarketSalePlan) -> MarketSaleReport:
        """Submit a previously reviewed sale plan."""
        return await MarketClient(self).submit_sales(plan)

    async def inspect_lottery(self, kind: LotteryKind) -> LotterySnapshot:
        """Return a read-only snapshot for one explicit lottery."""
        return await LotteryClient(self).inspect(kind)

    async def purchase_lottery_tickets(
        self,
        kind: LotteryKind,
        amount: int,
        *,
        expected_before: LotterySnapshot | None = None,
    ) -> LotteryPurchaseReport:
        """Purchase and verify one explicit lottery ticket quantity."""
        return await LotteryClient(self).purchase(
            kind,
            amount,
            expected_before=expected_before,
        )

    async def loetterycheck(self, num: int) -> None:
        """Compatibility wrapper that replenishes both lotteries in order."""
        logger.info(f"Checking lottery tickets (target: {num})")
        if not isinstance(num, int) or isinstance(num, bool) or num < 0:
            raise ValueError("Lottery target must be a non-negative integer")
        client = LotteryClient(self)
        for kind in LotteryKind:
            snapshot = await client.inspect(kind)
            deficit = max(0, num - snapshot.tickets)
            purchase_amount = min(
                deficit, snapshot.gp_balance // snapshot.ticket_price_gp
            )
            if purchase_amount:
                await client.purchase(
                    kind,
                    purchase_amount,
                    expected_before=snapshot,
                )
            elif deficit:
                logger.info(
                    "Insufficient GP to replenish %s (holding %d of %d)",
                    kind.value,
                    snapshot.tickets,
                    num,
                )

    async def get_stamina(self) -> int:
        stamina_elements = await self.page.xpath(
            "//div[contains(text(), 'Stamina:')]", timeout=5
        )
        if not stamina_elements:
            raise ValueError("Unable to find stamina readout")
        match = re.search(r"Stamina:\s*(\d+)", stamina_elements[0].text)
        if not match:
            raise ValueError(
                f"Unable to parse stamina from: {stamina_elements[0].text!r}"
            )
        return int(match.group(1))

    async def recoverstamina(self) -> bool:
        logger.info("Checking USR RESTORATIVE availability for stamina recovery")

        stamina_readout = await self.page.select("#stamina_readout")
        await stamina_readout.mouse_move()

        restorative_elements = await self.page.xpath(
            "//img[@onclick=\"document.getElementById('recoverform').submit()\"]",
            timeout=5,
        )
        if not restorative_elements:
            logger.debug("USR RESTORATIVE is not available")
            return False

        restorative_img = restorative_elements[0]
        await restorative_img.mouse_move()
        await restorative_img.mouse_click()
        await self.page.wait(1)

        error_elements = await self.page.xpath(
            "//p[contains(@class, 'messagebox_error')]", timeout=2
        )
        if error_elements:
            logger.warning(f"USR RESTORATIVE failed: {error_elements[0].text}")
            await error_elements[0].click()
            return False

        logger.info("Used USR RESTORATIVE to recover stamina")
        return True

    async def _select_all_and_check_repair_submit(
        self, equipcount_elements: list[Any]
    ) -> tuple[bool | None, list[Any]]:
        """全選裝備並回報 repair submit 按鈕狀態。

        回傳 (is_disabled, submit_elements):
        is_disabled 為 None 代表找不到 submit 按鈕，呼叫端應視為無需處理。
        """
        logger.debug(f"Before select_all click: {equipcount_elements[0].text!r}")
        await self.wait(equipcount_elements[0].mouse_click, ischangeurl=False)

        equipcount_after = await self.page.xpath("//label[@id='equipcount']", timeout=5)
        if equipcount_after:
            logger.debug(f"After select_all click: {equipcount_after[0].text!r}")

        submit_elements = await self.page.xpath("//input[@id='equipsubmit']", timeout=5)
        if not submit_elements:
            logger.warning("Unable to find equipment repair submit button")
            return None, []

        is_disabled = await self.page.evaluate(
            "document.getElementById('equipsubmit').disabled"
        )
        if is_disabled:
            debug_state = await self.page.evaluate("""
                JSON.stringify({
                    selected_count: selected_count,
                    selectable_count: selectable_count,
                    block_submit: block_submit,
                    materials: (() => {
                        const totals = {};
                        for (const el of document.querySelectorAll('input[name="eqids[]"]')) {
                            if (el.checked && eqitems[el.value]) {
                                for (const m in eqitems[el.value].m) {
                                    totals[m] = (totals[m] || 0) + eqitems[el.value].m[m];
                                }
                            }
                        }
                        return Object.entries(totals).map(([id, need]) => ({
                            id,
                            name: itemdata[id] ? itemdata[id].n : undefined,
                            need,
                            have: itemdata[id] ? itemdata[id].c : undefined,
                        }));
                    })(),
                })
                """)
            logger.warning(f"Not enough materials to repair equipment: {debug_state}")

        return bool(is_disabled), submit_elements

    async def _goto_repair_tab(self) -> bool:
        """導航到 Bazaar -> The Armory -> Repair 頁籤。成功回傳 True。"""
        try:
            bazaar = await self.page.select("#parent_Bazaar")
        except TimeoutError:
            logger.warning(
                "Timed out waiting for #parent_Bazaar; homepage may not have "
                "finished loading yet, reloading and retrying once"
            )
            await self.gohomepage(force=True)
            bazaar = await self.page.select("#parent_Bazaar")
        armory_elements = await self.page.xpath(
            "//div[contains(text(), 'The Armory')]", timeout=5
        )
        if not armory_elements:
            logger.warning("Unable to find The Armory entry")
            return False

        await bazaar.mouse_move()
        await armory_elements[0].mouse_move()
        await self.wait(armory_elements[0].mouse_click, ischangeurl=True)

        repair_elements = await self.page.xpath(
            "//div[contains(@class, 'armory_tab') and contains(text(), 'Repair')]",
            timeout=5,
        )
        if not repair_elements:
            logger.warning("Unable to find Repair tab")
            return False
        await self.wait(repair_elements[0].click, ischangeurl=True)
        return True

    async def repairequipment(self) -> bool:
        logger.info("Checking equipped gear for repairs")
        if not await self._goto_repair_tab():
            return True

        equipcount_elements = await self.page.xpath(
            "//label[@id='equipcount']", timeout=5
        )
        if not equipcount_elements:
            logger.debug("No equipment needs repair")
            return True

        match = re.search(
            r"Selected \d+ of (\d+) matching", equipcount_elements[0].text
        )
        if not match or int(match.group(1)) == 0:
            logger.debug("No equipment needs repair")
            return True

        is_disabled, submit_elements = await self._select_all_and_check_repair_submit(
            equipcount_elements
        )
        if is_disabled is None:
            return True

        if is_disabled:
            logger.debug("Re-entering Repair tab to verify against fresh server state")
            if not await self._goto_repair_tab():
                return True

            equipcount_reentered = await self.page.xpath(
                "//label[@id='equipcount']", timeout=5
            )
            if not equipcount_reentered:
                logger.debug("No equipment needs repair after re-entering Repair tab")
                return True

            is_disabled, submit_elements = (
                await self._select_all_and_check_repair_submit(equipcount_reentered)
            )
            if is_disabled is None:
                return True
            if is_disabled:
                logger.error(
                    "Still not enough materials to repair equipment "
                    "after re-entering Repair tab"
                )
                return False
            logger.info(
                "Repair submit was enabled after re-entering Repair tab; "
                "the earlier disabled check was stale"
            )

        await submit_elements[0].mouse_click()
        await self.page.wait(2)

        equipcount_after_submit = await self.page.xpath(
            "//label[@id='equipcount']", timeout=5
        )
        remaining = 0
        if equipcount_after_submit:
            match_after_submit = re.search(
                r"Selected \d+ of (\d+) matching", equipcount_after_submit[0].text
            )
            if match_after_submit:
                remaining = int(match_after_submit.group(1))

        if remaining:
            logger.error(
                f"Repair submitted but {remaining} pieces of equipment still need repair"
            )
            return False

        logger.info("Repaired equipment")
        return True

    async def inspect_monster_lab(self) -> MonsterLabSnapshot:
        """Return read-only availability for both Monster Lab feed-all actions."""
        return await MonsterLabClient(self).inspect()

    async def feed_all_monsters(self, resource: MonsterLabFeed) -> MonsterLabFeedReport:
        """Perform and verify one explicit Monster Lab feed-all action."""
        return await MonsterLabClient(self).feed_all(resource)

    async def monstercheck(self) -> None:
        """Compatibility wrapper that applies food and then drugs when needed."""
        logger.info("Starting monster check")
        client = MonsterLabClient(self)
        for resource in MonsterLabFeed:
            await client.feed_all(resource)

    async def marketcheck(
        self, sellitems: SellItems, *, commit: bool = False
    ) -> MarketSalePlan | MarketSaleReport:
        """Plan requested Market sales and submit only with explicit consent.

        The default is read-only. This workflow never deposits credits and
        never re-lists unrelated existing orders.
        """
        plan = await self.plan_market_sales(sellitems)
        logger.info(
            "Market sale plan contains %d item types and %d units",
            len(plan.items),
            plan.total_units,
        )
        if not commit:
            return plan

        await self.validate_market_sales(plan)
        report = await self.submit_market_sales(plan)
        logger.info("Market submitted %d verified item sales", len(report.sales))
        return report
