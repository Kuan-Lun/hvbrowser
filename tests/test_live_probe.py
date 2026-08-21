import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from hvbrowser.live_probe import _ACTIVE_BATTLE_XPATH, LiveProbeRefused, run_live_probe
from hvbrowser.lottery import LotteryKind, LotterySnapshot
from hvbrowser.maintenance_navigation import MaintenanceNavigationContext
from hvbrowser.market import (
    MarketCategory,
    MarketItem,
    MarketSaleQuote,
    MarketSnapshot,
)
from hvbrowser.monster_lab import MonsterLabFeed, MonsterLabSnapshot
from hvbrowser.realm import Realm

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


class _FakeSession:
    def __init__(
        self,
        *,
        in_battle: bool = False,
        realm: Realm = Realm.PERSISTENT,
    ) -> None:
        self.browser = SimpleNamespace(page=_FakePage(in_battle=in_battle))
        self.player = SimpleNamespace(read_stamina=AsyncMock(return_value=83))
        self.realm = SimpleNamespace(current=AsyncMock(return_value=realm))
        self.market = SimpleNamespace(
            inspect=AsyncMock(),
            inspect_sale_quote=AsyncMock(),
        )
        self.lottery = SimpleNamespace(inspect=AsyncMock())
        self.monster_lab = SimpleNamespace(inspect=AsyncMock())
        self.exited = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exited = True


class LiveProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_credential_guard_runs_before_session_construction(self) -> None:
        factory = Mock()

        with self.assertRaisesRegex(LiveProbeRefused, "EH_USERNAME"):
            await run_live_probe(session_factory=factory, environment={})

        factory.assert_not_called()

    async def test_guard_rejects_missing_credential_indirection(self) -> None:
        factory = Mock()

        with self.assertRaisesRegex(LiveProbeRefused, "EH_PASSWORD"):
            await run_live_probe(
                session_factory=factory,
                environment={"EH_USERNAME": "set"},
            )

        factory.assert_not_called()

    async def test_active_battle_stops_before_non_battle_checks(self) -> None:
        session = _FakeSession(in_battle=True)

        with self.assertRaisesRegex(LiveProbeRefused, "active battle"):
            await run_live_probe(
                session_factory=lambda: session,
                environment=_CREDENTIAL_ENV,
            )

        session.player.read_stamina.assert_not_awaited()
        self.assertTrue(session.exited)

    async def test_market_form_requires_market_before_session_construction(
        self,
    ) -> None:
        factory = Mock()

        with self.assertRaisesRegex(ValueError, "requires inspect_market"):
            await run_live_probe(
                inspect_market=False,
                inspect_market_form=True,
                session_factory=factory,
                environment=_CREDENTIAL_ENV,
            )

        factory.assert_not_called()

    async def test_read_only_probe_returns_aggregate_market_state(self) -> None:
        session = _FakeSession()
        item = MarketItem(MarketCategory.CONSUMABLES, 101, "Health Draught", 15)
        session.market.inspect.return_value = MarketSnapshot(
            realm=Realm.PERSISTENT,
            items=(item,),
        )
        session.market.inspect_sale_quote.return_value = MarketSaleQuote(
            item=item,
            sell_order_id=987,
            order_text="100 C",
            current_stock=15,
        )
        session.lottery.inspect.side_effect = [
            LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200),
            LotterySnapshot(LotteryKind.ARMOR, 1_600_000, 100),
        ]
        session.monster_lab.inspect.return_value = MonsterLabSnapshot(
            frozenset({MonsterLabFeed.FOOD, MonsterLabFeed.DRUGS})
        )

        result = await run_live_probe(
            inspect_market_form=True,
            inspect_lotteries=True,
            inspect_monster_lab=True,
            session_factory=lambda: session,
            environment=_CREDENTIAL_ENV,
        )

        self.assertEqual(result.stamina, 83)
        self.assertIs(result.realm, Realm.PERSISTENT)
        self.assertEqual(result.market[0].stocked_item_types, 1)
        self.assertEqual(result.quote.sell_order_id if result.quote else None, 987)
        self.assertEqual(
            [(summary.kind, summary.tickets) for summary in result.lotteries],
            [(LotteryKind.WEAPON, 200), (LotteryKind.ARMOR, 100)],
        )
        self.assertTrue(result.monster_lab and result.monster_lab.food_available)
        self.assertTrue(result.monster_lab and result.monster_lab.drugs_available)
        self.assertEqual(
            session.lottery.inspect.await_args_list,
            [
                unittest.mock.call(
                    LotteryKind.WEAPON,
                    context=MaintenanceNavigationContext.ORDINARY,
                ),
                unittest.mock.call(
                    LotteryKind.ARMOR,
                    context=MaintenanceNavigationContext.ORDINARY,
                ),
            ],
        )
        session.monster_lab.inspect.assert_awaited_once_with(
            context=MaintenanceNavigationContext.ORDINARY
        )
        session.market.inspect_sale_quote.assert_awaited_once_with(
            item,
            realm=Realm.PERSISTENT,
        )
        self.assertTrue(session.exited)

    async def test_market_can_be_skipped_for_independent_selector_checks(
        self,
    ) -> None:
        session = _FakeSession()
        session.lottery.inspect.side_effect = [
            LotterySnapshot(LotteryKind.WEAPON, 1_600_000, 200),
            LotterySnapshot(LotteryKind.ARMOR, 1_600_000, 100),
        ]
        session.monster_lab.inspect.return_value = MonsterLabSnapshot(
            frozenset({MonsterLabFeed.FOOD})
        )

        result = await run_live_probe(
            inspect_market=False,
            inspect_lotteries=True,
            inspect_monster_lab=True,
            session_factory=lambda: session,
            environment=_CREDENTIAL_ENV,
        )

        session.market.inspect.assert_not_awaited()
        session.market.inspect_sale_quote.assert_not_awaited()
        self.assertEqual(result.market, ())
        self.assertIsNone(result.quote)
        self.assertEqual(len(result.lotteries), 2)
        self.assertTrue(result.monster_lab and result.monster_lab.food_available)

    async def test_isekai_probe_skips_persistent_only_services(self) -> None:
        session = _FakeSession(realm=Realm.ISEKAI)

        result = await run_live_probe(
            inspect_market=False,
            inspect_lotteries=True,
            inspect_monster_lab=True,
            session_factory=lambda: session,
            environment=_CREDENTIAL_ENV,
        )

        self.assertIs(result.realm, Realm.ISEKAI)
        self.assertEqual(result.lotteries, ())
        self.assertIsNone(result.monster_lab)
        session.market.inspect.assert_not_awaited()
        session.lottery.inspect.assert_not_awaited()
        session.monster_lab.inspect.assert_not_awaited()

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
                    "click",
                    "evaluate",
                    "feed_all",
                    "mouse_click",
                    "purchase",
                    "recover_stamina",
                    "repair_all",
                    "submit_sales",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
