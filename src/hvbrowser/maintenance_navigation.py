"""Fail-closed navigation shared by HentaiVerse maintenance clients."""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol, cast

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


class _MaintenanceNavigationDriver(Protocol):
    page: Any

    async def gohomepage(self, force: bool = False) -> None: ...


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


async def select_bazaar_for_maintenance(
    driver: _MaintenanceNavigationDriver,
) -> Any:
    """Return persistent Bazaar after at most one marker-free retry."""

    async def navigate_persistent_home() -> None:
        await driver.gohomepage(force=True)

    return await select_bazaar_with_safe_retry(
        driver,
        navigate_home=navigate_persistent_home,
        navigate_first=True,
    )


async def select_bazaar_with_safe_retry(
    driver: _MaintenanceNavigationDriver,
    *,
    navigate_home: Callable[[], Awaitable[None]],
    navigate_first: bool,
) -> Any:
    """Select Bazaar with one marker-free retry through the supplied realm."""
    last_missing_error: TimeoutError | None = None
    for attempt in range(2):
        # A Persistent post-battle task must be allowed to leave its own
        # positively observed completion page once. Repair and every retry
        # still classify the current page before navigating.
        if not (navigate_first and attempt == 0):
            blocker_error = _blocked_error(
                await classify_maintenance_navigation_blocker(driver.page)
            )
            if blocker_error is not None:
                raise blocker_error

        if navigate_first or attempt > 0:
            await navigate_home()
            blocker_error = _blocked_error(
                await classify_maintenance_navigation_blocker(driver.page)
            )
            if blocker_error is not None:
                raise blocker_error

        try:
            bazaar = await driver.page.select("#parent_Bazaar")
        except TimeoutError as error:
            blocker_error = _blocked_error(
                await classify_maintenance_navigation_blocker(driver.page)
            )
            if blocker_error is not None:
                raise blocker_error from error
            last_missing_error = error
        else:
            if bazaar is not None:
                return bazaar
            blocker_error = _blocked_error(
                await classify_maintenance_navigation_blocker(driver.page)
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
