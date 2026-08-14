"""Composition root for one authenticated HentaiVerse browser session."""

from collections.abc import Awaitable, Callable
from typing import Any, Self

from .equipment_repair import EquipmentRepairClient
from .hv import HVDriver
from .lottery import LotteryClient
from .maintenance_navigation import MaintenanceNavigator
from .market import MarketClient
from .monster_lab import MonsterLabClient
from .player import PlayerClient
from .realm import Realm, RealmNavigator

BrowserReadyHook = Callable[[], Awaitable[None]]


class HentaiVerseSession:
    """Own one browser and its explicitly composed HentaiVerse components."""

    def __init__(
        self,
        *args: Any,
        browser: HVDriver | None = None,
        **kwargs: Any,
    ) -> None:
        if browser is not None and (args or kwargs):
            raise TypeError(
                "Browser options cannot be combined with an injected browser"
            )
        self.browser = browser if browser is not None else HVDriver(*args, **kwargs)
        self.realm = RealmNavigator(self.browser)
        self.maintenance_navigation = MaintenanceNavigator(self.browser, self.realm)
        self.player = PlayerClient(self.browser)
        self.equipment = EquipmentRepairClient(
            self.browser,
            self.realm,
            self.maintenance_navigation,
        )
        self.market = MarketClient(self.browser, self.realm)
        self.lottery = LotteryClient(self.browser, self.maintenance_navigation)
        self.monster_lab = MonsterLabClient(
            self.browser,
            self.maintenance_navigation,
        )
        self._started = False

    async def start(
        self,
        *,
        on_browser_ready: BrowserReadyHook | None = None,
    ) -> Self:
        """Initialize, optionally configure, authenticate, and enter Persistent."""
        if self._started:
            raise RuntimeError("HentaiVerse session is already started")
        try:
            await self.browser._init_browser()
            if on_browser_ready is not None:
                await on_browser_ready()
            await self.browser.login()
            await self.realm.go_home(Realm.PERSISTENT)
        except BaseException as error:
            await self.browser.__aexit__(type(error), error, error.__traceback__)
            raise
        self._started = True
        return self

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: Any,
        exc_value: Any,
        traceback: Any,
    ) -> None:
        try:
            await self.browser.__aexit__(exc_type, exc_value, traceback)
        finally:
            self._started = False
