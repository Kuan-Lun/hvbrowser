"""Atomic page identity and battle-state observations for maintenance clients."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from .realm import Realm, RealmDetectionError, realm_from_url
from .runtime import evaluate_page

_MAINTENANCE_NAVIGATION_OBSERVATION_SCRIPT = r"""
(() => {
    const completion = document.getElementById("pane_completion");
    return {
        url: window.location.href,
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


class MaintenanceNavigationContext(StrEnum):
    """Why a client is about to leave its current trusted page."""

    ORDINARY = "ordinary"
    POST_BATTLE = "post-battle"


@dataclass(frozen=True, slots=True)
class MaintenanceNavigationObservation:
    """One atomic URL identity and battle-marker observation.

    ``realm`` is ``None`` when ``url`` does not identify a trusted HentaiVerse
    origin. Callers must validate realm and root path before using ``blocker``.
    """

    url: str
    realm: Realm | None
    blocker: MaintenanceNavigationBlocker | None


class MaintenanceNavigationBlockedError(RuntimeError):
    """Trusted maintenance navigation observed a battle page and stopped."""

    def __init__(self, blocker: MaintenanceNavigationBlocker) -> None:
        self.blocker = blocker
        super().__init__(
            f"Maintenance navigation blocked: battle_state={blocker.value}"
        )


async def observe_maintenance_navigation(
    page: Any,
) -> MaintenanceNavigationObservation:
    """Read and validate page identity and all battle markers atomically."""
    raw = await evaluate_page(
        page,
        _MAINTENANCE_NAVIGATION_OBSERVATION_SCRIPT,
    )
    if not isinstance(raw, dict):
        raise RuntimeError("Invalid maintenance navigation observation payload")
    payload = cast(dict[object, object], raw)
    url = payload.get("url")
    if not isinstance(url, str):
        raise RuntimeError("Invalid maintenance navigation observation payload")
    marker_names = ("challenge", "completion", "nextFloor", "active")
    if any(type(payload.get(name)) is not bool for name in marker_names):
        raise RuntimeError("Invalid maintenance navigation observation payload")

    blocker: MaintenanceNavigationBlocker | None = None
    if payload["challenge"]:
        blocker = MaintenanceNavigationBlocker.CHALLENGE
    elif payload["completion"]:
        blocker = MaintenanceNavigationBlocker.COMPLETION
    elif payload["nextFloor"]:
        blocker = MaintenanceNavigationBlocker.NEXT_FLOOR
    elif payload["active"]:
        blocker = MaintenanceNavigationBlocker.ACTIVE

    try:
        realm = realm_from_url(url)
    except RealmDetectionError:
        realm = None
    return MaintenanceNavigationObservation(url, realm, blocker)
