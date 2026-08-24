"""Typed, explicit Monster Lab feed-all browser operations."""

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast
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
    SERVER_STATE_RECEIPT_TIMEOUT_SECONDS,
    Deadline,
    PageStateTimeout,
    evaluate_page,
    invoke_mutation,
    is_browser_generation_error,
    wait_for_page_state,
)
from .urls import HENTAIVERSE_ROOT_URL

logger = logging.getLogger(__name__)


class MonsterLabFeed(StrEnum):
    FOOD = "food"
    DRUGS = "drugs"


_MONSTER_LAB_ROUTE = "ml"
_MONSTER_LAB_URL = f"{HENTAIVERSE_ROOT_URL}/?s=Bazaar&ss={_MONSTER_LAB_ROUTE}"
_MONSTER_LAB_STATE_SCRIPT = r"""
(() => {
    const actionAvailable = (action) => Boolean(document.querySelector(
        `img[src="/y/monster/${action}allmonsters.png"]`
    ));
    const error = document.querySelector("p.messagebox_error");
    return {
        hasApi: typeof do_feed_all === "function",
        foodAvailable: actionAvailable("feed"),
        drugsAvailable: actionAvailable("drug"),
        errorText: error ? error.textContent : null,
    };
})()
"""


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


@dataclass(frozen=True, slots=True)
class _MonsterLabPageState:
    has_api: bool
    available_feed_all: frozenset[MonsterLabFeed]
    error_text: str | None


def _decode_monster_lab_state(raw: object) -> _MonsterLabPageState:
    if not isinstance(raw, dict):
        raise MonsterLabPageError("Monster Lab state payload is invalid")
    payload = cast(dict[object, object], raw)
    has_api = payload.get("hasApi")
    food_available = payload.get("foodAvailable")
    drugs_available = payload.get("drugsAvailable")
    error_text = payload.get("errorText")
    if (
        type(has_api) is not bool
        or type(food_available) is not bool
        or type(drugs_available) is not bool
        or (error_text is not None and not isinstance(error_text, str))
    ):
        raise MonsterLabPageError("Monster Lab state payload is invalid")
    available: set[MonsterLabFeed] = set()
    if food_available:
        available.add(MonsterLabFeed.FOOD)
    if drugs_available:
        available.add(MonsterLabFeed.DRUGS)
    return _MonsterLabPageState(has_api, frozenset(available), error_text)


class MonsterLabClient:
    """Inspect Monster Lab and invoke one explicit feed-all resource."""

    def __init__(self, driver: _MonsterLabDriver) -> None:
        self.driver = driver

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
        deadline = Deadline.after(SERVER_STATE_RECEIPT_TIMEOUT_SECONDS)
        try:
            submitted = await invoke_mutation(
                lambda: self.page.evaluate(f"""
                    (() => {{
                        if (typeof do_feed_all !== 'function') return false;
                        do_feed_all({resource_js});
                        return true;
                    }})()
                    """),
                owner=self.page,
                operation=f"Monster Lab {resource.value} feed-all",
                deadline=deadline,
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

        try:
            after_state = await wait_for_page_state(
                self.page,
                snapshot_expression=_MONSTER_LAB_STATE_SCRIPT,
                decode=_decode_monster_lab_state,
                accept=lambda state: state.error_text is not None
                or (state.has_api and resource not in state.available_feed_all),
                deadline=deadline,
                description=f"Monster Lab {resource.value} feed-all result",
            )
        except (PageStateTimeout, MonsterLabPageError) as error:
            raise MonsterLabSubmissionError(
                f"Unable to confirm Monster Lab {resource.value} feed-all"
            ) from error
        if after_state.error_text is not None:
            message = after_state.error_text.strip() or "feed-all rejected"
            raise MonsterLabSubmissionError(
                f"Monster Lab rejected {resource.value} feed-all: {message}"
            )
        if not after_state.has_api:
            raise MonsterLabSubmissionError(
                f"Unable to confirm Monster Lab {resource.value} feed-all"
            )
        after = MonsterLabSnapshot(after_state.available_feed_all)
        logger.debug("Fed all eligible monsters with %s", resource.value)
        return MonsterLabFeedReport(resource, True, before, after)

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
        state = await self._read_state()
        if not state.has_api:
            raise MonsterLabPageError("Monster Lab feed-all API is missing")
        return MonsterLabSnapshot(state.available_feed_all)

    async def _read_state(
        self,
        *,
        deadline: Deadline | None = None,
    ) -> _MonsterLabPageState:
        try:
            return _decode_monster_lab_state(
                await evaluate_page(
                    self.page,
                    _MONSTER_LAB_STATE_SCRIPT,
                    deadline=deadline,
                )
            )
        except Exception as error:
            if is_browser_generation_error(error) or isinstance(
                error, MonsterLabPageError
            ):
                raise
            raise MonsterLabPageError("Unable to inspect Monster Lab state") from error
