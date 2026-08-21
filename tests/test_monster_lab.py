import asyncio
import unittest
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, patch

from hvbrowser import (
    MaintenanceNavigationContext,
    MonsterLabClient,
    MonsterLabFeed,
    MonsterLabPageError,
    MonsterLabSnapshot,
    MonsterLabSubmissionError,
)
from hvbrowser.runtime import ZendriverOperationTimeout


def _maintenance_markers(url: str) -> dict[str, object]:
    return {
        "url": url,
        "challenge": False,
        "completion": False,
        "nextFloor": False,
        "active": False,
    }


async def _no_sleep(_seconds: float) -> None:
    return


class _FakeElement:
    def __init__(self, *, on_click: Callable[[], None] | None = None) -> None:
        self.mouse_move_count = 0
        self.mouse_click_count = 0
        self._on_click = on_click

    async def mouse_move(self) -> None:
        self.mouse_move_count += 1

    async def mouse_click(self) -> None:
        self.mouse_click_count += 1
        if self._on_click is not None:
            self._on_click()


class _FakeMonsterLabPage:
    def __init__(
        self,
        available: set[MonsterLabFeed] | None = None,
    ) -> None:
        self.available = set() if available is None else set(available)
        self.has_api = True
        self.has_bazaar = True
        self.has_menu_entry = True
        self.preserve_after_submit = False
        self.submission_result = True
        self.submission_error: Exception | None = None
        self.current_url = "https://hentaiverse.org/"
        self.bazaar = _FakeElement()
        self.menu_entry = _FakeElement(
            on_click=lambda: setattr(
                self,
                "current_url",
                "https://hentaiverse.org/?s=Bazaar&ss=ml",
            )
        )
        self.evaluated: list[str] = []
        self.xpath_calls: list[tuple[str, int]] = []
        self.waited: list[int] = []

    async def select(
        self,
        selector: str,
        *,
        timeout: float,
    ) -> _FakeElement | None:
        if selector != "#parent_Bazaar":
            raise AssertionError(f"Unexpected selector: {selector}")
        if timeout != 5:
            raise AssertionError(f"Unexpected selector timeout: {timeout}")
        return self.bazaar if self.has_bazaar else None

    async def xpath(self, selector: str, timeout: int) -> list[_FakeElement]:
        self.xpath_calls.append((selector, timeout))
        if (
            "//*[@id='child_Bazaar']" in selector
            and "contains(@onclick, 'ss=ml')" in selector
            and "contains(@href, 'ss=ml')" in selector
            and timeout == 5
        ):
            return [self.menu_entry] if self.has_menu_entry else []
        for resource, action in (
            (MonsterLabFeed.FOOD, "feed"),
            (MonsterLabFeed.DRUGS, "drug"),
        ):
            expected = f'//img[@src="/y/monster/{action}allmonsters.png"]'
            if selector == expected and timeout == 2:
                return [_FakeElement()] if resource in self.available else []
        raise AssertionError(f"Unexpected XPath: {selector}, {timeout}")

    async def evaluate(self, expression: str) -> object:
        self.evaluated.append(expression)
        if "riddlesubmit" in expression and "finishbattle.png" in expression:
            return _maintenance_markers(self.current_url)
        if expression == "typeof do_feed_all === 'function'":
            return self.has_api
        if self.submission_error is not None:
            raise self.submission_error
        for resource in MonsterLabFeed:
            if f'do_feed_all("{resource.value}")' in expression:
                if self.submission_result and not self.preserve_after_submit:
                    self.available.discard(resource)
                return self.submission_result
        raise AssertionError(f"Unexpected JavaScript: {expression}")

    async def wait(self, seconds: int) -> None:
        self.waited.append(seconds)


class _FakeMonsterLabDriver:
    def __init__(self, page: _FakeMonsterLabPage) -> None:
        self.page = page
        self.homepage_calls: list[bool] = []
        self.get_calls: list[str] = []
        self.wait_calls: list[
            tuple[Callable[[], Awaitable[None]], bool, int, object, float]
        ] = []

    async def gohomepage(self, force: bool = False) -> None:
        self.homepage_calls.append(force)
        self.page.current_url = "https://hentaiverse.org/"

    async def get(self, url: str) -> None:
        self.get_calls.append(url)
        self.page.current_url = url

    async def wait(
        self,
        fun: Callable[[], Awaitable[None]],
        ischangeurl: bool,
        sleeptime: int = -1,
        *,
        owner: object,
        operation_timeout: float,
    ) -> None:
        self.wait_calls.append((fun, ischangeurl, sleeptime, owner, operation_timeout))
        await fun()


def _client(
    driver: _FakeMonsterLabDriver,
    *,
    confirmation_checks: int = 5,
    confirmation_interval: float = 1,
    sleep=_no_sleep,
) -> MonsterLabClient:
    return MonsterLabClient(
        driver,
        confirmation_checks=confirmation_checks,
        confirmation_interval=confirmation_interval,
        sleep=sleep,
    )


