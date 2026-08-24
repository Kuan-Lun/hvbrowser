"""Low-level authenticated browser transport for HentaiVerse."""

from typing import Any

from hbrowser.gallery import EHDriver

from .runtime import BrowserOperationDeadline
from .urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL


class HVDriver(EHDriver):
    """Authenticated browser lifecycle without HentaiVerse domain workflows."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.url[self.name] = HENTAIVERSE_ROOT_URL
        self.url["HentaiVerse isekai"] = HENTAIVERSE_ISEKAI_ROOT_URL

    def _setname(self) -> str:
        return "HentaiVerse"

    async def navigate_with_budget(
        self,
        url: str,
        *,
        budget_seconds: float,
    ) -> None:
        """Navigate within a caller-owned remaining-time budget.

        Domain packages own their semantic deadlines; hbrowser owns the
        concrete deadline required by its multi-phase navigation machinery.
        Accepting the remaining budget here keeps that implementation type on
        its owning side of the package boundary.
        """

        deadline = BrowserOperationDeadline.after(budget_seconds)
        if deadline.expired:
            raise TimeoutError("navigation budget expired before dispatch")
        await self.get(url, deadline=deadline)
