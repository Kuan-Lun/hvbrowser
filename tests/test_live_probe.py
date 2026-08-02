import ast
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from hvbrowser.live_probe import _ACTIVE_BATTLE_XPATH, LiveProbeRefused, run_live_probe
from hvbrowser.lottery import LotteryKind, LotterySnapshot
from hvbrowser.market import (
    MarketCategory,
    MarketItem,
    MarketSaleQuote,
    MarketSnapshot,
)
from hvbrowser.monster_lab import MonsterLabFeed, MonsterLabSnapshot

_CREDENTIAL_ENV = {
    "EH_USERNAME": "provided-indirectly",
    "EH_PASSWORD": "provided-indirectly",
}


class _FakePage:
    def __init__(self, *, in_battle: bool = False) -> None:
        self.in_battle = in_battle

    async def xpath(self, selector: str, timeout: int) -> list[object]:
        if selector != _ACTIVE_BATTLE_XPATH or timeout != 2:
            raise AssertionError(f"Unexpected battle check: {selector}, {timeout}")
        return [object()] if self.in_battle else []


class _FakeDriver:
    def __init__(self, *, in_battle: bool = False) -> None:
        self.page = _FakePage(in_battle=in_battle)
        self.get_stamina = AsyncMock(return_value=83)
        self.exited = False

    async def __aenter__(self) -> _FakeDriver:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exited = True

    @property
    async def is_isekai(self) -> bool:
        return False


class LiveProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_credential_guard_runs_before_driver_construction(self) -> None:
        factory = Mock()

        with self.assertRaisesRegex(LiveProbeRefused, "EH_USERNAME"):
            await run_live_probe(driver_factory=factory, environment={})

        factory.assert_not_called()

    async def test_guard_rejects_missing_credential_indirection(self) -> None:
        factory = Mock()

        with self.assertRaisesRegex(LiveProbeRefused, "EH_PASSWORD"):
            await run_live_probe(
                driver_factory=factory,
                environment={"EH_USERNAME": "set"},
            )

        factory.assert_not_called()

    async def test_active_battle_stops_before_non_battle_checks(self) -> None:
        driver = _FakeDriver(in_battle=True)

        with self.assertRaisesRegex(LiveProbeRefused, "active battle"):
            await run_live_probe(
                driver_factory=lambda: driver,
                environment=_CREDENTIAL_ENV,
            )

        driver.get_stamina.assert_not_awaited()
        self.assertTrue(driver.exited)

    async def test_market_form_requires_market_before_driver_construction(
        self,
    ) -> None:
        factory = Mock()

        with self.assertRaisesRegex(ValueError, "requires inspect_market"):
            await run_live_probe(
                inspect_market=False,
                inspect_market_form=True,
                driver_factory=factory,
                environment=_CREDENTIAL_ENV,
            )

        factory.assert_not_called()

    async def test_read_only_probe_returns_aggregate_market_state(self) -> None:
        driver = _FakeDriver()
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)
        market_client = Mock()
        market_client.inspect = AsyncMock(
            return_value=MarketSnapshot(is_isekai=False, items=(item,))
        )
        market_client.inspect_sale_quote = AsyncMock(
            return_value=MarketSaleQuote(
                item=item,
                sell_order_id=987,
                order_text="100 C",
                current_stock=15,
            )
        )
        lottery_client = Mock()
        lottery_client.inspect = AsyncMock(
            side_effect=[
                LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200),
                LotterySnapshot(LotteryKind.ARMOR, 1_600_000, 100),
            ]
        )
        monster_lab_client = Mock()
        monster_lab_client.inspect = AsyncMock(
            return_value=MonsterLabSnapshot(
                frozenset({MonsterLabFeed.FOOD, MonsterLabFeed.DRUGS})
            )
        )

        with (
            patch("hvbrowser.live_probe.MarketClient", return_value=market_client),
            patch("hvbrowser.live_probe.LotteryClient", return_value=lottery_client),
            patch(
                "hvbrowser.live_probe.MonsterLabClient",
                return_value=monster_lab_client,
            ),
        ):
            result = await run_live_probe(
                inspect_market_form=True,
                inspect_lotteries=True,
                inspect_monster_lab=True,
                driver_factory=lambda: driver,
                environment=_CREDENTIAL_ENV,
            )

        self.assertEqual(result.stamina, 83)
        self.assertEqual(result.market[0].stocked_item_types, 1)
        self.assertEqual(result.quote.sell_order_id if result.quote else None, 987)
        self.assertEqual(
            [(summary.kind, summary.tickets) for summary in result.lotteries],
            [(LotteryKind.WEAPON, 200), (LotteryKind.ARMOR, 100)],
        )
        self.assertTrue(result.monster_lab and result.monster_lab.food_available)
        self.assertTrue(result.monster_lab and result.monster_lab.drugs_available)
        self.assertEqual(
            lottery_client.inspect.await_args_list,
            [
                unittest.mock.call(LotteryKind.WEAPON),
                unittest.mock.call(LotteryKind.ARMOR),
            ],
        )
        monster_lab_client.inspect.assert_awaited_once_with()
        market_client.inspect_sale_quote.assert_awaited_once()
        self.assertTrue(driver.exited)

    async def test_market_can_be_skipped_for_independent_selector_checks(
        self,
    ) -> None:
        driver = _FakeDriver()
        lottery_client = Mock()
        lottery_client.inspect = AsyncMock(
            side_effect=[
                LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200),
                LotterySnapshot(LotteryKind.ARMOR, 1_600_000, 100),
            ]
        )
        monster_lab_client = Mock()
        monster_lab_client.inspect = AsyncMock(
            return_value=MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        )

        with (
            patch("hvbrowser.live_probe.MarketClient") as market_type,
            patch("hvbrowser.live_probe.LotteryClient", return_value=lottery_client),
            patch(
                "hvbrowser.live_probe.MonsterLabClient",
                return_value=monster_lab_client,
            ),
        ):
            result = await run_live_probe(
                inspect_market=False,
                inspect_lotteries=True,
                inspect_monster_lab=True,
                driver_factory=lambda: driver,
                environment=_CREDENTIAL_ENV,
            )

        market_type.assert_not_called()
        self.assertEqual(result.market, ())
        self.assertIsNone(result.quote)
        self.assertEqual(len(result.lotteries), 2)
        self.assertTrue(result.monster_lab and result.monster_lab.food_available)

    def test_probe_source_contains_no_mutating_browser_calls(self) -> None:
        repository = Path(__file__).parents[1]
        source_files = (
            repository / "src" / "hvbrowser" / "live_probe.py",
            repository / "scripts" / "live_readonly_smoke.py",
        )
        called_attributes: set[str] = set()
        for source_file in source_files:
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            called_attributes.update(
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            )

        self.assertTrue(
            called_attributes.isdisjoint(
                {
                    "battle",
                    "click",
                    "evaluate",
                    "feed_all",
                    "feed_all_monsters",
                    "loetterycheck",
                    "marketcheck",
                    "monstercheck",
                    "mouse_click",
                    "purchase",
                    "purchase_lottery_tickets",
                    "recoverstamina",
                    "repairequipment",
                    "submit_market_sales",
                    "submit_sales",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
