import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hvbrowser import (
    HENTAIVERSE_ISEKAI_ROOT_URL,
    HVDriver,
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
)
from hvbrowser import hv as hv_module


def _maintenance_markers(*, active: bool = False) -> dict[str, bool]:
    return {
        "challenge": False,
        "completion": False,
        "nextFloor": False,
        "active": active,
    }


class _LocationPage:
    def __init__(self, location: object) -> None:
        self.location = location

    async def evaluate(self, expression: str) -> object:
        if expression != "window.location.href":
            raise AssertionError(f"Unexpected expression: {expression}")
        return self.location


def _driver_at(location: object) -> HVDriver:
    driver = object.__new__(HVDriver)
    driver.page = _LocationPage(location)
    return driver


def _repair_driver(
    location: str,
    *,
    marker_payloads: list[dict[str, bool]],
) -> tuple[HVDriver, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    async def evaluate(expression: str) -> object:
        if expression == "window.location.href":
            return location
        if "riddlesubmit" in expression and "finishbattle.png" in expression:
            return marker_payloads.pop(0)
        raise AssertionError(f"Unexpected expression: {expression}")

    bazaar = SimpleNamespace(mouse_move=AsyncMock())
    armory = SimpleNamespace(mouse_move=AsyncMock(), mouse_click=AsyncMock())
    repair = SimpleNamespace(click=AsyncMock())
    page = SimpleNamespace(
        evaluate=AsyncMock(side_effect=evaluate),
        select=AsyncMock(),
        xpath=AsyncMock(side_effect=[[armory], [repair]]),
    )
    driver = object.__new__(HVDriver)
    driver.page = page
    driver.goisekai = AsyncMock()
    driver.gohomepage = AsyncMock()
    driver.wait = AsyncMock()
    return driver, bazaar, armory, repair


class HVDriverRealmTests(unittest.IsolatedAsyncioTestCase):
    async def test_isekai_is_detected_from_the_current_url_path(self) -> None:
        locations = (
            "https://hentaiverse.org/isekai",
            "https://hentaiverse.org/isekai/",
            "https://hentaiverse.org:443/isekai/?s=Battle&ss=ba&round=3",
        )

        for location in locations:
            with self.subTest(location=location):
                self.assertTrue(await _driver_at(location).is_isekai)

    async def test_persistent_and_isekai_lookalikes_are_not_isekai(self) -> None:
        locations = (
            "https://hentaiverse.org/?s=Battle&ss=ba",
            "https://hentaiverse.org/?next=/isekai/",
            "https://hentaiverse.org/#isekai",
            "https://hentaiverse.org/not-isekai/",
            "https://hentaiverse.org/isekaiish/",
            "https://hentaiverse.org/foo/isekai/",
        )

        for location in locations:
            with self.subTest(location=location):
                self.assertFalse(await _driver_at(location).is_isekai)

    async def test_realm_detection_rejects_an_unexpected_origin(self) -> None:
        locations = (
            "http://hentaiverse.org/isekai/",
            "https://hentaiverse.org:444/isekai/",
            "https://example.test/isekai/",
            None,
        )

        for location in locations:
            with self.subTest(location=location):
                with self.assertRaisesRegex(RuntimeError, "determine realm"):
                    await _driver_at(location).is_isekai

    async def test_goisekai_navigates_to_the_canonical_root(self) -> None:
        driver = object.__new__(HVDriver)
        driver.get = AsyncMock()

        with self.assertLogs("hvbrowser.hv", level="DEBUG") as captured:
            await driver.goisekai()

        driver.get.assert_awaited_once_with(HENTAIVERSE_ISEKAI_ROOT_URL)
        self.assertEqual(len(captured.output), 1)
        self.assertTrue(captured.output[0].startswith("DEBUG:hvbrowser.hv:"))
        self.assertIn(HENTAIVERSE_ISEKAI_ROOT_URL, captured.output[0])


class HVDriverLevelTests(unittest.IsolatedAsyncioTestCase):
    async def test_level_is_parsed_from_the_visible_readout_text(self) -> None:
        for text, expected in (
            ("PFUDOR Lv.500", 500),
            ("Lv.1", 1),
            ("  Nightmare\tLv. 365\n", 365),
        ):
            with self.subTest(text=text):
                level_readout = SimpleNamespace(text=text)
                driver = object.__new__(HVDriver)
                driver.page = SimpleNamespace(
                    select=AsyncMock(return_value=level_readout)
                )

                self.assertEqual(await driver.get_level(), expected)
                driver.page.select.assert_awaited_once_with(
                    "#level_readout",
                    timeout=5,
                )

    async def test_missing_level_readout_raises_value_error(self) -> None:
        driver = object.__new__(HVDriver)
        driver.page = SimpleNamespace(
            select=AsyncMock(side_effect=TimeoutError("missing"))
        )

        with self.assertRaisesRegex(ValueError, "find level readout"):
            await driver.get_level()

    async def test_malformed_level_readout_raises_value_error(self) -> None:
        malformed_texts = (
            "PFUDOR 500",
            "PFUDOR lv.500",
            "PFUDOR Lv.-1",
            "PFUDOR Lv.500 EXP",
            "PFUDOR Lv.500.0",
            "PFUDOR Lv.\uff15\uff10\uff10",
        )

        for text in malformed_texts:
            with self.subTest(text=text):
                driver = object.__new__(HVDriver)
                driver.page = SimpleNamespace(
                    select=AsyncMock(return_value=SimpleNamespace(text=text))
                )

                with self.assertRaisesRegex(ValueError, "parse level"):
                    await driver.get_level()


class HVDriverStaminaLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_availability_check_is_debug_not_info(self) -> None:
        stamina_readout = SimpleNamespace(mouse_move=AsyncMock())
        driver = object.__new__(HVDriver)
        driver.page = SimpleNamespace(
            select=AsyncMock(return_value=stamina_readout),
            xpath=AsyncMock(return_value=[]),
        )

        with (
            patch.object(hv_module.logger, "debug") as debug,
            patch.object(hv_module.logger, "info") as info,
        ):
            recovered = await HVDriver.recoverstamina(driver)

        self.assertFalse(recovered)
        debug.assert_any_call(
            "Checking USR RESTORATIVE availability for stamina recovery"
        )
        info.assert_not_called()

    async def test_server_error_text_is_rendered_on_one_log_line(self) -> None:
        stamina_readout = SimpleNamespace(mouse_move=AsyncMock())
        restorative = SimpleNamespace(
            mouse_move=AsyncMock(),
            mouse_click=AsyncMock(),
        )
        error_message = SimpleNamespace(
            text="first line\nsecond line\rthird line",
            click=AsyncMock(),
        )
        driver = object.__new__(HVDriver)
        driver.page = SimpleNamespace(
            select=AsyncMock(return_value=stamina_readout),
            xpath=AsyncMock(side_effect=[[restorative], [error_message]]),
            wait=AsyncMock(),
        )

        with self.assertLogs("hvbrowser.hv", level="WARNING") as captured:
            recovered = await HVDriver.recoverstamina(driver)

        self.assertFalse(recovered)
        self.assertEqual(len(captured.output), 1)
        self.assertNotIn("\n", captured.output[0])
        self.assertNotIn("\r", captured.output[0])
        self.assertIn(r"first line\nsecond line\rthird line", captured.output[0])


class HVDriverRepairNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_isekai_timeout_reloads_only_isekai_realm(self) -> None:
        driver, bazaar, armory, repair = _repair_driver(
            "https://hentaiverse.org/isekai/?s=Bazaar&ss=es",
            marker_payloads=[_maintenance_markers()] * 4,
        )
        driver.page.select.side_effect = [TimeoutError("missing Bazaar"), bazaar]

        opened = await HVDriver._goto_repair_tab(driver)

        self.assertTrue(opened)
        driver.goisekai.assert_awaited_once_with()
        driver.gohomepage.assert_not_awaited()
        bazaar.mouse_move.assert_awaited_once_with()
        armory.mouse_move.assert_awaited_once_with()
        self.assertEqual(driver.wait.await_count, 2)
        repair.click.assert_not_awaited()

    async def test_persistent_timeout_reloads_only_persistent_realm(self) -> None:
        driver, bazaar, armory, _repair = _repair_driver(
            "https://hentaiverse.org/?s=Bazaar&ss=es",
            marker_payloads=[_maintenance_markers()] * 4,
        )
        driver.page.select.side_effect = [TimeoutError("missing Bazaar"), bazaar]

        opened = await HVDriver._goto_repair_tab(driver)

        self.assertTrue(opened)
        driver.gohomepage.assert_awaited_once_with(force=True)
        driver.goisekai.assert_not_awaited()
        armory.mouse_click.assert_not_awaited()
        self.assertEqual(driver.wait.await_count, 2)

    async def test_battle_marker_blocks_without_reload_or_click(self) -> None:
        driver, _bazaar, _armory, _repair = _repair_driver(
            "https://hentaiverse.org/isekai/?s=Battle&ss=ba",
            marker_payloads=[_maintenance_markers(active=True)],
        )

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await HVDriver._goto_repair_tab(driver)

        self.assertIs(raised.exception.blocker, MaintenanceNavigationBlocker.ACTIVE)
        driver.goisekai.assert_not_awaited()
        driver.gohomepage.assert_not_awaited()
        driver.page.select.assert_not_awaited()
        driver.page.xpath.assert_not_awaited()
        driver.wait.assert_not_awaited()


class HVDriverRepairLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_routine_check_is_debug_not_info(self) -> None:
        driver = object.__new__(HVDriver)
        driver._goto_repair_tab = AsyncMock(return_value=False)

        with (
            patch.object(hv_module.logger, "debug") as debug,
            patch.object(hv_module.logger, "info") as info,
        ):
            repaired = await HVDriver.repairequipment(driver)

        self.assertTrue(repaired)
        debug.assert_called_once_with("Checking equipped gear for repairs")
        info.assert_not_called()

    async def test_initial_disabled_submit_is_debug_until_fresh_verification(
        self,
    ) -> None:
        driver = object.__new__(HVDriver)
        equipcount = SimpleNamespace(
            text="Selected 0 of 1 matching",
            mouse_click=AsyncMock(),
        )
        submit = object()
        debug_state = '{"selected_count":1}'
        driver.wait = AsyncMock()
        driver.page = SimpleNamespace(
            xpath=AsyncMock(side_effect=[[equipcount], [submit]]),
            evaluate=AsyncMock(side_effect=[True, debug_state]),
        )

        with (
            patch.object(hv_module.logger, "isEnabledFor", return_value=True),
            patch.object(hv_module.logger, "debug") as debug,
            patch.object(hv_module.logger, "warning") as warning,
        ):
            disabled, submit_elements = (
                await HVDriver._select_all_and_check_repair_submit(
                    driver,
                    [equipcount],
                )
            )

        self.assertTrue(disabled)
        self.assertEqual(submit_elements, [submit])
        debug.assert_any_call(
            "Repair submit disabled at current observation: state=%s",
            debug_state,
        )
        warning.assert_not_called()

    async def test_disabled_submit_skips_debug_probe_when_debug_is_off(self) -> None:
        driver = object.__new__(HVDriver)
        equipcount = SimpleNamespace(
            text="Selected 0 of 1 matching",
            mouse_click=AsyncMock(),
        )
        submit = object()
        driver.wait = AsyncMock()
        driver.page = SimpleNamespace(
            xpath=AsyncMock(side_effect=[[equipcount], [submit]]),
            evaluate=AsyncMock(return_value=True),
        )

        with (
            patch.object(hv_module.logger, "isEnabledFor", return_value=False),
            patch.object(hv_module.logger, "debug") as debug,
        ):
            disabled, submit_elements = (
                await HVDriver._select_all_and_check_repair_submit(
                    driver,
                    [equipcount],
                )
            )

        self.assertTrue(disabled)
        self.assertEqual(submit_elements, [submit])
        driver.page.evaluate.assert_awaited_once_with(
            "document.getElementById('equipsubmit').disabled"
        )
        self.assertFalse(
            any(
                call.args
                and call.args[0]
                == "Repair submit disabled at current observation: state=%s"
                for call in debug.call_args_list
            )
        )

    async def test_debug_probe_failure_does_not_change_repair_state(self) -> None:
        driver = object.__new__(HVDriver)
        equipcount = SimpleNamespace(
            text="Selected 0 of 1 matching",
            mouse_click=AsyncMock(),
        )
        submit = object()
        driver.wait = AsyncMock()
        driver.page = SimpleNamespace(
            xpath=AsyncMock(side_effect=[[equipcount], [submit]]),
            evaluate=AsyncMock(
                side_effect=[True, RuntimeError("diagnostic evaluation failed")]
            ),
        )

        with (
            patch.object(hv_module.logger, "isEnabledFor", return_value=True),
            patch.object(hv_module.logger, "debug") as debug,
        ):
            disabled, submit_elements = (
                await HVDriver._select_all_and_check_repair_submit(
                    driver,
                    [equipcount],
                )
            )

        self.assertTrue(disabled)
        self.assertEqual(submit_elements, [submit])
        debug.assert_any_call(
            "Repair submit diagnostic probe failed: error_type=%s",
            "RuntimeError",
        )

    async def test_missing_submit_logs_indeterminate_legacy_outcome(self) -> None:
        driver = object.__new__(HVDriver)
        equipcount = SimpleNamespace(
            text="Selected 0 of 1 matching",
            mouse_click=AsyncMock(),
        )
        driver.wait = AsyncMock()
        driver.page = SimpleNamespace(
            xpath=AsyncMock(side_effect=[[equipcount], []]),
            evaluate=AsyncMock(),
        )

        with patch.object(hv_module.logger, "warning") as warning:
            disabled, submit_elements = (
                await HVDriver._select_all_and_check_repair_submit(
                    driver,
                    [equipcount],
                )
            )

        self.assertIsNone(disabled)
        self.assertEqual(submit_elements, [])
        warning.assert_called_once_with(
            "Equipment repair check is indeterminate: submit button is "
            "unavailable; repair was skipped"
        )
        driver.page.evaluate.assert_not_awaited()

    async def test_missing_armory_logs_skipped_non_blocking_outcome(self) -> None:
        driver, bazaar, _armory, _repair = _repair_driver(
            "https://hentaiverse.org/?s=Bazaar&ss=es",
            marker_payloads=[_maintenance_markers()],
        )
        driver.page.select.return_value = bazaar
        driver.page.xpath.side_effect = [[]]

        with patch.object(hv_module.logger, "warning") as warning:
            repaired = await HVDriver.repairequipment(driver)

        self.assertTrue(repaired)
        warning.assert_called_once_with(
            "Equipment repair check skipped: The Armory entry is unavailable"
        )

    async def test_missing_repair_tab_logs_skipped_non_blocking_outcome(self) -> None:
        driver, bazaar, armory, _repair = _repair_driver(
            "https://hentaiverse.org/?s=Bazaar&ss=es",
            marker_payloads=[_maintenance_markers()],
        )
        driver.page.select.return_value = bazaar
        driver.page.xpath.side_effect = [[armory], []]

        with patch.object(hv_module.logger, "warning") as warning:
            repaired = await HVDriver.repairequipment(driver)

        self.assertTrue(repaired)
        warning.assert_called_once_with(
            "Equipment repair check skipped: Repair tab is unavailable"
        )

    async def test_missing_post_submit_count_does_not_claim_repair(self) -> None:
        driver = object.__new__(HVDriver)
        driver._goto_repair_tab = AsyncMock(return_value=True)
        equipcount = SimpleNamespace(text="Selected 0 of 1 matching")
        submit = SimpleNamespace(mouse_click=AsyncMock())
        driver._select_all_and_check_repair_submit = AsyncMock(
            return_value=(False, [submit])
        )
        driver.page = SimpleNamespace(
            xpath=AsyncMock(side_effect=[[equipcount], []]),
            wait=AsyncMock(),
        )

        with patch.object(hv_module.logger, "info") as info:
            repaired = await HVDriver.repairequipment(driver)

        self.assertTrue(repaired)
        info.assert_called_once_with(
            "Repair submitted; no remaining equipment count is visible"
        )

    async def test_unreadable_post_submit_count_is_logged_as_indeterminate(
        self,
    ) -> None:
        driver = object.__new__(HVDriver)
        driver._goto_repair_tab = AsyncMock(return_value=True)
        equipcount = SimpleNamespace(text="Selected 0 of 1 matching")
        unreadable = SimpleNamespace(text="remaining unknown\nretry later")
        submit = SimpleNamespace(mouse_click=AsyncMock())
        driver._select_all_and_check_repair_submit = AsyncMock(
            return_value=(False, [submit])
        )
        driver.page = SimpleNamespace(
            xpath=AsyncMock(side_effect=[[equipcount], [unreadable]]),
            wait=AsyncMock(),
        )

        with self.assertLogs("hvbrowser.hv", level="WARNING") as captured:
            repaired = await HVDriver.repairequipment(driver)

        self.assertTrue(repaired)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("outcome is indeterminate", captured.output[0])
        self.assertIn(r"remaining unknown\nretry later", captured.output[0])
        self.assertNotIn("\n", captured.output[0])

    async def test_zero_post_submit_count_confirms_repair(self) -> None:
        driver = object.__new__(HVDriver)
        driver._goto_repair_tab = AsyncMock(return_value=True)
        equipcount = SimpleNamespace(text="Selected 0 of 1 matching")
        repaired_count = SimpleNamespace(text="Selected 0 of 0 matching")
        submit = SimpleNamespace(mouse_click=AsyncMock())
        driver._select_all_and_check_repair_submit = AsyncMock(
            return_value=(False, [submit])
        )
        driver.page = SimpleNamespace(
            xpath=AsyncMock(side_effect=[[equipcount], [repaired_count]]),
            wait=AsyncMock(),
        )

        with patch.object(hv_module.logger, "info") as info:
            repaired = await HVDriver.repairequipment(driver)

        self.assertTrue(repaired)
        info.assert_called_once_with("Repaired equipment: remaining=0")

    async def test_stale_disabled_submit_recovery_is_a_warning(self) -> None:
        driver = object.__new__(HVDriver)
        driver._goto_repair_tab = AsyncMock(side_effect=[True, True])
        equipcount = SimpleNamespace(text="Selected 0 of 1 matching")
        submit = SimpleNamespace(mouse_click=AsyncMock())
        driver._select_all_and_check_repair_submit = AsyncMock(
            side_effect=[(True, []), (False, [submit])]
        )
        driver.page = SimpleNamespace(
            xpath=AsyncMock(side_effect=[[equipcount], [equipcount], []]),
            wait=AsyncMock(),
        )

        with patch.object(hv_module.logger, "warning") as warning:
            repaired = await HVDriver.repairequipment(driver)

        self.assertTrue(repaired)
        warning.assert_called_once_with(
            "Repair submit was enabled after re-entering Repair tab; "
            "the earlier disabled check was stale"
        )
        submit.mouse_click.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