class MonsterLabClientTests(unittest.IsolatedAsyncioTestCase):
    def test_constructor_rejects_invalid_confirmation_settings(self) -> None:
        driver = _FakeMonsterLabDriver(_FakeMonsterLabPage())

        with self.assertRaisesRegex(ValueError, "at least 2"):
            _client(driver, confirmation_checks=0)
        with self.assertRaisesRegex(ValueError, "at least 2"):
            _client(driver, confirmation_checks=1)
        with self.assertRaisesRegex(ValueError, "at least 2"):
            _client(driver, confirmation_checks=True)
        with self.assertRaisesRegex(ValueError, "positive"):
            _client(driver, confirmation_interval=-0.1)
        with self.assertRaisesRegex(ValueError, "positive"):
            _client(driver, confirmation_interval=0)

    async def test_inspect_is_read_only_and_reports_both_actions(self) -> None:
        page = _FakeMonsterLabPage(set(MonsterLabFeed))
        driver = _FakeMonsterLabDriver(page)

        snapshot = await _client(driver).inspect(
            context=MaintenanceNavigationContext.ORDINARY
        )

        self.assertEqual(
            snapshot,
            MonsterLabSnapshot(frozenset(MonsterLabFeed)),
        )
        self.assertEqual(driver.homepage_calls, [])
        self.assertEqual(
            driver.get_calls,
            ["https://hentaiverse.org/?s=Bazaar&ss=ml"],
        )
        self.assertEqual(page.bazaar.mouse_move_count, 0)
        self.assertEqual(page.menu_entry.mouse_move_count, 0)
        self.assertEqual(page.menu_entry.mouse_click_count, 0)
        self.assertEqual(driver.wait_calls, [])
        self.assertEqual(page.waited, [])
        self.assertEqual(len(page.evaluated), 3)
        self.assertIn("riddlesubmit", page.evaluated[0])
        self.assertIn("riddlesubmit", page.evaluated[1])
        self.assertIn("window.location.href", page.evaluated[0])
        self.assertIn("window.location.href", page.evaluated[1])
        self.assertEqual(page.evaluated[2], "typeof do_feed_all === 'function'")

    async def test_inspect_fails_closed_when_page_api_is_missing(self) -> None:
        page = _FakeMonsterLabPage()
        page.has_api = False

        with self.assertRaisesRegex(MonsterLabPageError, "API is missing"):
            await _client(_FakeMonsterLabDriver(page)).inspect(
                context=MaintenanceNavigationContext.ORDINARY
            )

    async def test_inspect_does_not_depend_on_menu_structure(self) -> None:
        page = _FakeMonsterLabPage()
        page.has_bazaar = False
        page.has_menu_entry = False
        driver = _FakeMonsterLabDriver(page)

        snapshot = await _client(driver).inspect(
            context=MaintenanceNavigationContext.ORDINARY
        )

        self.assertEqual(snapshot, MonsterLabSnapshot(frozenset()))
        self.assertEqual(
            driver.get_calls,
            ["https://hentaiverse.org/?s=Bazaar&ss=ml"],
        )
        self.assertEqual(page.bazaar.mouse_move_count, 0)
        self.assertEqual(page.menu_entry.mouse_move_count, 0)

    async def test_feed_all_returns_noop_without_submitting_when_unavailable(
        self,
    ) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.DRUGS})

        report = await _client(
            _FakeMonsterLabDriver(page),
            confirmation_interval=0.01,
            sleep=_no_sleep,
        ).feed_all(
            MonsterLabFeed.FOOD,
            context=MaintenanceNavigationContext.ORDINARY,
        )

        self.assertFalse(report.performed)
        self.assertEqual(report.before, report.after)
        self.assertFalse(
            any('do_feed_all("food")' in script for script in page.evaluated)
        )

    async def test_feed_all_submits_one_resource_and_confirms_stable_disappearance(
        self,
    ) -> None:
        page = _FakeMonsterLabPage(set(MonsterLabFeed))

        with self.assertLogs("hvbrowser.monster_lab", level="DEBUG") as captured:
            report = await _client(
                _FakeMonsterLabDriver(page),
                confirmation_checks=2,
                confirmation_interval=0.01,
                sleep=_no_sleep,
            ).feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertTrue(report.performed)
        self.assertIn(MonsterLabFeed.FOOD, report.before.available_feed_all)
        self.assertNotIn(MonsterLabFeed.FOOD, report.after.available_feed_all)
        self.assertIn(MonsterLabFeed.DRUGS, report.after.available_feed_all)
        self.assertEqual(len(captured.output), 1)
        self.assertTrue(captured.output[0].startswith("DEBUG:hvbrowser.monster_lab:"))
        self.assertIn("Fed all eligible monsters with food", captured.output[0])
        submission_scripts = [
            script for script in page.evaluated if 'do_feed_all("food")' in script
        ]
        self.assertEqual(len(submission_scripts), 1)

    async def test_feed_all_rejects_false_submission_result(self) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.DRUGS})
        page.submission_result = False

        with self.assertRaisesRegex(MonsterLabSubmissionError, "rejected"):
            await _client(
                _FakeMonsterLabDriver(page),
                confirmation_interval=0.01,
                sleep=_no_sleep,
            ).feed_all(
                MonsterLabFeed.DRUGS,
                context=MaintenanceNavigationContext.ORDINARY,
            )

    async def test_feed_all_marks_evaluation_failure_as_unknown(self) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})
        page.submission_error = RuntimeError("disconnected")

        with self.assertRaisesRegex(MonsterLabSubmissionError, "outcome is unknown"):
            await _client(
                _FakeMonsterLabDriver(page),
                confirmation_interval=0.01,
                sleep=_no_sleep,
            ).feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        submission_scripts = [
            script for script in page.evaluated if 'do_feed_all("food")' in script
        ]
        self.assertEqual(len(submission_scripts), 1)

    async def test_feed_all_submit_hang_is_terminal_without_confirmation(self) -> None:
        release = asyncio.Event()

        async def hang(_: str) -> object:
            await release.wait()
            return True

        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})
        page.evaluate = AsyncMock(side_effect=hang)  # type: ignore[method-assign]
        client = _client(
            _FakeMonsterLabDriver(page),
            confirmation_checks=2,
            confirmation_interval=0.01,
            sleep=_no_sleep,
        )
        before = MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]
        client._inspect_current = AsyncMock()  # type: ignore[method-assign]

        with (
            patch("hvbrowser.monster_lab._MUTATION_TIMEOUT_SECONDS", 0.01),
            self.assertRaises(ZendriverOperationTimeout) as raised,
        ):
            await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(raised.exception.timeout_seconds, 0.01)
        client._inspect_current.assert_not_awaited()
        page.evaluate.assert_awaited_once()
        release.set()
        await asyncio.sleep(0)

    async def test_feed_all_rejects_unconfirmed_result(self) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})
        page.preserve_after_submit = True

        with self.assertRaisesRegex(MonsterLabSubmissionError, "Unable to confirm"):
            await _client(
                _FakeMonsterLabDriver(page),
                confirmation_checks=2,
                confirmation_interval=0.01,
                sleep=_no_sleep,
            ).feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

    async def test_transient_action_absence_is_not_accepted_as_confirmation(
        self,
    ) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})
        client = _client(
            _FakeMonsterLabDriver(page),
            confirmation_checks=2,
            confirmation_interval=0.01,
            sleep=_no_sleep,
        )
        before = MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]
        client._inspect_current = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                MonsterLabSnapshot(frozenset()),
                MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD})),
            ]
        )

        with self.assertRaisesRegex(MonsterLabSubmissionError, "Unable to confirm"):
            await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

    async def test_read_error_breaks_consecutive_absence_confirmation(self) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})
        client = _client(
            _FakeMonsterLabDriver(page),
            confirmation_checks=3,
            confirmation_interval=0.01,
            sleep=_no_sleep,
        )
        before = MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]
        client._inspect_current = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                MonsterLabSnapshot(frozenset()),
                MonsterLabPageError("transient read failure"),
                MonsterLabSnapshot(frozenset()),
            ]
        )

        with self.assertRaisesRegex(MonsterLabSubmissionError, "Unable to confirm"):
            await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

    async def test_feed_all_warns_once_after_confirmation_read_recovers(
        self,
    ) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})
        client = _client(
            _FakeMonsterLabDriver(page),
            confirmation_checks=3,
            confirmation_interval=0.01,
            sleep=_no_sleep,
        )
        before = MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        after = MonsterLabSnapshot(frozenset())
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]
        client._inspect_current = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                MonsterLabPageError("private detail\nsecond line"),
                after,
                after,
            ]
        )

        with self.assertLogs("hvbrowser.monster_lab", level="WARNING") as captured:
            report = await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertTrue(report.performed)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("confirmed_attempt=3/3", captured.output[0])
        self.assertIn("error_count=1", captured.output[0])
        self.assertIn("last_error_type=MonsterLabPageError", captured.output[0])
        self.assertNotIn("private detail", captured.output[0])

    async def test_confirmation_wait_failure_is_an_unknown_submission(self) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})

        async def broken_sleep(_seconds: float) -> None:
            raise RuntimeError("scheduler unavailable")

        with self.assertRaisesRegex(
            MonsterLabSubmissionError, "Unable to confirm"
        ) as raised:
            await _client(
                _FakeMonsterLabDriver(page),
                confirmation_interval=0.01,
                sleep=broken_sleep,
            ).feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    async def test_invalid_resource_is_rejected_before_navigation(self) -> None:
        driver = _FakeMonsterLabDriver(_FakeMonsterLabPage())

        with self.assertRaisesRegex(TypeError, "MonsterLabFeed"):
            await _client(driver).feed_all(  # type: ignore[arg-type]
                "food",
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(driver.homepage_calls, [])


if __name__ == "__main__":
    unittest.main()
