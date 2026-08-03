import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hvbrowser import (
    HENTAIVERSE_ISEKAI_ROOT_URL,
    HVDriver,
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
)


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

        await driver.goisekai()

        driver.get.assert_awaited_once_with(HENTAIVERSE_ISEKAI_ROOT_URL)


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


if __name__ == "__main__":
    unittest.main()
