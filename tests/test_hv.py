import unittest
from unittest.mock import AsyncMock

from hvbrowser import HENTAIVERSE_ISEKAI_ROOT_URL, HVDriver


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


if __name__ == "__main__":
    unittest.main()
