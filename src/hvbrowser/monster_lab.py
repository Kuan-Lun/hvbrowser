"""Typed, explicit Monster Lab feed-all browser operations."""

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from .maintenance_navigation import (
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
    MaintenanceNavigationContext,
    MaintenanceNavigationObservation,
    observe_maintenance_navigation,
)
from .realm import Realm
from .runtime import (
    is_browser_generation_error,
    setup_logger,
    wait_for_zendriver,
)
from .urls import HENTAIVERSE_ROOT_URL

logger = setup_logger(__name__)

_READ_TIMEOUT_SECONDS = 8.0
_MUTATION_TIMEOUT_SECONDS = 15.0
_SELECTOR_INNER_TIMEOUT_SECONDS = 5.0
_SELECTOR_OUTER_TIMEOUT_SECONDS = 7.0
_SHORT_SELECTOR_INNER_TIMEOUT_SECONDS = 2.0
_SHORT_SELECTOR_OUTER_TIMEOUT_SECONDS = 4.0


class MonsterLabFeed(StrEnum):
    FOOD = "food"
    DRUGS = "drugs"


_FEED_ACTION_NAMES: dict[MonsterLabFeed, str] = {
    MonsterLabFeed.FOOD: "feed",
    MonsterLabFeed.DRUGS: "drug",
}
_MONSTER_LAB_ROUTE = "ml"
_MONSTER_LAB_URL = f"{HENTAIVERSE_ROOT_URL}/?s=Bazaar&ss={_MONSTER_LAB_ROUTE}"


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


class _MonsterLabNavigationSafetyError(MonsterLabPageError):
    """The current battle state, origin, or realm could not be trusted."""


class MonsterLabSubmissionError(RuntimeError):
    """A feed-all operation was rejected or could not be confirmed."""


