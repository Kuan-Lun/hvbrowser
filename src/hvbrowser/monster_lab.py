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
    MaintenanceNavigator,
    classify_maintenance_navigation_blocker,
)
from .realm import Realm, realm_from_url
from .runtime import setup_logger
from .urls import HENTAIVERSE_ROOT_URL

logger = setup_logger(__name__)


class MonsterLabFeed(StrEnum):
    FOOD = "food"
    DRUGS = "drugs"


_FEED_ACTION_NAMES: dict[MonsterLabFeed, str] = {
    MonsterLabFeed.FOOD: "feed",
    MonsterLabFeed.DRUGS: "drug",
}
_MONSTER_LAB_ROUTE = "ml"
_MONSTER_LAB_URL = f"{HENTAIVERSE_ROOT_URL}/?s=Bazaar&ss={_MONSTER_LAB_ROUTE}"
_MONSTER_LAB_MENU_XPATH = (
    "//*[@id='child_Bazaar']"
    "//*[@onclick and contains(@onclick, 's=Bazaar') "
    f"and contains(@onclick, 'ss={_MONSTER_LAB_ROUTE}')]"
    " | //*[@id='child_Bazaar']//a[contains(@href, 's=Bazaar') "
    f"and contains(@href, 'ss={_MONSTER_LAB_ROUTE}')]"
)


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

    async def wait(
        self,
        fun: Any,
        ischangeurl: bool,
        sleeptime: int = -1,
    ) -> None: ...


class MonsterLabClient:
    """Inspect Monster Lab and invoke one explicit feed-all resource."""

    def __init__(
        self,
        driver: _MonsterLabDriver,
        navigation: MaintenanceNavigator,
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
        self.navigation = navigation
        self.confirmation_checks = confirmation_checks
        self.confirmation_interval = confirmation_interval
        self._sleep = sleep

    @property
    def page(self) -> Any:
        return self.driver.page

    async def inspect(self) -> MonsterLabSnapshot:
        """Navigate to and inspect Monster Lab without feeding monsters."""
        await self._navigate()
        try:
            return await self._inspect_current()
        except MonsterLabPageError as error:
            logger.warning(
                "Monster Lab was not readable after navigation; reloading once "
                "through the Persistent direct URL: error_type=%s",
                type(error).__name__,
            )

        await self._open_directly()
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
        confirmation_error_count = 0
        last_confirmation_error_type: str | None = None
        for check in range(self.confirmation_checks):
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

    async def _navigate(self) -> None:
        try:
            await self._open_from_menu()
            return
        except MaintenanceNavigationBlockedError:
            raise
        except _MonsterLabNavigationSafetyError:
            raise
        except MonsterLabPageError as error:
            logger.warning(
                "Monster Lab menu navigation did not open the requested page; "
                "retrying once through the Persistent direct URL: error_type=%s",
                type(error).__name__,
            )

        await self._open_directly()

    async def _open_from_menu(self) -> None:
        try:
            bazaar = await self.navigation.select_bazaar(
                Realm.PERSISTENT,
                navigate_first=True,
            )
        except MaintenanceNavigationBlockedError:
            raise
        except Exception as error:
            raise MonsterLabPageError("Bazaar menu is missing") from error
        try:
            elements = await self.page.xpath(_MONSTER_LAB_MENU_XPATH, timeout=5)
        except Exception as error:
            raise MonsterLabPageError(
                "Unable to find Monster Lab menu entry"
            ) from error
        if not elements:
            raise MonsterLabPageError("Unable to find Monster Lab menu entry")

        try:
            await bazaar.mouse_move()
            await elements[0].mouse_move()
            await self.driver.wait(
                elements[0].mouse_click,
                ischangeurl=True,
            )
        except Exception as error:
            raise MonsterLabPageError("Unable to open Monster Lab") from error

        await self._verify_destination()

    async def _open_directly(self) -> None:
        await self._ensure_navigation_is_safe("before direct Monster Lab navigation")
        try:
            await self.driver.get(_MONSTER_LAB_URL)
        except Exception as error:
            try:
                await self._ensure_navigation_is_safe(
                    "after direct Monster Lab navigation"
                )
            except MaintenanceNavigationBlockedError as blocked:
                raise blocked from error
            except _MonsterLabNavigationSafetyError as safety_error:
                raise safety_error from error
            raise MonsterLabPageError(
                "Unable to open Monster Lab through its direct URL"
            ) from error

        await self._verify_destination()

    async def _ensure_navigation_is_safe(self, context: str) -> None:
        try:
            blocker = await classify_maintenance_navigation_blocker(self.page)
        except Exception as error:
            raise _MonsterLabNavigationSafetyError(
                f"Unable to verify battle state {context}"
            ) from error
        if blocker is not None:
            raise MaintenanceNavigationBlockedError(blocker)

    async def _verify_destination(self) -> None:
        await self._ensure_navigation_is_safe("after opening Monster Lab")
        try:
            current_url = await self.page.evaluate("window.location.href")
            landed_realm = realm_from_url(current_url)
        except Exception as error:
            raise _MonsterLabNavigationSafetyError(
                "Unable to verify the Monster Lab URL"
            ) from error
        if landed_realm is not Realm.PERSISTENT:
            raise _MonsterLabNavigationSafetyError(
                "Monster Lab navigation landed in the wrong realm"
            )
        if not isinstance(current_url, str):
            raise _MonsterLabNavigationSafetyError("Monster Lab URL is invalid")
        parsed_url = urlsplit(current_url)
        if parsed_url.path != "/":
            raise _MonsterLabNavigationSafetyError(
                "Monster Lab navigation landed on an unexpected path"
            )
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
