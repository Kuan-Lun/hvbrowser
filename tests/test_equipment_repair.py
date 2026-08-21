import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    MaintenanceNavigationObservation,
)
from hvbrowser.realm import Realm
from hvbrowser.runtime import ZendriverOperationTimeout

_PERSISTENT_REPAIR_URL = (
    "https://hentaiverse.org/" "?s=Bazaar&ss=am&screen=repair&filter=equipped"
)
_ISEKAI_REPAIR_URL = (
    "https://hentaiverse.org/isekai/" "?s=Bazaar&ss=am&screen=repair&filter=equipped"
)
_PERSISTENT_ROOT_URL = "https://hentaiverse.org/"
_ISEKAI_ROOT_URL = "https://hentaiverse.org/isekai/"


def _observation(
    *,
    realm: Realm = Realm.PERSISTENT,
    blocker: MaintenanceNavigationBlocker | None = None,
    url: str | None = None,
) -> MaintenanceNavigationObservation:
    if url is None:
        url = _ISEKAI_ROOT_URL if realm is Realm.ISEKAI else _PERSISTENT_ROOT_URL
    return MaintenanceNavigationObservation(url, realm, blocker)


def _element(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        mouse_click=AsyncMock(),
    )


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
]:
    landing_urls = list(urls or [])
    current_url = _ISEKAI_ROOT_URL if realm is Realm.ISEKAI else _PERSISTENT_ROOT_URL
    state_results = iter(equipment_states or [])
    disabled_results = iter(submit_disabled or [])

    async def evaluate(script: str) -> object:
        if script == repair_module._EQUIPMENT_STATE_SCRIPT:
            return next(state_results)
        if script == "document.getElementById('equipsubmit').disabled":
            return next(disabled_results)
        if "nextFloor" in script and "battle_main" in script:
            return {
                "url": current_url,
                "challenge": False,
                "completion": False,
                "nextFloor": False,
                "active": False,
            }
        if "JSON.stringify" in script:
            return "{}"
        raise AssertionError(f"Unexpected evaluate script: {script!r}")

    async def get(url: str) -> None:
        nonlocal current_url
        current_url = landing_urls.pop(0) if landing_urls else url

    page = SimpleNamespace(
        xpath=AsyncMock(side_effect=xpath_results),
        evaluate=AsyncMock(side_effect=evaluate),
        wait=AsyncMock(),
    )
    driver = SimpleNamespace(
        page=page,
        get=AsyncMock(side_effect=get),
        wait=AsyncMock(),
    )
    realm_navigator = SimpleNamespace(current=AsyncMock(return_value=realm))
    return (
        EquipmentRepairClient(driver, realm_navigator),
        driver,
        realm_navigator,
    )


class EquipmentRepairClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_accepts_localized_text_and_returns_count(self) -> None:
        selected = _element("维修")
        count = _element("已选择 0 / 3 件符合条件的装备")
        client, driver, realm_navigator = _client(
            [[selected], [selected], [count]],
            equipment_states=[_equipment_state(3, row_count=3)],
            realm=Realm.ISEKAI,
        )

        snapshot = await client.inspect()

        self.assertEqual(snapshot, EquipmentRepairSnapshot(Realm.ISEKAI, 3))
        realm_navigator.current.assert_awaited_once_with()
        driver.get.assert_awaited_once_with(_ISEKAI_REPAIR_URL)
        driver.wait.assert_not_awaited()

    async def test_repair_navigation_does_not_probe_menu_visibility(self) -> None:
        selected = _element("Repair")
        count = _element()
        client, driver, _realm = _client(
            [[selected], [selected], [count]],
            equipment_states=[_equipment_state(0)],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        driver.page.wait.assert_not_awaited()
        driver.wait.assert_not_awaited()

    async def test_direct_navigation_uses_single_canonical_get(self) -> None:
        selected = _element("Repair")
        count = _element()
        client, driver, _realm = _client(
            [[selected], [selected], [count]],
            equipment_states=[_equipment_state(0)],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        self.assertEqual(driver.get.await_count, 1)

    async def test_each_realm_uses_realm_scoped_direct_url(self) -> None:
        cases = (
            (Realm.PERSISTENT, _PERSISTENT_REPAIR_URL),
            (Realm.ISEKAI, _ISEKAI_REPAIR_URL),
        )
        for realm, expected_url in cases:
            with self.subTest(realm=realm):
                selected = _element("Repair")
                count = _element()
                client, driver, _realm = _client(
                    [[selected], [selected], [count]],
                    equipment_states=[_equipment_state(0)],
                    realm=realm,
                )

                report = await client.repair_all()

                self.assertIs(
                    report.outcome,
                    EquipmentRepairOutcome.NO_REPAIR_NEEDED,
                )
                driver.get.assert_awaited_once_with(expected_url)
                driver.wait.assert_not_awaited()

    async def test_direct_repair_route_is_verified_after_navigation(self) -> None:
        selected = _element("Repair")
        count = _element()
        client, driver, _realm = _client(
            [[selected], [selected], [count]],
            equipment_states=[_equipment_state(0)],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        self.assertEqual(driver.page.xpath.await_count, 3)

    async def test_missing_selected_marker_after_direct_navigation_is_terminal(
        self,
    ) -> None:
        client, driver, _realm = _client([[]])

        with self.assertRaisesRegex(EquipmentRepairPageError, "selected-tab marker"):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        self.assertTrue(
            any(
                "window.location.href" in call.args[0] and "battle_main" in call.args[0]
                for call in driver.page.evaluate.await_args_list
            )
        )
        self.assertNotIn(
            repair_module._EQUIPMENT_STATE_SCRIPT,
            [call.args[0] for call in driver.page.evaluate.await_args_list],
        )

    async def test_selected_marker_read_error_is_not_retried(
        self,
    ) -> None:
        client, driver, _realm = _client([RuntimeError("selected marker read failed")])

        with self.assertRaisesRegex(
            EquipmentRepairPageError,
            "Unable to verify",
        ):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        self.assertEqual(driver.get.await_count, 1)

    async def test_organize_controls_are_never_used_as_repair_controls(self) -> None:
        organize_count = _element("Selected 0 of 4 matching")
        organize_submit = _element("Organize Equipment")
        client, driver, _realm = _client([[]])

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

    async def test_each_battle_state_blocks_after_direct_landing(self) -> None:
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                client, driver, _realm = _client([])

                with (
                    patch.object(
                        repair_module,
                        "observe_maintenance_navigation",
                        new=AsyncMock(
                            side_effect=[
                                _observation(),
                                _observation(blocker=blocker),
                            ]
                        ),
                    ),
                    self.assertRaises(MaintenanceNavigationBlockedError) as raised,
                ):
                    await client.repair_all()

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)

    async def test_each_battle_state_blocks_before_direct_navigation(self) -> None:
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                client, driver, _realm = _client([])

                with (
                    patch.object(
                        repair_module,
                        "observe_maintenance_navigation",
                        new=AsyncMock(return_value=_observation(blocker=blocker)),
                    ),
                    self.assertRaises(MaintenanceNavigationBlockedError) as raised,
                ):
                    await client.repair_all()

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_not_awaited()

    async def test_untrusted_identity_wins_over_marker_before_and_after_get(
        self,
    ) -> None:
        unsafe_observations = (
            MaintenanceNavigationObservation(
                "https://example.test/",
                None,
                MaintenanceNavigationBlocker.ACTIVE,
            ),
            MaintenanceNavigationObservation(
                "https://hentaiverse.org:0/",
                None,
                MaintenanceNavigationBlocker.ACTIVE,
            ),
            _observation(
                realm=Realm.ISEKAI,
                blocker=MaintenanceNavigationBlocker.ACTIVE,
            ),
            _observation(
                url="https://hentaiverse.org/unexpected",
                blocker=MaintenanceNavigationBlocker.ACTIVE,
            ),
        )
        for after_get in (False, True):
            for unsafe in unsafe_observations:
                with self.subTest(after_get=after_get, url=unsafe.url):
                    client, driver, _realm = _client([])
                    observations = [_observation(), unsafe] if after_get else [unsafe]

                    with (
                        patch.object(
                            repair_module,
                            "observe_maintenance_navigation",
                            new=AsyncMock(side_effect=observations),
                        ),
                        self.assertRaises(EquipmentRepairPageError) as raised,
                    ):
                        await client.repair_all()

                    self.assertNotIsInstance(
                        raised.exception,
                        MaintenanceNavigationBlockedError,
                    )
                    if after_get:
                        driver.get.assert_awaited_once()
                    else:
                        driver.get.assert_not_awaited()

    async def test_each_battle_state_appearing_before_inspection_stops_safely(
        self,
    ) -> None:
        for blocker in MaintenanceNavigationBlocker:
            with self.subTest(blocker=blocker):
                selected = _element("Repair")
                client, driver, _realm = _client([[selected]])

                with (
                    patch.object(
                        repair_module,
                        "observe_maintenance_navigation",
                        new=AsyncMock(
                            side_effect=[
                                _observation(),
                                _observation(url=_PERSISTENT_REPAIR_URL),
                                _observation(
                                    url=_PERSISTENT_REPAIR_URL,
                                    blocker=blocker,
                                ),
                            ]
                        ),
                    ),
                    self.assertRaises(MaintenanceNavigationBlockedError) as raised,
                ):
                    await client.repair_all()

                self.assertIs(raised.exception.blocker, blocker)
                driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
                self.assertNotIn(
                    repair_module._EQUIPMENT_STATE_SCRIPT,
                    [call.args[0] for call in driver.page.evaluate.await_args_list],
                )

    async def test_unknown_battle_state_after_direct_landing_is_terminal(self) -> None:
        client, driver, _realm = _client([])

        with (
            patch.object(
                repair_module,
                "observe_maintenance_navigation",
                new=AsyncMock(
                    side_effect=[_observation(), RuntimeError("unreadable DOM")]
                ),
            ),
            self.assertRaisesRegex(EquipmentRepairPageError, "battle state"),
        ):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)

    async def test_public_direct_timeout_is_terminal_without_post_failure_probe(
        self,
    ) -> None:
        client, driver, _realm = _client([])
        timeout = ZendriverOperationTimeout(timeout_seconds=0.01)
        driver.get.side_effect = timeout

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await client.repair_all()

        self.assertIs(raised.exception, timeout)
        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        driver.page.evaluate.assert_awaited_once()
        driver.page.xpath.assert_not_awaited()

    async def test_direct_navigation_timeout_has_no_post_failure_probe(self) -> None:
        client, driver, _realm = _client([])
        timeout = ZendriverOperationTimeout(timeout_seconds=0.01)
        driver.get.side_effect = timeout

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await client._open_repair_directly(Realm.PERSISTENT)

        self.assertIs(raised.exception, timeout)
        driver.page.evaluate.assert_awaited_once()

    async def test_direct_get_error_prefers_detected_battle_block(self) -> None:
        client, driver, _realm = _client([])
        navigation_error = RuntimeError("navigation interrupted")
        driver.get.side_effect = navigation_error

        with (
            patch.object(
                repair_module,
                "observe_maintenance_navigation",
                new=AsyncMock(
                    side_effect=[
                        _observation(),
                        _observation(blocker=MaintenanceNavigationBlocker.ACTIVE),
                    ]
                ),
            ),
            self.assertRaises(MaintenanceNavigationBlockedError) as raised,
        ):
            await client.repair_all()

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        self.assertIs(raised.exception.__cause__, navigation_error)

    async def test_direct_navigation_rejects_wrong_realm(self) -> None:
        client, driver, _realm = _client(
            [],
            urls=[_PERSISTENT_REPAIR_URL],
            realm=Realm.ISEKAI,
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "wrong realm"):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_ISEKAI_REPAIR_URL)

    async def test_direct_navigation_rejects_wrong_repair_query(self) -> None:
        organize_url = "https://hentaiverse.org/?s=Bazaar&ss=am&screen=organize"
        client, driver, _realm = _client(
            [],
            urls=[organize_url],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "Repair route"):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        driver.page.xpath.assert_not_awaited()

    async def test_direct_navigation_rejects_non_equipped_filter(self) -> None:
        new_filter_url = (
            "https://hentaiverse.org/" "?s=Bazaar&ss=am&screen=repair&filter=new"
        )
        client, driver, _realm = _client(
            [],
            urls=[new_filter_url],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "Equipped filter"):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        driver.page.xpath.assert_not_awaited()

    async def test_direct_navigation_rejects_unexpected_path(self) -> None:
        wrong_path = "https://hentaiverse.org/battle" "?s=Bazaar&ss=am&screen=repair"
        client, driver, _realm = _client(
            [],
            urls=[wrong_path],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "unexpected path"):
            await client.repair_all()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)
        driver.page.xpath.assert_not_awaited()

    async def test_reordered_repair_query_is_accepted(self) -> None:
        selected = _element("Repair")
        count = _element()
        reordered_url = "https://hentaiverse.org/?screen=repair&extra=1&ss=am&s=Bazaar"
        client, driver, _realm = _client(
            [[selected], [selected], [count]],
            equipment_states=[_equipment_state(0)],
            urls=[reordered_url],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)

    async def test_no_matching_equipment_is_ready_without_submission(self) -> None:
        selected = _element("Repair")
        count = _element()
        client, driver, _realm = _client(
            [[selected], [selected], [count]],
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
        selected = _element("Repair")
        client, _driver, _realm = _client(
            [[selected], [selected], []],
            equipment_states=[_equipment_state(None, empty=True)],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        self.assertEqual(report.before.repair_count, 0)

    async def test_missing_count_without_empty_list_fails_closed(self) -> None:
        selected = _element("Repair")
        client, _driver, _realm = _client(
            [[selected], [selected], []],
            equipment_states=[_equipment_state(None)],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "empty equipment"):
            await client.repair_all()

    async def test_empty_marker_with_rows_fails_closed(self) -> None:
        selected = _element("Repair")
        client, _driver, _realm = _client(
            [[selected], [selected], []],
            equipment_states=[_equipment_state(None, empty=True, row_count=1)],
        )

        with self.assertRaises(EquipmentRepairPageError):
            await client.repair_all()

    async def test_repair_all_confirms_zero_remaining_items(self) -> None:
        selected = _element("Repair")
        before = _element()
        submit = _element()
        after = _element()
        client, driver, _realm = _client(
            [
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
        driver.wait.assert_awaited_once_with(
            before.mouse_click,
            ischangeurl=False,
            owner=before,
            operation_timeout=15.0,
        )

    async def test_disabled_submit_is_rechecked_from_fresh_state(self) -> None:
        selected_1 = _element("Repair")
        selected_2 = _element("Repair")
        before = _element()
        first_submit = _element()
        fresh = _element()
        second_submit = _element()
        client, driver, _realm = _client(
            [
                [selected_1],
                [selected_1],
                [before],
                [first_submit],
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
        self.assertEqual(driver.get.await_count, 2)
        first_submit.mouse_click.assert_not_awaited()
        second_submit.mouse_click.assert_not_awaited()
        driver.page.wait.assert_not_awaited()

    async def test_stale_disabled_state_can_recover_and_submit(self) -> None:
        selected_1 = _element("Repair")
        selected_2 = _element("Repair")
        before = _element()
        first_submit = _element()
        fresh = _element()
        second_submit = _element()
        after = _element()
        client, _driver, _realm = _client(
            [
                [selected_1],
                [selected_1],
                [before],
                [first_submit],
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
        first_submit.mouse_click.assert_not_awaited()
        second_submit.mouse_click.assert_awaited_once_with()

    async def test_fresh_retry_detects_concurrent_state_change(self) -> None:
        selected_1 = _element("Repair")
        selected_2 = _element("Repair")
        client, _driver, _realm = _client(
            [
                [selected_1],
                [selected_1],
                [_element()],
                [_element()],
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
        selected = _element("Repair")
        client, _driver, _realm = _client(
            [[selected], [selected], [_element()]],
            equipment_states=[_equipment_state(None)],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "state is invalid"):
            await client.inspect()

    async def test_boolean_count_state_is_rejected(self) -> None:
        selected = _element("Repair")
        invalid_state = _equipment_state(None)
        invalid_state["selectableCount"] = True
        client, _driver, _realm = _client(
            [[selected], [selected], [_element()]],
            equipment_states=[invalid_state],
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "state is invalid"):
            await client.inspect()

    async def test_missing_submit_is_a_typed_page_error(self) -> None:
        selected = _element("Repair")
        client, _driver, _realm = _client(
            [
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
        selected = _element("Repair")
        submit = _element()
        client, _driver, _realm = _client(
            [
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
        selected = _element("Repair")
        client, driver, _realm = _client([[selected], []])

        with self.assertRaisesRegex(EquipmentRepairPageError, "selected-tab marker"):
            await client.inspect()

        driver.get.assert_awaited_once_with(_PERSISTENT_REPAIR_URL)

    async def test_wrong_post_submit_page_is_an_unknown_outcome(self) -> None:
        selected = _element("Repair")
        client, _driver, _realm = _client(
            [
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
        selected = _element("Repair")
        client, driver, _realm = _client(
            [[selected], [selected], [_element()]],
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
