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
from hvbrowser.realm import Realm


def _element(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        mouse_move=AsyncMock(),
        mouse_click=AsyncMock(),
        click=AsyncMock(),
    )


def _navigation_elements() -> tuple[SimpleNamespace, SimpleNamespace]:
    return _element("The Armory"), _element("Repair")


def _client(
    xpath_results: list[object],
    *,
    evaluate_results: list[object] | None = None,
    realm: Realm = Realm.PERSISTENT,
) -> tuple[
    EquipmentRepairClient,
    SimpleNamespace,
    SimpleNamespace,
    SimpleNamespace,
]:
    page = SimpleNamespace(
        xpath=AsyncMock(side_effect=xpath_results),
        evaluate=AsyncMock(side_effect=evaluate_results or []),
        wait=AsyncMock(),
    )
    driver = SimpleNamespace(page=page, wait=AsyncMock())
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
    async def test_inspect_returns_realm_scoped_repair_count(self) -> None:
        armory, repair = _navigation_elements()
        count = _element("Selected 0 of 3 matching")
        client, driver, realm_navigator, maintenance = _client(
            [[armory], [repair], [repair], [count]],
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
        driver.page.evaluate.assert_not_awaited()

    async def test_no_matching_equipment_is_ready_without_submission(self) -> None:
        armory, repair = _navigation_elements()
        client, driver, _realm, _maintenance = _client(
            [[armory], [repair], [repair], []]
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.NO_REPAIR_NEEDED)
        self.assertTrue(report.ready)
        self.assertEqual(report.before.repair_count, 0)
        self.assertEqual(report.after, report.before)
        driver.page.evaluate.assert_not_awaited()
        driver.page.wait.assert_not_awaited()

    async def test_repair_all_confirms_zero_remaining_items(self) -> None:
        armory, repair = _navigation_elements()
        before = _element("Selected 0 of 3 matching")
        submit = _element()
        after = _element("Selected 0 of 0 matching")
        client, driver, _realm, _maintenance = _client(
            [
                [armory],
                [repair],
                [repair],
                [before],
                [submit],
                [repair],
                [after],
            ],
            evaluate_results=[False],
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
        before = _element("Selected 0 of 2 matching")
        first_submit = _element()
        fresh = _element("Selected 0 of 2 matching")
        second_submit = _element()
        client, driver, _realm, maintenance = _client(
            [
                [armory_1],
                [repair_1],
                [repair_1],
                [before],
                [first_submit],
                [armory_2],
                [repair_2],
                [repair_2],
                [fresh],
                [second_submit],
            ],
            evaluate_results=[True, True],
        )

        with patch.object(
            repair_module.logger,
            "isEnabledFor",
            return_value=False,
        ):
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
        before = _element("Selected 0 of 2 matching")
        first_submit = _element()
        fresh = _element("Selected 0 of 2 matching")
        second_submit = _element()
        after = _element("Selected 0 of 0 matching")
        client, _driver, _realm, maintenance = _client(
            [
                [armory_1],
                [repair_1],
                [repair_1],
                [before],
                [first_submit],
                [armory_2],
                [repair_2],
                [repair_2],
                [fresh],
                [second_submit],
                [repair_2],
                [after],
            ],
            evaluate_results=[True, False],
        )

        with patch.object(
            repair_module.logger,
            "isEnabledFor",
            return_value=False,
        ):
            report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.REPAIRED)
        self.assertTrue(report.ready)
        self.assertEqual(maintenance.select_bazaar.await_count, 2)
        first_submit.mouse_click.assert_not_awaited()
        second_submit.mouse_click.assert_awaited_once_with()

    async def test_fresh_retry_detects_concurrent_state_change(self) -> None:
        armory_1, repair_1 = _navigation_elements()
        armory_2, repair_2 = _navigation_elements()
        client, _driver, _realm, _maintenance = _client(
            [
                [armory_1],
                [repair_1],
                [repair_1],
                [_element("Selected 0 of 2 matching")],
                [_element()],
                [armory_2],
                [repair_2],
                [repair_2],
                [_element("Selected 0 of 1 matching")],
            ],
            evaluate_results=[True],
        )

        with patch.object(
            repair_module.logger,
            "isEnabledFor",
            return_value=False,
        ):
            with self.assertRaises(EquipmentRepairStateChangedError):
                await client.repair_all()

    async def test_malformed_count_is_a_typed_page_error(self) -> None:
        armory, repair = _navigation_elements()
        client, _driver, _realm, _maintenance = _client(
            [[armory], [repair], [repair], [_element("unknown")]]
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "parse"):
            await client.inspect()

    async def test_missing_submit_is_a_typed_page_error(self) -> None:
        armory, repair = _navigation_elements()
        client, _driver, _realm, _maintenance = _client(
            [
                [armory],
                [repair],
                [repair],
                [_element("Selected 0 of 1 matching")],
                [],
            ]
        )

        with self.assertRaisesRegex(EquipmentRepairPageError, "submit button"):
            await client.repair_all()

    async def test_disappearing_count_confirms_no_matching_equipment(self) -> None:
        armory, repair = _navigation_elements()
        client, _driver, _realm, _maintenance = _client(
            [
                [armory],
                [repair],
                [repair],
                [_element("Selected 0 of 1 matching")],
                [_element()],
                [repair],
                [],
            ],
            evaluate_results=[False],
        )

        report = await client.repair_all()

        self.assertIs(report.outcome, EquipmentRepairOutcome.REPAIRED)
        self.assertEqual(report.after.repair_count, 0)

    async def test_wrong_page_without_repair_marker_fails_closed(self) -> None:
        armory, repair = _navigation_elements()
        client, _driver, _realm, _maintenance = _client([[armory], [repair], []])

        with self.assertRaisesRegex(EquipmentRepairPageError, "Repair page marker"):
            await client.inspect()

    async def test_wrong_post_submit_page_is_an_unknown_outcome(self) -> None:
        armory, repair = _navigation_elements()
        client, _driver, _realm, _maintenance = _client(
            [
                [armory],
                [repair],
                [repair],
                [_element("Selected 0 of 1 matching")],
                [_element()],
                [],
            ],
            evaluate_results=[False],
        )

        with self.assertRaisesRegex(
            EquipmentRepairSubmissionError,
            "Unable to confirm",
        ):
            await client.repair_all()

    async def test_expected_snapshot_prevents_stale_submission(self) -> None:
        armory, repair = _navigation_elements()
        client, driver, _realm, _maintenance = _client(
            [
                [armory],
                [repair],
                [repair],
                [_element("Selected 0 of 1 matching")],
            ]
        )

        with self.assertRaises(EquipmentRepairStateChangedError):
            await client.repair_all(EquipmentRepairSnapshot(Realm.PERSISTENT, 2))

        driver.page.evaluate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
