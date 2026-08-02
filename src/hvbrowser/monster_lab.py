"""Typed, explicit Monster Lab feed-all browser operations."""

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .runtime import setup_logger

logger = setup_logger(__name__)


class MonsterLabFeed(StrEnum):
    FOOD = "food"
    DRUGS = "drugs"


_FEED_ACTION_NAMES: dict[MonsterLabFeed, str] = {
    MonsterLabFeed.FOOD: "feed",
    MonsterLabFeed.DRUGS: "drug",
}


@dataclass(frozen=True, slots=True)
class MonsterLabSnapshot:
    available_feed_all: frozenset[MonsterLabFeed]


@dataclass(frozen=True, slots=True)
class MonsterLabFeedReport:
    resource: MonsterLabFeed
    performed: bool
    before: MonsterLabSnapshot
    after: MonsterLabSnapshot


class MonsterLabPageError(RuntimeError):
    """The Monster Lab page did not expose the expected structure."""


class MonsterLabSubmissionError(RuntimeError):
    """A feed-all operation was rejected or could not be confirmed."""


class _MonsterLabDriver(Protocol):
    page: Any

    async def gohomepage(self, force: bool = False) -> None: ...


class MonsterLabClient:
    """Inspect Monster Lab and invoke one explicit feed-all resource."""

    def __init__(
        self,
        driver: _MonsterLabDriver,
        *,
        confirmation_checks: int = 5,
        confirmation_interval: float = 1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (
            not isinstance(confirmation_checks, int)
            or isinstance(confirmation_checks, bool)
            or confirmation_checks < 2
        ):
            raise ValueError("confirmation_checks must be at least 2")
        if (
            isinstance(confirmation_interval, bool)
            or not math.isfinite(confirmation_interval)
            or confirmation_interval <= 0
        ):
            raise ValueError("confirmation_interval must be a finite positive number")
        self.driver = driver
        self.confirmation_checks = confirmation_checks
        self.confirmation_interval = confirmation_interval
        self._sleep = sleep

    @property
    def page(self) -> Any:
        return self.driver.page

    async def inspect(self) -> MonsterLabSnapshot:
        """Navigate to and inspect Monster Lab without feeding monsters."""
        await self._navigate()
        return await self._inspect_current()

    async def feed_all(self, resource: MonsterLabFeed) -> MonsterLabFeedReport:
        """Invoke one available feed-all operation and verify it is consumed."""
        if not isinstance(resource, MonsterLabFeed):
            raise TypeError("resource must be a MonsterLabFeed")
        before = await self.inspect()
        if resource not in before.available_feed_all:
            return MonsterLabFeedReport(resource, False, before, before)

        resource_js = json.dumps(resource.value)
        try:
            submitted = await self.page.evaluate(f"""
                (() => {{
                    if (typeof do_feed_all !== 'function') return false;
                    do_feed_all({resource_js});
                    return true;
                }})()
                """)
        except Exception as error:
            raise MonsterLabSubmissionError(
                f"Monster Lab {resource.value} feed-all outcome is unknown"
            ) from error
        if submitted is not True:
            raise MonsterLabSubmissionError(
                f"Monster Lab rejected {resource.value} feed-all"
            )

        last_snapshot: MonsterLabSnapshot | None = None
        last_error: Exception | None = None
        consecutive_absences = 0
        for _ in range(self.confirmation_checks):
            try:
                await self._sleep(self.confirmation_interval)
            except Exception as error:
                raise MonsterLabSubmissionError(
                    f"Unable to confirm Monster Lab {resource.value} feed-all"
                ) from error
            try:
                last_snapshot = await self._inspect_current()
            except Exception as error:
                last_error = error
                consecutive_absences = 0
                continue
            last_error = None
            if resource not in last_snapshot.available_feed_all:
                consecutive_absences += 1
                if consecutive_absences >= 2:
                    logger.info("Fed all eligible monsters with %s", resource.value)
                    return MonsterLabFeedReport(resource, True, before, last_snapshot)
            else:
                consecutive_absences = 0

        raise MonsterLabSubmissionError(
            f"Unable to confirm Monster Lab {resource.value} feed-all"
        ) from last_error

    async def _navigate(self) -> None:
        await self.driver.gohomepage(force=True)
        try:
            bazaar = await self.page.select("#parent_Bazaar")
        except Exception as error:
            raise MonsterLabPageError("Bazaar menu is missing") from error
        if bazaar is None:
            raise MonsterLabPageError("Bazaar menu is missing")
        try:
            elements = await self.page.xpath(
                "//div[contains(text(), 'Monster Lab')]", timeout=5
            )
        except Exception as error:
            raise MonsterLabPageError(
                "Unable to find Monster Lab menu entry"
            ) from error
        if not elements:
            raise MonsterLabPageError("Unable to find Monster Lab menu entry")

        try:
            await bazaar.mouse_move()
            await elements[0].mouse_move()
            await elements[0].mouse_click()
            await self.page.wait(1)
        except Exception as error:
            raise MonsterLabPageError("Unable to open Monster Lab") from error

    async def _inspect_current(self) -> MonsterLabSnapshot:
        try:
            is_monster_lab = await self.page.evaluate(
                "typeof do_feed_all === 'function'"
            )
        except Exception as error:
            raise MonsterLabPageError(
                "Unable to inspect Monster Lab feed-all API"
            ) from error
        if is_monster_lab is not True:
            raise MonsterLabPageError("Monster Lab feed-all API is missing")

        available: set[MonsterLabFeed] = set()
        for resource, action in _FEED_ACTION_NAMES.items():
            try:
                elements = await self.page.xpath(
                    f'//img[@src="/y/monster/{action}allmonsters.png"]', timeout=2
                )
            except Exception as error:
                raise MonsterLabPageError(
                    f"Unable to inspect Monster Lab {resource.value} action"
                ) from error
            if elements:
                available.add(resource)
        return MonsterLabSnapshot(frozenset(available))