class _MonsterLabDriver(Protocol):
    page: Any

    async def get(self, url: str) -> None: ...


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

    async def inspect(
        self,
        *,
        context: MaintenanceNavigationContext,
    ) -> MonsterLabSnapshot:
        """Navigate to and inspect Monster Lab without feeding monsters."""
        if not isinstance(context, MaintenanceNavigationContext):
            raise TypeError("context must be a MaintenanceNavigationContext")
        await self._navigate(context=context)
        try:
            return await self._inspect_current()
        except MonsterLabPageError as error:
            logger.warning(
                "Monster Lab was not readable after navigation; reloading once "
                "through the Persistent direct URL: error_type=%s",
                type(error).__name__,
            )

        await self._open_directly(context=MaintenanceNavigationContext.ORDINARY)
        return await self._inspect_current()

    async def feed_all(
        self,
        resource: MonsterLabFeed,
        *,
        context: MaintenanceNavigationContext,
    ) -> MonsterLabFeedReport:
        """Invoke one available feed-all operation and verify it is consumed."""
        if not isinstance(resource, MonsterLabFeed):
            raise TypeError("resource must be a MonsterLabFeed")
        if not isinstance(context, MaintenanceNavigationContext):
            raise TypeError("context must be a MaintenanceNavigationContext")
        before = await self.inspect(context=context)
        if resource not in before.available_feed_all:
            return MonsterLabFeedReport(resource, False, before, before)

        resource_js = json.dumps(resource.value)
        try:
            submitted = await wait_for_zendriver(
                self.page.evaluate(f"""
                    (() => {{
                        if (typeof do_feed_all !== 'function') return false;
                        do_feed_all({resource_js});
                        return true;
                    }})()
                    """),
                timeout=_MUTATION_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
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
        confirmation_error_count = 0
        last_confirmation_error_type: str | None = None
        for check in range(self.confirmation_checks):
            try:
                await self._sleep(self.confirmation_interval)
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                raise MonsterLabSubmissionError(
                    f"Unable to confirm Monster Lab {resource.value} feed-all"
                ) from error
            try:
                last_snapshot = await self._inspect_current()
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                last_error = error
                consecutive_absences = 0
                confirmation_error_count += 1
                last_confirmation_error_type = type(error).__name__
                continue
            last_error = None
            if resource not in last_snapshot.available_feed_all:
                consecutive_absences += 1
                if consecutive_absences >= 2:
                    if confirmation_error_count:
                        logger.warning(
                            "Monster Lab feed-all confirmation recovered after read "
                            "errors: resource=%s confirmed_attempt=%d/%d "
                            "error_count=%d last_error_type=%s",
                            resource.value,
                            check + 1,
                            self.confirmation_checks,
                            confirmation_error_count,
                            last_confirmation_error_type,
                        )
                    logger.debug("Fed all eligible monsters with %s", resource.value)
                    return MonsterLabFeedReport(resource, True, before, last_snapshot)
            else:
                consecutive_absences = 0

        raise MonsterLabSubmissionError(
            f"Unable to confirm Monster Lab {resource.value} feed-all"
        ) from last_error

    async def _navigate(
        self,
        *,
        context: MaintenanceNavigationContext = MaintenanceNavigationContext.ORDINARY,
    ) -> None:
        await self._open_directly(context=context)

    async def _open_directly(
        self,
        *,
        context: MaintenanceNavigationContext = MaintenanceNavigationContext.ORDINARY,
    ) -> None:
        await self._ensure_navigation_is_safe(
            Realm.PERSISTENT,
            "before direct Monster Lab navigation",
            context=context,
        )
        try:
            await self.driver.get(_MONSTER_LAB_URL)
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            try:
                await self._ensure_navigation_is_safe(
                    Realm.PERSISTENT,
                    "after direct Monster Lab navigation",
                    context=MaintenanceNavigationContext.ORDINARY,
                )
            except MaintenanceNavigationBlockedError as blocked:
                raise blocked from error
            except _MonsterLabNavigationSafetyError as safety_error:
                raise safety_error from error
            raise MonsterLabPageError(
                "Unable to open Monster Lab through its direct URL"
            ) from error

        await self._verify_destination()

    async def _ensure_navigation_is_safe(
        self,
        expected_realm: Realm,
        phase: str,
        *,
        context: MaintenanceNavigationContext,
    ) -> MaintenanceNavigationObservation:
        if not isinstance(context, MaintenanceNavigationContext):
            raise TypeError("context must be a MaintenanceNavigationContext")
        try:
            observation = await observe_maintenance_navigation(self.page)
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise _MonsterLabNavigationSafetyError(
                f"Unable to verify battle state {phase}"
            ) from error
        if observation.realm is not expected_realm:
            raise _MonsterLabNavigationSafetyError(
                f"Monster Lab navigation is on an untrusted or wrong realm {phase}"
            )
        expected_path = "/isekai/" if expected_realm is Realm.ISEKAI else "/"
        if urlsplit(observation.url).path != expected_path:
            raise _MonsterLabNavigationSafetyError(
                f"Monster Lab navigation is on an unexpected path {phase}"
            )
        may_leave_completion = (
            context is MaintenanceNavigationContext.POST_BATTLE
            and expected_realm is Realm.PERSISTENT
            and observation.blocker is MaintenanceNavigationBlocker.COMPLETION
        )
        if observation.blocker is not None and not may_leave_completion:
            raise MaintenanceNavigationBlockedError(observation.blocker)
        return observation

    async def _verify_destination(self) -> None:
        observation = await self._ensure_navigation_is_safe(
            Realm.PERSISTENT,
            "after opening Monster Lab",
            context=MaintenanceNavigationContext.ORDINARY,
        )
        parsed_url = urlsplit(observation.url)
        query = parse_qs(parsed_url.query, keep_blank_values=True)
        expected_query = {
            "s": ["Bazaar"],
            "ss": [_MONSTER_LAB_ROUTE],
        }
        if any(query.get(key) != value for key, value in expected_query.items()):
            raise MonsterLabPageError(
                "Monster Lab navigation did not land on the requested route"
            )

    async def _inspect_current(self) -> MonsterLabSnapshot:
        try:
            is_monster_lab = await wait_for_zendriver(
                self.page.evaluate("typeof do_feed_all === 'function'"),
                timeout=_READ_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise MonsterLabPageError(
                "Unable to inspect Monster Lab feed-all API"
            ) from error
        if is_monster_lab is not True:
            raise MonsterLabPageError("Monster Lab feed-all API is missing")

        available: set[MonsterLabFeed] = set()
        for resource, action in _FEED_ACTION_NAMES.items():
            try:
                elements = await wait_for_zendriver(
                    self.page.xpath(
                        f'//img[@src="/y/monster/{action}allmonsters.png"]',
                        timeout=_SHORT_SELECTOR_INNER_TIMEOUT_SECONDS,
                    ),
                    timeout=_SHORT_SELECTOR_OUTER_TIMEOUT_SECONDS,
                    owner=self.page,
                )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                raise MonsterLabPageError(
                    f"Unable to inspect Monster Lab {resource.value} action"
                ) from error
            if elements:
                available.add(resource)
        return MonsterLabSnapshot(frozenset(available))
