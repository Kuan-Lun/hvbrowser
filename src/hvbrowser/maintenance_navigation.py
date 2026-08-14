"""Fail-closed navigation shared by HentaiVerse maintenance clients."""

from enum import StrEnum
from typing import Any, Protocol, cast

from .realm import Realm, RealmNavigator
from .runtime import setup_logger

logger = setup_logger(__name__)

_MAINTENANCE_MARKERS_SCRIPT = r"""
(() => {
    const completion = document.getElementById("pane_completion");
    return {
        challenge: Boolean(document.getElementById("riddlesubmit")),
        completion: Boolean(
            completion
            && completion.querySelector('img[src*="finishbattle.png"]')
        ),
        nextFloor: Boolean(document.getElementById("btcp")),
        active: Boolean(document.getElementById("battle_main")),
    };
})()
"""


class MaintenanceNavigationBlocker(StrEnum):
    """Battle states that make maintenance navigation unsafe."""

    CHALLENGE = "challenge"
    COMPLETION = "completion"
    NEXT_FLOOR = "next-floor"
    ACTIVE = "active"


class MaintenanceNavigationBlockedError(RuntimeError):
    """Maintenance landed on a battle page and stopped safely."""

    def __init__(self, blocker: MaintenanceNavigationBlocker) -> None:
        self.blocker = blocker
        super().__init__(
            f"Maintenance navigation blocked: battle_state={blocker.value}"
        )


class _MaintenanceDriver(Protocol):
    page: Any


async def classify_maintenance_navigation_blocker(
    page: Any,
) -> MaintenanceNavigationBlocker | None:
    """Read all battle markers atomically and return the highest-risk state."""
    raw: object = await page.evaluate(_MAINTENANCE_MARKERS_SCRIPT)
    if not isinstance(raw, dict):
        raise RuntimeError("Invalid maintenance navigation marker payload")
    payload = cast(dict[object, object], raw)
    marker_names = ("challenge", "completion", "nextFloor", "active")
    if any(type(payload.get(name)) is not bool for name in marker_names):
        raise RuntimeError("Invalid maintenance navigation marker payload")

    if payload["challenge"]:
        return MaintenanceNavigationBlocker.CHALLENGE
    if payload["completion"]:
        return MaintenanceNavigationBlocker.COMPLETION
    if payload["nextFloor"]:
        return MaintenanceNavigationBlocker.NEXT_FLOOR
    if payload["active"]:
        return MaintenanceNavigationBlocker.ACTIVE
    return None


def _blocked_error(
    blocker: MaintenanceNavigationBlocker | None,
) -> MaintenanceNavigationBlockedError | None:
    return MaintenanceNavigationBlockedError(blocker) if blocker is not None else None


class MaintenanceNavigator:
    """Open Bazaar with realm-aware navigation and one safe retry."""

    def __init__(
        self,
        driver: _MaintenanceDriver,
        realm_navigator: RealmNavigator,
    ) -> None:
        self._driver = driver
        self._realm = realm_navigator

    async def select_bazaar(
        self,
        realm: Realm,
        *,
        navigate_first: bool,
    ) -> Any:
        if not isinstance(realm, Realm):
            raise TypeError("realm must be a Realm")
        if not isinstance(navigate_first, bool):
            raise TypeError("navigate_first must be bool")

        last_missing_error: TimeoutError | None = None
        for attempt in range(2):
            blocker = await classify_maintenance_navigation_blocker(self._driver.page)
            may_leave_completion = (
                navigate_first
                and attempt == 0
                and blocker is MaintenanceNavigationBlocker.COMPLETION
            )
            if blocker is not None and not may_leave_completion:
                raise MaintenanceNavigationBlockedError(blocker)

            if navigate_first or attempt > 0:
                await self._realm.go_home(realm, force=True)
                blocker_error = _blocked_error(
                    await classify_maintenance_navigation_blocker(self._driver.page)
                )
                if blocker_error is not None:
                    raise blocker_error

            try:
                bazaar = await self._driver.page.select("#parent_Bazaar")
            except TimeoutError as error:
                blocker_error = _blocked_error(
                    await classify_maintenance_navigation_blocker(self._driver.page)
                )
                if blocker_error is not None:
                    raise blocker_error from error
                last_missing_error = error
            else:
                if bazaar is not None:
                    return bazaar
                blocker_error = _blocked_error(
                    await classify_maintenance_navigation_blocker(self._driver.page)
                )
                if blocker_error is not None:
                    raise blocker_error
                last_missing_error = TimeoutError(
                    "Bazaar menu selection returned no element"
                )

            if attempt == 0:
                logger.warning(
                    "Bazaar menu is unavailable without battle markers; "
                    "reloading the same maintenance realm and retrying once"
                )

        if last_missing_error is None:
            raise RuntimeError("Maintenance navigation ended without a result")
        raise last_missing_error
