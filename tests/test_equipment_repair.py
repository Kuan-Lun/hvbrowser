import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from hvbrowser import equipment_repair as repair_module
from hvbrowser.equipment_repair import (
    EquipmentRepairClient,
    EquipmentRepairOutcome,
    EquipmentRepairPageError,
    EquipmentRepairSnapshot,
    EquipmentRepairStateChangedError,
    EquipmentRepairSubmissionError,
)
from hvbrowser.maintenance_navigation import (
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
)
from hvbrowser.realm import Realm
from hvbrowser.runtime import ZendriverOperationTimeout

_PERSISTENT_REPAIR_URL = (
    "https://hentaiverse.org/" "?s=Bazaar&ss=am&screen=repair&filter=equipped"
)
_ISEKAI_REPAIR_URL = (
    "https://hentaiverse.org/isekai/" "?s=Bazaar&ss=am&screen=repair&filter=equipped"
)
_NO_BATTLE_MARKERS = {
    "challenge": False,
    "completion": False,
    "nextFloor": False,
    "active": False,
}


def _element(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        get_position=AsyncMock(return_value=object()),
        mouse_move=AsyncMock(),
        mouse_click=AsyncMock(),
        click=AsyncMock(),
    )


def _navigation_elements() -> tuple[SimpleNamespace, SimpleNamespace]:
    return _element("The Armory"), _element("Repair")


def _equipment_state(
    count: int | None,
    *,
    empty: bool = False,
    row_count: int | None = 0,
) -> dict[str, object]:
    return {
        "hasEquipForm": True,
        "hasEquipList": True,
        "selectableCount": count,
        "empty": empty,
        "rowCount": row_count,
    }


def _client(
    xpath_results: list[object],
    *,
    equipment_states: list[object] | None = None,
    submit_disabled: list[object] | None = None,
    urls: list[object] | None = None,
    realm: Realm = Realm.PERSISTENT,
) -> tuple[
    EquipmentRepairClient,
    SimpleNamespace,
    SimpleNamespace,
    SimpleNamespace,
]:
    default_url = (
        _ISEKAI_REPAIR_URL if realm is Realm.ISEKAI else _PERSISTENT_REPAIR_URL
    )
    current_urls = list(urls or [default_url])
    state_results = iter(equipment_states or [])
    disabled_results = iter(submit_disabled or [])

    async def evaluate(script: str) -> object:
        if script == "window.location.href":
            if len(current_urls) > 1:
                return current_urls.pop(0)
            return current_urls[0]
        if script == repair_module._EQUIPMENT_STATE_SCRIPT:
            return next(state_results)
        if script == "document.getElementById('equipsubmit').disabled":
            return next(disabled_results)
        if "nextFloor" in script and "battle_main" in script:
            return dict(_NO_BATTLE_MARKERS)
        if "JSON.stringify" in script:
            return "{}"
        raise AssertionError(f"Unexpected evaluate script: {script!r}")

    page = SimpleNamespace(
        xpath=AsyncMock(side_effect=xpath_results),
        evaluate=AsyncMock(side_effect=evaluate),
        wait=AsyncMock(),
    )
    driver = SimpleNamespace(page=page, get=AsyncMock(), wait=AsyncMock())
    realm_navigator = SimpleNamespace(current=AsyncMock(return_value=realm))
    bazaar = _element("Bazaar")
    maintenance = SimpleNamespace(select_bazaar=AsyncMock(return_value=bazaar))
    return (
        EquipmentRepairClient(driver, realm_navigator, maintenance),
        driver,
        realm_navigator,
        maintenance,
    )


class EquipmentRepairClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_accepts_localized_text_and_returns_count(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("维修")
        count = _element("已选择 0 / 3 件符合条件的装备")
        client, driver, realm_navigator, maintenance = _client(
            [[armory], [repair], [selected], [selected], [count]],
            equipment_states=[_equipment_state(3, row_count=3)],
            realm=Realm.ISEKAI,
        )

        snapshot = await client.inspect()

        self.assertEqual(snapshot, EquipmentRepairSnapshot(Realm.ISEKAI, 3))
        realm_navigator.current.assert_awaited_once_with()
        maintenance.select_bazaar.assert_awaited_once_with(
            Realm.ISEKAI,
            navigate_first=False,
        )
        self.assertEqual(driver.wait.await_count, 2)
        driver.get.assert_not_awaited()

    async def test_armory_menu_is_polled_until_it_becomes_visible(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        count = _element()
        armory.get_position.side_effect = [None, None, object()]
        client, driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], [selected], [count]],
            equipment_states=[_equipment_state(0)],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        self.assertEqual(armory.get_position.await_count, 3)
        self.assertEqual(
            driver.page.wait.await_args_list,
            [
                call(repair_module._ARMORY_MENU_VISIBILITY_INTERVAL_SECONDS),
                call(repair_module._ARMORY_MENU_VISIBILITY_INTERVAL_SECONDS),
            ],
        )
        driver.get.assert_not_awaited()

    async def test_hidden_armory_menu_uses_direct_repair_fallback(self) -> None:
        armory = _element("The Armory")
        armory.get_position.return_value = None
        selected = _element("Repair")
        count = _element()
        client, driver, _realm, _maintenance = _client(
            [[armory], [selected], [selected], [count]],
            equipment_states=[_equipment_state(0)],
        )

        with patch.object(
            repair_module,
            "_ARMORY_MENU_VISIBILITY_ATTEMPTS",
            3,
        ):
            report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        self.assertEqual(armory.get_position.await_count, 3)
        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        self.assertEqual(driver.page.wait.await_count, 2)

    async def test_missing_repair_tab_uses_realm_scoped_direct_url(self) -> None:
        cases = (
            (Realm.PERSISTENT, _PERSISTENT_REPAIR_URL),
            (Realm.ISEKAI, _ISEKAI_REPAIR_URL),
        )
        for realm, expected_url in cases:
            with self.subTest(realm=realm):
                armory = _element("The Armory")
                selected = _element("Repair")
                count = _element()
                client, driver, _realm, _maintenance = _client(
                    [[armory], [], [selected], [selected], [count]],
                    equipment_states=[_equipment_state(0)],
                    realm=realm,
                )

                report = await client.repair_all()

                self.assertIs(
                    report.outcome,
                    EquipmentRepairOutcome.NO_REPAIR_NEEDED,
                )
                driver.get.assert_awaited_once_with(expected_url)
                driver.wait.assert_any_await(
                    armory.mouse_click,
                    ischangeurl=True,
                    owner=armory,
                    operation_timeout=15.0,
                )

    async def test_unchanged_repair_route_uses_direct_fallback(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        count = _element()
        organize_url = "https://hentaiverse.org/?s=Bazaar&ss=am"
        client, driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], [selected], [count]],
            equipment_states=[_equipment_state(0)],
            urls=[organize_url, _PERSISTENT_REPAIR_URL],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        driver.wait.assert_any_await(
            repair.click,
            ischangeurl=True,
            owner=repair,
            operation_timeout=15.0,
        )

    async def test_missing_selected_marker_after_click_uses_fallback(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        count = _element()
        client, driver, _realm, _maintenance = _client(
            [[armory], [repair], [], [selected], [selected], [count]],
            equipment_states=[_equipment_state(0)],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)

    async def test_direct_fallback_fails_once_when_repair_is_not_selected(
        self,
    ) -> None:
        armory, repair = _navigation_elements()
        client, driver, _realm, _maintenance = _client([[armory], [repair], [], []])

        with self.assertRaisesRegex(
            EquipmentRepairPageError,
            "selected-tab marker",
        ):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        self.assertEqual(driver.get.await_count, 1)

    async def test_organize_controls_are_never_used_as_repair_controls(self) -> None:
        armory, repair = _navigation_elements()
        organize_count = _element("Selected 0 of 4 matching")
        organize_submit = _element("Organize Equipment")
        client, driver, _realm, _maintenance = _client([[armory], [repair], [], []])

        with self.assertRaises(EquipmentRepairPageError):
            await client.repair_all()

        organize_count.mouse_click.assert_not_awaited()
        organize_submit.mouse_click.assert_not_awaited()
        self.assertEqual(driver.get.await_count, 1)
        evaluated_scripts = [
            item.args[0] for item in driver.page.evaluate.await_args_list
        ]
        self.assertNotIn(repair_module._EQUIPMENT_STATE_SCRIPT, evaluated_scripts)
        self.assertNotIn(
            "document.getElementById('equipsubmit').disabled",
            evaluated_scripts,
        )

    async def test_each_battle_state_blocks_after_direct_fallback(self) -> None:
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                armory = _element("The Armory")
                client, driver, _realm, _maintenance = _client([[armory], []])

                with (
                    patch.object(
                        repair_module,
                        "classify_maintenance_navigation_blocker",
                        new=AsyncMock(side_effect=[None, blocker]),
                    ),
                    self.assertRaises(MaintenanceNavigationBlockedError) as raised,
                ):
                    await client.repair_all()

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
                self.assertEqual(driver.wait.await_count, 1)

    async def test_each_battle_state_blocks_before_direct_fallback(self) -> None:
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                armory = _element("The Armory")
                client, driver, _realm, _maintenance = _client([[armory], []])

                with (
                    patch.object(
                        repair_module,
                        "classify_maintenance_navigation_blocker",
                        new=AsyncMock(return_value=blocker),
                    ),
                    self.assertRaises(MaintenanceNavigationBlockedError) as raised,
                ):
                    await client.repair_all()

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_not_awaited()

    async def test_each_battle_state_after_repair_click_does_not_fallback(
        self,
    ) -> None:
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                armory, repair = _navigation_elements()
                client, driver, _realm, _maintenance = _client([[armory], [repair]])

                with (
                    patch.object(
                        repair_module,
                        "classify_maintenance_navigation_blocker",
                        new=AsyncMock(return_value=blocker),
                    ),
                    self.assertRaises(MaintenanceNavigationBlockedError) as raised,
                ):
                    await client.repair_all()

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_not_awaited()

    async def test_unknown_battle_state_after_click_does_not_fallback(self) -> None:
        armory, repair = _navigation_elements()
        client, driver, _realm, _maintenance = _client([[armory], [repair]])

        with (
            patch.object(
                repair_module,
                "classify_maintenance_navigation_blocker",
                new=AsyncMock(side_effect=RuntimeError("unreadable DOM")),
            ),
            self.assertRaisesRegex(EquipmentRepairPageError, "battle state"),
        ):
            await client.repair_all()

        driver.get.assert_not_awaited()

    async def test_menu_click_timeout_is_terminal_without_safety_probe(self) -> None:
        armory = _element("The Armory")
        client, driver, _realm, _maintenance = _client([[armory]])
        timeout = ZendriverOperationTimeout(timeout_seconds=0.01)
        driver.wait.side_effect = timeout

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await client.repair_all()

        self.assertIs(raised.exception, timeout)
        driver.wait.assert_awaited_once_with(
            armory.mouse_click,
            ischangeurl=True,
            owner=armory,
            operation_timeout=15.0,
        )
        driver.get.assert_not_awaited()
        driver.page.evaluate.assert_not_awaited()

    async def test_direct_navigation_timeout_has_no_post_failure_probe(self) -> None:
        client, driver, _realm, _maintenance = _client([])
        timeout = ZendriverOperationTimeout(timeout_seconds=0.01)
        driver.get.side_effect = timeout

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await client._open_repair_directly(Realm.PERSISTENT)

        self.assertIs(raised.exception, timeout)
        driver.page.evaluate.assert_awaited_once()

    async def test_direct_get_error_prefers_detected_battle_block(self) -> None:
        armory = _element("The Armory")
        client, driver, _realm, _maintenance = _client([[armory], []])
        navigation_error = RuntimeError("navigation interrupted")
        driver.get.side_effect = navigation_error

        with (
            patch.object(
                repair_module,
                "classify_maintenance_navigation_blocker",
                new=AsyncMock(side_effect=[None, MaintenanceNavigationBlocker.ACTIVE]),
            ),
            self.assertRaises(MaintenanceNavigationBlockedError) as raised,
        ):
            await client.repair_all()

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        self.assertIs(raised.exception.__cause__, navigation_error)

    async def test_direct_fallback_rejects_wrong_realm(self) -> None:
        armory = _element("The Armory")
        client, driver, _realm, _maintenance = _client(
            [[armory], []],
            urls=[_PERSISTENT_REPAIR_URL],
            realm=Realm.ISEKAI,
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "wrong realm"):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_ISEKAI_REPAIR_URL)

    async def test_direct_fallback_rejects_wrong_repair_query(self) -> None:
        armory = _element("The Armory")
        organize_url = "https://hentaiverse.org/?s=Bazaar&ss=am&screen=organize"
        client, driver, _realm, _maintenance = _client(
            [[armory], []],
            urls=[organize_url],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "Repair route"):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        self.assertEqual(driver.page.xpath.await_count, 2)

    async def test_direct_fallback_rejects_non_equipped_filter(self) -> None:
        armory = _element("The Armory")
        new_filter_url = (
            "https://hentaiverse.org/" "?s=Bazaar&ss=am&screen=repair&filter=new"
        )
        client, driver, _realm, _maintenance = _client(
            [[armory], []],
            urls=[new_filter_url],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "Equipped filter"):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        self.assertEqual(driver.page.xpath.await_count, 2)

    async def test_direct_fallback_rejects_unexpected_path(self) -> None:
        armory = _element("The Armory")
        wrong_path = "https://hentaiverse.org/battle" "?s=Bazaar&ss=am&screen=repair"
        client, driver, _realm, _maintenance = _client(
            [[armory], []],
            urls=[wrong_path],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "unexpected path"):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        self.assertEqual(driver.page.xpath.await_count, 2)

    async def test_reordered_repair_query_is_accepted(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        count = _element()
        reordered_url = "https://hentaiverse.org/?screen=repair&extra=1&ss=am&s=Bazaar"
        client, driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], [selected], [count]],
            equipment_states=[_equipment_state(0)],
            urls=[reordered_url],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        driver.get.assert_not_awaited()

    async def test_no_matching_equipment_is_ready_without_submission(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        count = _element()
        client, driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], [selected], [count]],
            equipment_states=[_equipment_state(0)],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        self.assertTrue(report.ready)
        self.assertEqual(report.before.repair_count, 0)
        self.assertEqual(report.after, report.before)
        evaluated_scripts = [
            item.args[0] for item in driver.page.evaluate.await_args_list
        ]
        self.assertNotIn(
            "document.getElementById('equipsubmit').disabled",
            evaluated_scripts,
        )

    async def test_empty_equipment_list_without_count_is_zero(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        client, _driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], [selected], []],
            equipment_states=[_equipment_state(None, empty=True)],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        self.assertEqual(report.before.repair_count, 0)

    async def test_missing_count_without_empty_list_fails_closed(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        client, _driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], [selected], []],
            equipment_states=[_equipment_state(None)],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "empty equipment"):
            await client.repair_all()

    async def test_empty_marker_with_rows_fails_closed(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        client, _driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], [selected], []],
            equipment_states=[_equipment_state(None, empty=True, row_count=1)],
        )

        with self.assertRaises(EquipmentRepairPageError):
            await client.repair_all()

    async def test_repair_all_confirms_zero_remaining_items(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        before = _element()
        submit = _element()
        after = _element()
        client, driver, _realm, _maintenance = _client(
            [
                [armory],
                [repair],
                [selected],
                [selected],
                [before],
                [submit],
                [selected],
                [after],
            ],
            equipment_states=[
                _equipment_state(3, row_count=3),
                _equipment_state(0),
            ],
            submit_disabled=[False],
        )

        report = await client.repair_all(EquipmentRepairSnapshot(Realm.PERSISTENT, 3))

        self.assertIs(report.outcome, EquipmentRepairOutcome.REPAIRED)
        self.assertTrue(report.ready)
        self.assertEqual(report.after.repair_count, 0)
        submit.mouse_click.assert_awaited_once_with()
        driver.page.wait.assert_awaited_once_with(2)
        self.assertEqual(driver.wait.await_count, 3)

    async def test_disabled_submit_is_rechecked_from_fresh_state(self) -> None:
        armory_1, repair_1 = _navigation_elements()
        armory_2, repair_2 = _navigation_elements()
        selected_1 = _element("Repair")
        selected_2 = _element("Repair")
        before = _element()
        first_submit = _element()
        fresh = _element()
        second_submit = _element()
        client, driver, _realm, maintenance = _client(
            [
                [armory_1],
                [repair_1],
                [selected_1],
                [selected_1],
                [before],
                [first_submit],
                [armory_2],
                [repair_2],
                [selected_2],
                [selected_2],
                [fresh],
                [second_submit],
            ],
            equipment_states=[_equipment_state(2), _equipment_state(2)],
            submit_disabled=[True, True],
        )

        with patch.object(repair_module.logger, "isEnabledFor", return_value=False):
            report = await client.repair_all()

        self.assertIs(
            report.outcome,
            EquipmentRepairOutcome.MATERIALS_UNAVAILABLE,
        )
        self.assertFalse(report.ready)
        self.assertEqual(maintenance.select_bazaar.await_count, 2)
        first_submit.mouse_click.assert_not_awaited()
        second_submit.mouse_click.assert_not_awaited()
        driver.page.wait.assert_not_awaited()

    async def test_stale_disabled_state_can_recover_and_submit(self) -> None:
        armory_1, repair_1 = _navigation_elements()
        armory_2, repair_2 = _navigation_elements()
        selected_1 = _element("Repair")
        selected_2 = _element("Repair")
        before = _element()
        first_submit = _element()
        fresh = _element()
        second_submit = _element()
        after = _element()
        client, _driver, _realm, maintenance = _client(
            [
                [armory_1],
                [repair_1],
                [selected_1],
                [selected_1],
                [before],
                [first_submit],
                [armory_2],
                [repair_2],
                [selected_2],
                [selected_2],
                [fresh],
                [second_submit],
                [selected_2],
                [after],
            ],
            equipment_states=[
                _equipment_state(2),
                _equipment_state(2),
                _equipment_state(0),
            ],
            submit_disabled=[True, False],
        )

        with patch.object(repair_module.logger, "isEnabledFor", return_value=False):
            report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.REPAIRED)
        self.assertTrue(report.ready)
        self.assertEqual(maintenance.select_bazaar.await_count, 2)
        first_submit.mouse_click.assert_not_awaited()
        second_submit.mouse_click.assert_awaited_once_with()

    async def test_fresh_retry_detects_concurrent_state_change(self) -> None:
        armory_1, repair_1 = _navigation_elements()
        armory_2, repair_2 = _navigation_elements()
        selected_1 = _element("Repair")
        selected_2 = _element("Repair")
        client, _driver, _realm, _maintenance = _client(
            [
                [armory_1],
                [repair_1],
                [selected_1],
                [selected_1],
                [_element()],
                [_element()],
                [armory_2],
                [repair_2],
                [selected_2],
                [selected_2],
                [_element()],
            ],
            equipment_states=[_equipment_state(2), _equipment_state(1)],
            submit_disabled=[True],
        )

        with patch.object(repair_module.logger, "isEnabledFor", return_value=False):
            with self.assertRaises(EquipmentRepairStateChangedError):
                await client.repair_all()

    async def test_invalid_count_state_is_a_typed_page_error(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        client, _driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], [selected], [_element()]],
            equipment_states=[_equipment_state(None)],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "state is invalid"):
            await client.inspect()

    async def test_boolean_count_state_is_rejected(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        invalid_state = _equipment_state(None)
        invalid_state["selectableCount"] = True
        client, _driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], [selected], [_element()]],
            equipment_states=[invalid_state],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "state is invalid"):
            await client.inspect()

    async def test_missing_submit_is_a_typed_page_error(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        client, _driver, _realm, _maintenance = _client(
            [
                [armory],
                [repair],
                [selected],
                [selected],
                [_element()],
                [],
            ],
            equipment_states=[_equipment_state(1)],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "submit button"):
            await client.repair_all()

    async def test_disappearing_count_with_empty_list_confirms_repair(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        submit = _element()
        client, _driver, _realm, _maintenance = _client(
            [
                [armory],
                [repair],
                [selected],
                [selected],
                [_element()],
                [submit],
                [selected],
                [],
            ],
            equipment_states=[
                _equipment_state(1),
                _equipment_state(None, empty=True),
            ],
            submit_disabled=[False],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.REPAIRED)
        self.assertEqual(report.after.repair_count, 0)

    async def test_wrong_page_after_navigation_fails_without_fallback(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        client, driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], []]
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "selected-tab marker"):
            await client.inspect()

        driver.get.assert_not_awaited()

    async def test_wrong_post_submit_page_is_an_unknown_outcome(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        client, _driver, _realm, _maintenance = _client(
            [
                [armory],
                [repair],
                [selected],
                [selected],
                [_element()],
                [_element()],
                [],
            ],
            equipment_states=[_equipment_state(1)],
            submit_disabled=[False],
        )

        with self.assertRaisesRegex(
            EquipmentRepairSubmissionError,
            "Unable to confirm",
        ):
            await client.repair_all()

    async def test_expected_snapshot_prevents_stale_submission(self) -> None:
        armory, repair = _navigation_elements()
        selected = _element("Repair")
        client, driver, _realm, _maintenance = _client(
            [[armory], [repair], [selected], [selected], [_element()]],
            equipment_states=[_equipment_state(1)],
        )

        with self.assertRaises(EquipmentRepairStateChangedError):
            await client.repair_all(EquipmentRepairSnapshot(Realm.PERSISTENT, 2))

        evaluated_scripts = [
            item.args[0] for item in driver.page.evaluate.await_args_list
        ]
        self.assertNotIn(
            "document.getElementById('equipsubmit').disabled",
            evaluated_scripts,
        )


if __name__ == "__main__":
    unittest.main()
