import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hvbrowser import (
    HVDriver,
    MonsterLabClient,
    MonsterLabFeed,
    MonsterLabFeedReport,
    MonsterLabPageError,
    MonsterLabSnapshot,
    MonsterLabSubmissionError,
)


async def _no_sleep(_seconds: float) -> None:
    return


class _FakeElement:
    def __init__(self) -> None:
        self.mouse_move_count = 0
        self.mouse_click_count = 0

    async def mouse_move(self) -> None:
        self.mouse_move_count += 1

    async def mouse_click(self) -> None:
        self.mouse_click_count += 1


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
        self.bazaar = _FakeElement()
        self.menu_entry = _FakeElement()
        self.evaluated: list[str] = []
        self.xpath_calls: list[tuple[str, int]] = []
        self.waited: list[int] = []

    async def select(self, selector: str) -> _FakeElement | None:
        if selector != "#parent_Bazaar":
            raise AssertionError(f"Unexpected selector: {selector}")
        return self.bazaar if self.has_bazaar else None

    async def xpath(self, selector: str, timeout: int) -> list[_FakeElement]:
        self.xpath_calls.append((selector, timeout))
        if selector == "//div[contains(text(), 'Monster Lab')]" and timeout == 5:
            return [self.menu_entry] if self.has_menu_entry else []
        for resource, action in (
            (MonsterLabFeed.FOOD, "feed"),
            (MonsterLabFeed.DRUGS, "drug"),
        ):
            expected = f'//img[@src="/y/monster/{action}allmonsters.png"]'
            if selector == expected and timeout == 2:
                return [_FakeElement()] if resource in self.available else []
        raise AssertionError(f"Unexpected XPath: {selector}, {timeout}")

    async def evaluate(self, expression: str) -> bool:
        self.evaluated.append(expression)
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

    async def gohomepage(self, force: bool = False) -> None:
        self.homepage_calls.append(force)


