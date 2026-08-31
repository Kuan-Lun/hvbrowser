import ast
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from hvbrowser.live_probe import (
    LiveProbeRefused,
    run_live_probe,
    run_lottery_readonly_probe,
)
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
from hvbrowser.runtime import ZendriverOperationTimeout

_CREDENTIAL_ENV = {
    "EH_USERNAME": "provided-indirectly",
    "EH_PASSWORD": "provided-indirectly",
}


class _FakePage:
    def __init__(self, *, in_battle: bool = False) -> None:
        self.in_battle = in_battle
        self.observation_payload: object = {
            "url": "https://hentaiverse.org/",
            "challenge": False,
            "completion": False,
            "nextFloor": False,
            "active": in_battle,
        }

    async def evaluate(self, script: str) -> object:
        if "nextFloor" in script and "battle_main" in script:
            return self.observation_payload
        raise AssertionError(f"Unexpected battle check: {script}")


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
        self.lottery = SimpleNamespace(
            inspect=AsyncMock(),
            inspect_once=AsyncMock(),
        )
        self.monster_lab = SimpleNamespace(inspect=AsyncMock())
        self.exited = False
        self.home_calls = 0

    async def start(
        self,
        *,
        on_persistent_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> _FakeSession:
        self.home_calls += 1
        try:
            if on_persistent_ready is not None:
                await on_persistent_ready()
        except BaseException:
            self.exited = True
            raise
        return self

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exited = True


class LiveProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_lottery_probe_credential_guard_precedes_session(self) -> None:
        factory = Mock()

        with self.assertRaisesRegex(LiveProbeRefused, "EH_USERNAME"):
            await run_lottery_readonly_probe(
                session_factory=factory,
                environment={},
            )

        factory.assert_not_called()

    async def test_lottery_probe_only_inspects_both_lotteries_once(self) -> None:
        session = _FakeSession()
        session.lottery.inspect_once.side_effect = [
            LotterySnapshot(LotteryKind.WEAPON, 8_077_830, 87),
            LotterySnapshot(LotteryKind.ARMOR, 8_077_830, 46),
        ]

        result = await run_lottery_readonly_probe(
            session_factory=lambda: session,
            environment=_CREDENTIAL_ENV,
        )

        self.assertEqual(
            [(summary.kind, summary.tickets) for summary in result],
            [(LotteryKind.WEAPON, 87), (LotteryKind.ARMOR, 46)],
        )
        self.assertTrue(all(summary.gp_balance == 8_077_830 for summary in result))
        self.assertEqual(
            session.lottery.inspect_once.await_args_list,
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
        session.player.read_stamina.assert_not_awaited()
        session.realm.current.assert_not_awaited()
        session.market.inspect.assert_not_awaited()
        session.market.inspect_sale_quote.assert_not_awaited()
        session.monster_lab.inspect.assert_not_awaited()
        session.lottery.inspect.assert_not_awaited()
        self.assertEqual(session.home_calls, 1)
        self.assertTrue(session.exited)

    async def test_lottery_probe_refuses_every_battle_marker_before_inspect(
        self,
    ) -> None:
        marker_names = ("challenge", "completion", "nextFloor", "active")
        for marker_name in marker_names:
            with self.subTest(marker=marker_name):
                session = _FakeSession()
                session.browser.page.observation_payload = {
                    "url": "https://hentaiverse.org/",
                    "challenge": marker_name == "challenge",
                    "completion": marker_name == "completion",
                    "nextFloor": marker_name == "nextFloor",
                    "active": marker_name == "active",
                }

                with self.assertRaisesRegex(LiveProbeRefused, "battle state"):
                    await run_lottery_readonly_probe(
                        session_factory=lambda session=session: session,
                        environment=_CREDENTIAL_ENV,
                    )

                session.lottery.inspect_once.assert_not_awaited()
                session.lottery.inspect.assert_not_awaited()
                self.assertEqual(session.home_calls, 1)
                self.assertTrue(session.exited)

    async def test_lottery_probe_refuses_untrusted_or_wrong_realm(self) -> None:
        destinations = (
            "https://example.test/",
            "https://hentaiverse.org/isekai/",
            "https://hentaiverse.org/unexpected",
        )
        for destination in destinations:
            with self.subTest(destination=destination):
                session = _FakeSession()
                session.browser.page.observation_payload = {
                    "url": destination,
                    "challenge": False,
                    "completion": False,
                    "nextFloor": False,
                    "active": False,
                }

                with self.assertRaisesRegex(LiveProbeRefused, "Persistent realm"):
                    await run_lottery_readonly_probe(
                        session_factory=lambda session=session: session,
                        environment=_CREDENTIAL_ENV,
                    )

                session.lottery.inspect_once.assert_not_awaited()
                session.lottery.inspect.assert_not_awaited()
                self.assertEqual(session.home_calls, 1)
                self.assertTrue(session.exited)

    async def test_lottery_probe_fails_closed_without_retry_and_closes(self) -> None:
        session = _FakeSession()
        session.lottery.inspect_once.side_effect = RuntimeError("unreadable")

        with self.assertRaisesRegex(RuntimeError, "unreadable"):
            await run_lottery_readonly_probe(
                session_factory=lambda: session,
                environment=_CREDENTIAL_ENV,
            )

        self.assertEqual(session.lottery.inspect_once.await_count, 1)
        session.lottery.inspect.assert_not_awaited()
        session.player.read_stamina.assert_not_awaited()
        session.market.inspect.assert_not_awaited()
        session.monster_lab.inspect.assert_not_awaited()
        self.assertEqual(session.home_calls, 1)
        self.assertTrue(session.exited)

    async def test_lottery_probe_refuses_invalid_preflight_payload(self) -> None:
        session = _FakeSession()
        session.browser.page.observation_payload = None

        with self.assertRaisesRegex(LiveProbeRefused, "Unable to verify"):
            await run_lottery_readonly_probe(
                session_factory=lambda: session,
                environment=_CREDENTIAL_ENV,
            )

        session.lottery.inspect_once.assert_not_awaited()
        session.lottery.inspect.assert_not_awaited()
        self.assertEqual(session.home_calls, 1)
        self.assertTrue(session.exited)

    async def test_lottery_probe_preserves_browser_generation_failure(self) -> None:
        session = _FakeSession()
        failure = ZendriverOperationTimeout(timeout_seconds=5)

        with (
            patch(
                "hvbrowser.live_probe.observe_maintenance_navigation",
                new=AsyncMock(side_effect=failure),
            ),
            self.assertRaises(ZendriverOperationTimeout) as raised,
        ):
            await run_lottery_readonly_probe(
                session_factory=lambda: session,
                environment=_CREDENTIAL_ENV,
            )

        self.assertIs(raised.exception, failure)
        session.lottery.inspect_once.assert_not_awaited()
        self.assertEqual(session.home_calls, 1)
        self.assertTrue(session.exited)

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

        with self.assertRaisesRegex(LiveProbeRefused, "battle state"):
            await run_live_probe(
                session_factory=lambda: session,
                environment=_CREDENTIAL_ENV,
            )

        session.player.read_stamina.assert_not_awaited()
        self.assertEqual(session.home_calls, 1)
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
        session.lottery.inspect_once.side_effect = [
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
            session.lottery.inspect_once.await_args_list,
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
        session.lottery.inspect_once.side_effect = [
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
        session.lottery.inspect_once.assert_not_awaited()
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
