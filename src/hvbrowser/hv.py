"""Low-level authenticated browser transport for HentaiVerse."""

from typing import Any

from hbrowser.gallery import EHDriver

from .urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL


class HVDriver(EHDriver):
    """Authenticated browser lifecycle without HentaiVerse domain workflows."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.url[self.name] = HENTAIVERSE_ROOT_URL
        self.url["HentaiVerse isekai"] = HENTAIVERSE_ISEKAI_ROOT_URL

    def _setname(self) -> str:
        return "HentaiVerse"
