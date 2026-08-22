import unittest
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hbrowser import BrowserMutationOutcomeUnknownError

from hvbrowser import (
    EquipmentRepairClient,
    EquipmentRepairOutcome,
    EquipmentRepairPageError,
    EquipmentRepairSnapshot,
    EquipmentRepairStateChangedError,
    EquipmentRepairSubmissionError,
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
)
from hvbrowser.equipment_repair import _EquipmentPageState
from hvbrowser.maintenance_navigation import MaintenanceNavigationObservation
from hvbrowser.realm import Realm
from hvbrowser.runtime import PageStateTimeout, ZendriverOperationTimeout


class _Element:
    def __init__(self, action: Callable[[], None] | None = None) -> None:
        self._action = action
        self.clicks = 0
        self.text = ""

    async def mouse_click(self) -> None:
        self.clicks += 1
        if self._action is not None:
            self._action()


class _Page:
    def __init__(self, repair_count: int = 2) -> None:
        self.repair_count = repair_count
        self.selected_count = 0
        self.submit_disabled = False
        self.submit_error: Exception | None = None
        self.server_rejection: str | None = None
        self.error: str | None = None
        self.keep_after_submit = False
        self.count = _Element(self._select_all)
        self.submit = _Element(self._submit)
        self.expressions: list[str] = []

    def _select_all(self) -> None:
        self.selected_count = self.repair_count

    def _submit(self) -> None:
        if self.submit_error is not None:
            raise self.submit_error
        if self.server_rejection is not None:
            self.error = self.server_rejection
            return
        if not self.keep_after_submit:
            self.repair_count = 0
            self.selected_count = 0

    def state(self) -> _EquipmentPageState:
        return _EquipmentPageState(
            repair_selected=True,
            equipped_selected=True,
            has_equip_form=True,
            has_equip_list=True,
            has_equip_count=self.repair_count > 0,
            has_submit=True,
            submit_disabled=self.submit_disabled,
            selectable_count=self.repair_count,
            selected_count=self.selected_count,
            empty=self.repair_count == 0,
            row_count=self.repair_count,
            error_text=self.error,
        )

    async def evaluate(self, expression: str) -> dict[str, object]:
        self.expressions.append(expression)
        state = self.state()
        return {
            "repairSelected": state.repair_selected,
            "equippedSelected": state.equipped_selected,
            "hasEquipForm": state.has_equip_form,
            "hasEquipList": state.has_equip_list,
            "hasEquipCount": state.has_equip_count,
            "hasSubmit": state.has_submit,
            "submitDisabled": state.submit_disabled,
            "selectableCount": state.selectable_count,
            "selectedCount": state.selected_count,
            "empty": state.empty,
            "rowCount": state.row_count,
            "errorText": state.error_text,
        }

    async def query_selector(self, selector: str) -> _Element | None:
        if selector == "#equipform #equipcount":
            return self.count if self.repair_count > 0 else None
        if selector == "#equipform #equipsubmit":
            return self.submit
        raise AssertionError(f"unexpected selector: {selector}")


def _client(page: _Page) -> tuple[EquipmentRepairClient, AsyncMock]:
    realm = AsyncMock(return_value=Realm.PERSISTENT)
    driver = type("Driver", (), {"page": page})()
    client = EquipmentRepairClient(
        driver,  # type: ignore[arg-type]
        type("RealmNavigator", (), {"current": realm})(),  # type: ignore[arg-type]
    )
    return client, realm


class EquipmentRepairClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_rejects_wrong_realm_and_path(self) -> None:
        page = _Page()
        client, _ = _client(page)
        cases = (
            MaintenanceNavigationObservation(
                "https://hentaiverse.org/isekai/",
                Realm.ISEKAI,
                None,
            ),
            MaintenanceNavigationObservation(
                "https://hentaiverse.org/untrusted/path",
                Realm.PERSISTENT,
                None,
            ),
        )

        for observation in cases:
            with (
                self.subTest(observation=observation),
                patch(
                    "hvbrowser.equipment_repair.observe_maintenance_navigation",
                    new=AsyncMock(return_value=observation),
                ),
                self.assertRaises(EquipmentRepairPageError),
            ):
                await client._ensure_navigation_is_safe(
                    Realm.PERSISTENT,
                    "test",
                )

    async def test_each_battle_blocker_stops_before_navigation(self) -> None:
        for blocker in MaintenanceNavigationBlocker:
            page = _Page()
            driver = SimpleNamespace(page=page, get=AsyncMock())
            client = EquipmentRepairClient(  # type: ignore[arg-type]
                driver,
                SimpleNamespace(current=AsyncMock(return_value=Realm.PERSISTENT)),
            )
            observation = MaintenanceNavigationObservation(
                "https://hentaiverse.org/",
                Realm.PERSISTENT,
                blocker,
            )
            with (
                self.subTest(blocker=blocker),
                patch(
                    "hvbrowser.equipment_repair.observe_maintenance_navigation",
                    new=AsyncMock(return_value=observation),
                ),
                self.assertRaises(MaintenanceNavigationBlockedError),
            ):
                await client._open_repair_directly(Realm.PERSISTENT)
            driver.get.assert_not_awaited()

    async def test_each_battle_blocker_after_get_stops_without_page_probe(self) -> None:
        for blocker in MaintenanceNavigationBlocker:
            page = _Page()
            page.evaluate = AsyncMock(wraps=page.evaluate)
            driver = SimpleNamespace(page=page, get=AsyncMock())
            client = EquipmentRepairClient(  # type: ignore[arg-type]
                driver,
                SimpleNamespace(current=AsyncMock(return_value=Realm.PERSISTENT)),
            )
            observations = [
                MaintenanceNavigationObservation(
                    "https://hentaiverse.org/",
                    Realm.PERSISTENT,
                    None,
                ),
                MaintenanceNavigationObservation(
                    "https://hentaiverse.org/?s=Bazaar&ss=am&screen=repair",
                    Realm.PERSISTENT,
                    blocker,
                ),
            ]
            with (
                self.subTest(blocker=blocker),
                patch(
                    "hvbrowser.equipment_repair.observe_maintenance_navigation",
                    new=AsyncMock(side_effect=observations),
                ),
                self.assertRaises(MaintenanceNavigationBlockedError),
            ):
                await client._open_repair_directly(Realm.PERSISTENT)
            driver.get.assert_awaited_once()
            page.evaluate.assert_not_awaited()

    async def test_direct_get_generation_timeout_has_no_post_probe(self) -> None:
        page = _Page()
        timeout = ZendriverOperationTimeout(timeout_seconds=0.01)
        driver = SimpleNamespace(page=page, get=AsyncMock(side_effect=timeout))
        client = EquipmentRepairClient(  # type: ignore[arg-type]
            driver,
            SimpleNamespace(current=AsyncMock(return_value=Realm.PERSISTENT)),
        )
        observe = AsyncMock(
            return_value=MaintenanceNavigationObservation(
                "https://hentaiverse.org/",
                Realm.PERSISTENT,
                None,
            )
        )

        with (
            patch(
                "hvbrowser.equipment_repair.observe_maintenance_navigation",
                new=observe,
            ),
            self.assertRaises(ZendriverOperationTimeout) as raised,
        ):
            await client._open_repair_directly(Realm.PERSISTENT)

        self.assertIs(raised.exception, timeout)
        self.assertEqual(observe.await_count, 1)

    async def test_wrong_repair_query_fails_before_dom_state_probe(self) -> None:
        page = _Page()
        page.evaluate = AsyncMock(wraps=page.evaluate)
        client, _ = _client(page)
        client._ensure_navigation_is_safe = AsyncMock(  # type: ignore[method-assign]
            return_value=MaintenanceNavigationObservation(
                "https://hentaiverse.org/?s=Bazaar&ss=am&screen=shop",
                Realm.PERSISTENT,
                None,
            )
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "Repair route"):
            await client._verify_repair_destination(Realm.PERSISTENT)

        page.evaluate.assert_not_awaited()

    async def test_each_realm_uses_its_scoped_direct_url(self) -> None:
        expected = {
            Realm.PERSISTENT: "https://hentaiverse.org/?s=Bazaar&ss=am&screen=repair&filter=equipped",
            Realm.ISEKAI: "https://hentaiverse.org/isekai/?s=Bazaar&ss=am&screen=repair&filter=equipped",
        }
        for realm, url in expected.items():
            page = _Page(repair_count=0)
            driver = SimpleNamespace(page=page, get=AsyncMock())
            client = EquipmentRepairClient(  # type: ignore[arg-type]
                driver,
                SimpleNamespace(current=AsyncMock(return_value=realm)),
            )
            root = (
                "https://hentaiverse.org/isekai/"
                if realm is Realm.ISEKAI
                else "https://hentaiverse.org/"
            )
            observations = [
                MaintenanceNavigationObservation(root, realm, None),
                MaintenanceNavigationObservation(url, realm, None),
            ]
            with (
                self.subTest(realm=realm),
                patch(
                    "hvbrowser.equipment_repair.observe_maintenance_navigation",
                    new=AsyncMock(side_effect=observations),
                ),
            ):
                await client._open_repair_directly(realm)
            driver.get.assert_awaited_once_with(url)

    async def test_empty_and_missing_count_states_fail_closed(self) -> None:
        page = _Page(repair_count=0)
        client, _ = _client(page)
        empty, control = await client._inspect_current(
            Realm.PERSISTENT,
            state=page.state(),
        )
        self.assertEqual(empty.repair_count, 0)
        self.assertIsNone(control)

        invalid = replace(
            page.state(),
            has_equip_count=False,
            selectable_count=2,
            empty=False,
            row_count=2,
        )
        with self.assertRaisesRegex(EquipmentRepairPageError, "missing"):
            await client._inspect_current(Realm.PERSISTENT, state=invalid)

    async def test_malformed_atomic_count_payload_is_rejected(self) -> None:
        page = _Page()
        payload = await page.evaluate("state")
        for value in (-1, "2"):
            with self.subTest(value=value):
                page.evaluate = AsyncMock(
                    return_value={**payload, "selectableCount": value}
                )
                client, _ = _client(page)
                with self.assertRaisesRegex(EquipmentRepairPageError, "invalid"):
                    await client._read_state()

    async def test_atomic_state_decoder_rejects_missing_selected_marker(self) -> None:
        page = _Page()
        payload = await page.evaluate("state")
        payload["repairSelected"] = False
        page.evaluate = AsyncMock(return_value=payload)
        client, _ = _client(page)
        client._ensure_navigation_is_safe = AsyncMock(  # type: ignore[method-assign]
            return_value=MaintenanceNavigationObservation(
                "https://hentaiverse.org/?s=Bazaar&ss=am&screen=repair&filter=equipped",
                Realm.PERSISTENT,
                None,
            )
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "selected-tab"):
            await client._verify_repair_destination(Realm.PERSISTENT)

    async def test_no_matching_equipment_is_ready_without_mutation(self) -> None:
        page = _Page(repair_count=0)
        client, _ = _client(page)
        client._navigate = AsyncMock(return_value=page.state())  # type: ignore[method-assign]

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        self.assertEqual(page.count.clicks, 0)
        self.assertEqual(page.submit.clicks, 0)

    async def test_expected_snapshot_prevents_stale_submission(self) -> None:
        page = _Page(repair_count=3)
        client, _ = _client(page)
        client._navigate = AsyncMock(return_value=page.state())  # type: ignore[method-assign]

        with self.assertRaises(EquipmentRepairStateChangedError):
            await client.repair_all(EquipmentRepairSnapshot(Realm.PERSISTENT, 2))

        self.assertEqual(page.submit.clicks, 0)

    async def test_repair_submits_once_and_confirms_zero(self) -> None:
        page = _Page(repair_count=2)
        client, _ = _client(page)
        client._navigate = AsyncMock(return_value=page.state())  # type: ignore[method-assign]

        report = await client.repair_all(EquipmentRepairSnapshot(Realm.PERSISTENT, 2))

        self.assertIs(report.outcome, EquipmentRepairOutcome.REPAIRED)
        self.assertEqual(report.after.repair_count, 0)
        self.assertEqual(page.count.clicks, 1)
        self.assertEqual(page.submit.clicks, 1)

    async def test_materials_unavailable_is_rechecked_from_fresh_state(self) -> None:
        page = _Page(repair_count=2)
        page.submit_disabled = True
        client, _ = _client(page)
        client._navigate = AsyncMock(  # type: ignore[method-assign]
            side_effect=[page.state(), page.state()]
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.MATERIALS_UNAVAILABLE)
        self.assertEqual(page.count.clicks, 2)
        self.assertEqual(page.submit.clicks, 0)

    async def test_fresh_material_check_rejects_concurrent_count_change(self) -> None:
        page = _Page(repair_count=2)
        page.submit_disabled = True
        client, _ = _client(page)
        changed = replace(page.state(), selectable_count=3, row_count=3)
        client._navigate = AsyncMock(  # type: ignore[method-assign]
            side_effect=[page.state(), changed]
        )

        with self.assertRaises(EquipmentRepairStateChangedError):
            await client.repair_all()

        self.assertEqual(page.submit.clicks, 0)

    async def test_submit_failure_is_unknown_without_replay(self) -> None:
        page = _Page(repair_count=2)
        page.submit_error = RuntimeError("disconnected")
        client, _ = _client(page)
        client._navigate = AsyncMock(return_value=page.state())  # type: ignore[method-assign]

        with self.assertRaises(BrowserMutationOutcomeUnknownError):
            await client.repair_all()

        self.assertEqual(page.submit.clicks, 1)

    async def test_server_rejection_is_typed_without_replay(self) -> None:
        page = _Page(repair_count=2)
        page.server_rejection = "Insufficient materials"
        client, _ = _client(page)
        client._navigate = AsyncMock(return_value=page.state())  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            EquipmentRepairSubmissionError, "Insufficient materials"
        ):
            await client.repair_all()

        self.assertEqual(page.submit.clicks, 1)

    async def test_delayed_success_does_not_replay_submission(self) -> None:
        page = _Page(repair_count=2)
        page.keep_after_submit = True
        client, _ = _client(page)
        client._navigate = AsyncMock(return_value=page.state())  # type: ignore[method-assign]
        completed = _EquipmentPageState(
            True,
            True,
            True,
            True,
            False,
            True,
            False,
            0,
            0,
            True,
            0,
            None,
        )

        with patch(
            "hvbrowser.equipment_repair.wait_for_page_state",
            new=AsyncMock(return_value=completed),
        ):
            report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.REPAIRED)
        self.assertEqual(page.submit.clicks, 1)

    async def test_semantic_timeout_is_unknown_without_replay(self) -> None:
        page = _Page(repair_count=2)
        page.keep_after_submit = True
        client, _ = _client(page)
        client._navigate = AsyncMock(return_value=page.state())  # type: ignore[method-assign]

        selected = _EquipmentPageState(
            True, True, True, True, True, True, False, 2, 2, False, 2, None
        )
        with (
            patch(
                "hvbrowser.equipment_repair.wait_for_page_state",
                new=AsyncMock(side_effect=[selected, PageStateTimeout("unchanged")]),
            ) as state_wait,
            self.assertRaises(EquipmentRepairSubmissionError),
        ):
            await client.repair_all()

        self.assertEqual(state_wait.await_count, 2)
        self.assertEqual(page.submit.clicks, 1)


if __name__ == "__main__":
    unittest.main()
