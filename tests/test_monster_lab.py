import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from hbrowser import BrowserMutationOutcomeUnknownError

from hvbrowser import (
    MaintenanceNavigationContext,
    MonsterLabClient,
    MonsterLabFeed,
    MonsterLabFeedReport,
    MonsterLabPageError,
    MonsterLabSnapshot,
    MonsterLabSubmissionError,
)
from hvbrowser.monster_lab import _MonsterLabPageState
from hvbrowser.runtime import PageStateTimeout, ZendriverOperationTimeout


class _Page:
    def __init__(self, available: set[MonsterLabFeed] | None = None) -> None:
        self.available = set(available or ())
        self.has_api = True
        self.error: str | None = None
        self.submit_result = True
        self.submit_error: Exception | None = None
        self.server_rejection: str | None = None
        self.preserve_after_submit = False
        self.submissions: list[MonsterLabFeed] = []
        self.expressions: list[str] = []

    def state(self) -> dict[str, object]:
        return {
            "hasApi": self.has_api,
            "foodAvailable": MonsterLabFeed.FOOD in self.available,
            "drugsAvailable": MonsterLabFeed.DRUGS in self.available,
            "errorText": self.error,
        }

    async def evaluate(self, expression: str) -> object:
        self.expressions.append(expression)
        if "do_feed_all(" not in expression:
            return self.state()
        if self.submit_error is not None:
            raise self.submit_error
        resource = next(
            feed for feed in MonsterLabFeed if f'"{feed.value}"' in expression
        )
        self.submissions.append(resource)
        if self.server_rejection is not None:
            self.error = self.server_rejection
            return True
        if self.submit_result and not self.preserve_after_submit:
            self.available.discard(resource)
        return self.submit_result


def _client(page: _Page) -> MonsterLabClient:
    return MonsterLabClient(type("Driver", (), {"page": page})())


class MonsterLabClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_resource_is_rejected_before_inspection(self) -> None:
        client = _client(_Page())
        client.inspect = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaises(TypeError):
            await client.feed_all(  # type: ignore[arg-type]
                "food",
                context=MaintenanceNavigationContext.ORDINARY,
            )

        client.inspect.assert_not_awaited()

    async def test_inspect_reads_both_actions_in_one_snapshot(self) -> None:
        page = _Page(set(MonsterLabFeed))
        client = _client(page)
        client._navigate = AsyncMock()  # type: ignore[method-assign]

        snapshot = await client.inspect(context=MaintenanceNavigationContext.ORDINARY)

        self.assertEqual(snapshot, MonsterLabSnapshot(frozenset(MonsterLabFeed)))
        self.assertEqual(len(page.expressions), 1)

    async def test_missing_api_fails_closed(self) -> None:
        page = _Page()
        page.has_api = False
        client = _client(page)
        client._navigate = AsyncMock()  # type: ignore[method-assign]
        client._open_directly = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(MonsterLabPageError, "API is missing"):
            await client.inspect(context=MaintenanceNavigationContext.ORDINARY)

        client._open_directly.assert_awaited_once()

    async def test_unavailable_resource_is_noop(self) -> None:
        page = _Page({MonsterLabFeed.DRUGS})
        client = _client(page)
        before = MonsterLabSnapshot(frozenset({MonsterLabFeed.DRUGS}))
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]

        report = await client.feed_all(
            MonsterLabFeed.FOOD,
            context=MaintenanceNavigationContext.ORDINARY,
        )

        self.assertEqual(
            report, MonsterLabFeedReport(MonsterLabFeed.FOOD, False, before, before)
        )
        self.assertEqual(page.submissions, [])

    async def test_feed_all_submits_once_and_confirms_disappearance(self) -> None:
        page = _Page(set(MonsterLabFeed))
        client = _client(page)
        before = MonsterLabSnapshot(frozenset(MonsterLabFeed))
        client.inspect = AsyncMock(return_value=before)  # type: ignore[method-assign]

        report = await client.feed_all(
            MonsterLabFeed.FOOD,
            context=MaintenanceNavigationContext.ORDINARY,
        )

        self.assertTrue(report.performed)
        self.assertNotIn(MonsterLabFeed.FOOD, report.after.available_feed_all)
        self.assertEqual(page.submissions, [MonsterLabFeed.FOOD])

    async def test_false_submission_is_rejected_without_replay(self) -> None:
        page = _Page({MonsterLabFeed.FOOD})
        page.submit_result = False
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        )

        with self.assertRaisesRegex(MonsterLabSubmissionError, "rejected"):
            await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.submissions, [MonsterLabFeed.FOOD])

    async def test_server_rejection_is_typed_without_replay(self) -> None:
        page = _Page({MonsterLabFeed.FOOD})
        page.server_rejection = "No eligible monsters"
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        )

        with self.assertRaisesRegex(MonsterLabSubmissionError, "No eligible"):
            await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.submissions, [MonsterLabFeed.FOOD])

    async def test_mutation_failure_is_unknown_without_confirmation(self) -> None:
        page = _Page({MonsterLabFeed.FOOD})
        page.submit_error = RuntimeError("disconnected")
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        )

        with self.assertRaises(BrowserMutationOutcomeUnknownError):
            await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

    async def test_mutation_hang_is_terminal_without_confirmation(self) -> None:
        release = asyncio.Event()
        page = _Page({MonsterLabFeed.FOOD})

        async def hang(_expression: str) -> object:
            await release.wait()
            return True

        page.evaluate = AsyncMock(side_effect=hang)
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        )
        state_wait = AsyncMock()

        with (
            patch("hvbrowser.runtime.PROTOCOL_COMMAND_TIMEOUT_SECONDS", 0.01),
            patch(
                "hvbrowser.monster_lab.wait_for_page_state",
                new=state_wait,
            ),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        state_wait.assert_not_awaited()
        release.set()
        await asyncio.sleep(0)

    async def test_generation_error_during_receipt_is_propagated(self) -> None:
        page = _Page({MonsterLabFeed.FOOD})
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        )
        timeout = ZendriverOperationTimeout(timeout_seconds=5)

        with (
            patch(
                "hvbrowser.monster_lab.wait_for_page_state",
                new=AsyncMock(side_effect=timeout),
            ),
            self.assertRaises(ZendriverOperationTimeout) as raised,
        ):
            await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertIs(raised.exception, timeout)

    async def test_delayed_confirmation_does_not_replay_submission(self) -> None:
        page = _Page({MonsterLabFeed.FOOD})
        page.preserve_after_submit = True
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        )
        delayed = _MonsterLabPageState(True, frozenset(), None)

        with patch(
            "hvbrowser.monster_lab.wait_for_page_state",
            new=AsyncMock(return_value=delayed),
        ):
            report = await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertTrue(report.performed)
        self.assertEqual(page.submissions, [MonsterLabFeed.FOOD])

    async def test_semantic_timeout_is_unknown_without_replay(self) -> None:
        page = _Page({MonsterLabFeed.FOOD})
        page.preserve_after_submit = True
        client = _client(page)
        client.inspect = AsyncMock(  # type: ignore[method-assign]
            return_value=MonsterLabSnapshot(frozenset({MonsterLabFeed.FOOD}))
        )

        with (
            patch(
                "hvbrowser.monster_lab.wait_for_page_state",
                new=AsyncMock(side_effect=PageStateTimeout("unchanged")),
            ),
            self.assertRaisesRegex(MonsterLabSubmissionError, "Unable to confirm"),
        ):
            await client.feed_all(
                MonsterLabFeed.FOOD,
                context=MaintenanceNavigationContext.ORDINARY,
            )

        self.assertEqual(page.submissions, [MonsterLabFeed.FOOD])


if __name__ == "__main__":
    unittest.main()
