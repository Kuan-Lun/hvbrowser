"""Typed HentaiVerse realm inspection and navigation."""

from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from .runtime import setup_logger, wait_for_zendriver
from .urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL

logger = setup_logger(__name__)

_CURRENT_URL_TIMEOUT_SECONDS = 8.0


class Realm(StrEnum):
    """One of the two independent HentaiVerse realms."""

    PERSISTENT = "persistent"
    ISEKAI = "isekai"


class RealmDetectionError(RuntimeError):
    """The current page does not identify a trusted HentaiVerse realm."""


class _RealmDriver(Protocol):
    page: Any

    async def get(self, url: str) -> None: ...

    async def gohomepage(self, force: bool = False) -> None: ...


def realm_from_url(url: object) -> Realm:
    """Return the trusted realm encoded by a HentaiVerse URL."""
    if not isinstance(url, str):
        raise RealmDetectionError("Unable to determine realm from the current URL")

    parsed = urlsplit(url)
    expected = urlsplit(HENTAIVERSE_ROOT_URL)
    try:
        parsed_port = parsed.port
        expected_port = expected.port
        matches_origin = (
            parsed.scheme.casefold() == expected.scheme
            and parsed.hostname == expected.hostname
            and (443 if parsed_port is None else parsed_port)
            == (443 if expected_port is None else expected_port)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError as error:
        raise RealmDetectionError(
            "Unable to determine realm from the current URL"
        ) from error
    if not matches_origin:
        raise RealmDetectionError("Unable to determine realm outside HentaiVerse")

    if parsed.path == "/isekai" or parsed.path.startswith("/isekai/"):
        return Realm.ISEKAI
    return Realm.PERSISTENT


class RealmNavigator:
    """Inspect and navigate realms through one shared browser session."""

    def __init__(self, driver: _RealmDriver) -> None:
        self._driver = driver

    async def current(self) -> Realm:
        url = await wait_for_zendriver(
            self._driver.page.evaluate("window.location.href"),
            timeout=_CURRENT_URL_TIMEOUT_SECONDS,
            owner=self._driver.page,
        )
        return realm_from_url(url)

    async def go_home(self, realm: Realm, *, force: bool = False) -> None:
        if not isinstance(realm, Realm):
            raise TypeError("realm must be a Realm")
        if realm is Realm.PERSISTENT:
            await self._driver.gohomepage(force=force)
            return

        logger.debug(
            "Navigate to HentaiVerse Isekai root: target=%s forced=%s",
            HENTAIVERSE_ISEKAI_ROOT_URL,
            force,
        )
        await self._driver.get(HENTAIVERSE_ISEKAI_ROOT_URL)