class MonsterLabClientTests(unittest.IsolatedAsyncioTestCase):
    def test_constructor_rejects_invalid_confirmation_settings(self) -> None:
        driver = _FakeMonsterLabDriver(_FakeMonsterLabPage())

        with self.assertRaisesRegex(ValueError, "at least 2"):
            MonsterLabClient(driver, confirmation_checks=0)
        with self.assertRaisesRegex(ValueError, "at least 2"):
            MonsterLabClient(driver, confirmation_checks=1)
        with self.assertRaisesRegex(ValueError, "at least 2"):
            MonsterLabClient(driver, confirmation_checks=True)
        with self.assertRaisesRegex(ValueError, "positive"):
            MonsterLabClient(driver, confirmation_interval=-0.1)
        with self.assertRaisesRegex(ValueError, "positive"):
            MonsterLabClient(driver, confirmation_interval=0)

    async def test_inspect_is_read_only_and_reports_both_actions(self) -> None:
        page = _FakeMonsterLabPage(set(MonsterLabFeed))
        driver = _FakeMonsterLabDriver(page)

        snapshot = await MonsterLabClient(driver).inspect()

        self.assertEqual(
            snapshot,
            MonsterLabSnapshot(frozenset(MonsterLabFeed)),
        )
        self.assertEqual(driver.homepage_calls, [True])
        self.assertEqual(page.bazaar.mouse_move_count, 1)
        self.assertEqual(page.menu_entry.mouse_move_count, 1)
        self.assertEqual(page.menu_entry.mouse_click_count, 1)
        self.assertEqual(page.waited, [1])
        self.assertEqual(page.evaluated, ["typeof do_feed_all === 'function'"])

    async def test_inspect_fails_closed_when_page_api_is_missing(self) -> None:
        page = _FakeMonsterLabPage()
        page.has_api = False

        with self.assertRaisesRegex(MonsterLabPageError, "API is missing"):
            await MonsterLabClient(_FakeMonsterLabDriver(page)).inspect()

    async def test_inspect_fails_closed_when_menu_is_missing(self) -> None:
        page = _FakeMonsterLabPage()
        page.has_menu_entry = False

        with self.assertRaisesRegex(MonsterLabPageError, "menu entry"):
            await MonsterLabClient(_FakeMonsterLabDriver(page)).inspect()

    async def test_feed_all_returns_noop_without_submitting_when_unavailable(
        self,
    ) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.DRUGS})

        report = await MonsterLabClient(
            _FakeMonsterLabDriver(page),
            confirmation_interval=0.01,
            sleep=_no_sleep,
        ).feed_all(MonsterLabFeed.FOOD)

        self.assertFalse(report.performed)
        self.assertEqual(report.before, report.after)
        self.assertFalse(
            any('do_feed_all("food")' in script for script in page.evaluated)
        )

    async def test_feed_all_submits_one_resource_and_confirms_stable_disappearance(
        self,
    ) -> None:
        page = _FakeMonsterLabPage(set(MonsterLabFeed))

        report = await MonsterLabClient(
            _FakeMonsterLabDriver(page),
            confirmation_checks=2,
            confirmation_interval=0.01,
            sleep=_no_sleep,
        ).feed_all(MonsterLabFeed.FOOD)

        self.assertTrue(report.performed)
        self.assertIn(MonsterLabFeed.FOOD, report.before.available_feed_all)
        self.assertNotIn(MonsterLabFeed.FOOD, report.after.available_feed_all)
        self.assertIn(MonsterLabFeed.DRUGS, report.after.available_feed_all)
        submission_scripts = [
            script for script in page.evaluated if 'do_feed_all("food")' in script
        ]
        self.assertEqual(len(submission_scripts), 1)

    async def test_feed_all_rejects_false_submission_result(self) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.DRUGS})
        page.submission_result = False

        with self.assertRaisesRegex(MonsterLabSubmissionError, "rejected"):
            await MonsterLabClient(
                _FakeMonsterLabDriver(page),
                confirmation_interval=0.01,
                sleep=_no_sleep,
            ).feed_all(MonsterLabFeed.DRUGS)

    async def test_feed_all_marks_evaluation_failure_as_unknown(self) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})
        page.submission_error = RuntimeError("disconnected")

        with self.assertRaisesRegex(MonsterLabSubmissionError, "outcome is unknown"):
            await MonsterLabClient(
                _FakeMonsterLabDriver(page),
                confirmation_interval=0.01,
                sleep=_no_sleep,
            ).feed_all(MonsterLabFeed.FOOD)

    async def test_feed_all_rejects_unconfirmed_result(self) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})
        page.preserve_after_submit = True

        with self.assertRaisesRegex(MonsterLabSubmissionError, "Unable to confirm"):
            await MonsterLabClient(
                _FakeMonsterLabDriver(page),
                confirmation_checks=2,
                confirmation_interval=0.01,
                sleep=_no_sleep,
            ).feed_all(MonsterLabFeed.FOOD)

    async def test_transient_action_absence_is_not_accepted_as_confirmation(
        self,
    ) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})
        client = MonsterLabClient(
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
            await client.feed_all(MonsterLabFeed.FOOD)

    async def test_read_error_breaks_consecutive_absence_confirmation(self) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})
        client = MonsterLabClient(
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
            await client.feed_all(MonsterLabFeed.FOOD)

    async def test_confirmation_wait_failure_is_an_unknown_submission(self) -> None:
        page = _FakeMonsterLabPage({MonsterLabFeed.FOOD})

        async def broken_sleep(_seconds: float) -> None:
            raise RuntimeError("scheduler unavailable")

        with self.assertRaisesRegex(
            MonsterLabSubmissionError, "Unable to confirm"
        ) as raised:
            await MonsterLabClient(
                _FakeMonsterLabDriver(page),
                confirmation_interval=0.01,
                sleep=broken_sleep,
            ).feed_all(MonsterLabFeed.FOOD)

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    async def test_invalid_resource_is_rejected_before_navigation(self) -> None:
        driver = _FakeMonsterLabDriver(_FakeMonsterLabPage())

        with self.assertRaisesRegex(TypeError, "MonsterLabFeed"):
            await MonsterLabClient(driver).feed_all("food")  # type: ignore[arg-type]

        self.assertEqual(driver.homepage_calls, [])

    async def test_compatibility_workflow_applies_food_then_drugs(self) -> None:
        driver = object.__new__(HVDriver)
        client = SimpleNamespace(feed_all=AsyncMock())

        with patch("hvbrowser.hv.MonsterLabClient", return_value=client):
            await HVDriver.monstercheck(driver)

        self.assertEqual(
            client.feed_all.await_args_list,
            [
                unittest.mock.call(MonsterLabFeed.FOOD),
                unittest.mock.call(MonsterLabFeed.DRUGS),
            ],
        )

    async def test_hvdriver_exposes_explicit_monster_lab_operations(self) -> None:
        driver = object.__new__(HVDriver)
        before = MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        after = MonsterLabSnapshot(frozenset())
        report = MonsterLabFeedReport(MonsterLabFeed.FOOD, True, before, after)
        client = SimpleNamespace(
            inspect=AsyncMock(return_value=before),
            feed_all=AsyncMock(return_value=report),
        )

        with patch("hvbrowser.hv.MonsterLabClient", return_value=client):
            inspected = await HVDriver.inspect_monster_lab(driver)
            fed = await HVDriver.feed_all_monsters(driver, MonsterLabFeed.FOOD)

        self.assertIs(inspected, before)
        self.assertIs(fed, report)
        client.inspect.assert_awaited_once_with()
        client.feed_all.assert_awaited_once_with(MonsterLabFeed.FOOD)


if __name__ == "__main__":
    unittest.main()
