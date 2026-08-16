"""Typed, explicit Weapon/Armor Lottery browser operations."""

import asyncio
import math
import re
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

LOTTERY_TICKET_PRICE_GP = 1_000


class LotteryKind(StrEnum):
    WEAPON = "Weapon Lottery"
    ARMOR = "Armor Lottery"


_LOTTERY_ROUTES = {
    LotteryKind.WEAPON: "lt",
    LotteryKind.ARMOR: "la",
}
_LOTTERY_URLS = {
    kind: f"{HENTAIVERSE_ROOT_URL}/?s=Bazaar&ss={route}"
    for kind, route in _LOTTERY_ROUTES.items()
}


@dataclass(frozen=True, slots=True)
class LotterySnapshot:
    kind: LotteryKind
    gp_balance: int
    tickets: int
    ticket_price_gp: int = LOTTERY_TICKET_PRICE_GP


@dataclass(frozen=True, slots=True)
class LotteryPurchaseReport:
    before: LotterySnapshot
    purchased: int
    after: LotterySnapshot

    @property
    def spent_gp(self) -> int:
        return self.purchased * self.before.ticket_price_gp


class LotteryPageError(RuntimeError):
    """The Lottery page did not expose the expected read-only structure."""


class _LotteryNavigationSafetyError(LotteryPageError):
    """The current battle state, origin, or realm could not be trusted."""


class LotterySubmissionError(RuntimeError):
    """A ticket purchase was rejected or could not be confirmed."""


class LotteryStateChangedError(RuntimeError):
    """The inspected Lottery state changed before an explicit purchase."""


class _LotteryDriver(Protocol):
    page: Any

    async def get(self, url: str) -> None: ...

    async def wait(
        self,
        fun: Any,
        ischangeurl: bool,
        sleeptime: int = 1,
    ) -> None: ...


def _parse_first_integer(text: str, *, field: str) -> int:
    match = re.search(r"\d[\d,]*", text)
    if match is None:
        raise LotteryPageError(f"Unable to parse {field} from Lottery page")
    return int(match.group(0).replace(",", ""))


class LotteryClient:
    """Inspect one lottery and submit only an explicit ticket quantity."""

    def __init__(
        self,
        driver: _LotteryDriver,
        navigation: MaintenanceNavigator,
        *,
        confirmation_checks: int = 5,
        confirmation_interval: float = 0.5,
    ) -> None:
        if (
            not isinstance(confirmation_checks, int)
            or isinstance(confirmation_checks, bool)
            or confirmation_checks < 1
        ):
            raise ValueError("confirmation_checks must be at least 1")
        if (
            isinstance(confirmation_interval, bool)
            or not math.isfinite(confirmation_interval)
            or confirmation_interval < 0
        ):
            raise ValueError(
                "confirmation_interval must be a finite non-negative number"
            )
        self.driver = driver
        self.navigation = navigation
        self.confirmation_checks = confirmation_checks
        self.confirmation_interval = confirmation_interval

    @property
    def page(self) -> Any:
        return self.driver.page

    async def inspect(self, kind: LotteryKind) -> LotterySnapshot:
        """Navigate to and inspect one lottery without purchasing tickets."""
        if not isinstance(kind, LotteryKind):
            raise TypeError("kind must be a LotteryKind")
        await self._navigate(kind)
        try:
            return await self._inspect_current(kind)
        except LotteryPageError as error:
            logger.warning(
                "Lottery page was not readable after navigation; "
                "reloading once through the Persistent direct URL: "
                "kind=%s error_type=%s",
                kind.value,
                type(error).__name__,
            )

        await self._open_directly(kind)
        return await self._inspect_current(kind)

    async def purchase(
        self,
        kind: LotteryKind,
        amount: int,
        *,
        expected_before: LotterySnapshot | None = None,
    ) -> LotteryPurchaseReport:
        """Purchase exactly ``amount`` tickets and verify tickets and GP."""
        if not isinstance(kind, LotteryKind):
            raise TypeError("kind must be a LotteryKind")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("Lottery purchase amount must be a positive integer")
        if expected_before is not None and not isinstance(
            expected_before, LotterySnapshot
        ):
            raise TypeError("expected_before must be a LotterySnapshot or None")

        before = await self.inspect(kind)
        if expected_before is not None and before != expected_before:
            raise LotteryStateChangedError(
                f"{kind.value} state changed before purchase; inspect and plan again"
            )
        cost = amount * before.ticket_price_gp
        if cost > before.gp_balance:
            raise ValueError(
                f"Insufficient GP for {amount} {kind.value} tickets "
                f"({cost} required, {before.gp_balance} available)"
            )

        try:
            ticket_input = await self.page.select("#ticket_temp")
        except Exception as error:
            raise LotteryPageError("Lottery ticket input is missing") from error
        if ticket_input is None:
            raise LotteryPageError("Lottery ticket input is missing")

        try:
            can_submit = await self.page.evaluate("typeof submit_buy === 'function'")
        except Exception as error:
            raise LotteryPageError("Unable to inspect Lottery purchase API") from error
        if can_submit is not True:
            raise LotteryPageError("Lottery purchase API is missing")

        try:
            await ticket_input.clear_input()
            await ticket_input.send_keys(str(amount))
        except Exception as error:
            raise LotteryPageError("Unable to prepare Lottery purchase") from error

        try:
            await self.page.evaluate("submit_buy()")
        except Exception as error:
            raise LotterySubmissionError(
                f"{kind.value} purchase outcome is unknown"
            ) from error

        expected_tickets = before.tickets + amount
        expected_gp = before.gp_balance - cost
        last_snapshot: LotterySnapshot | None = None
        last_error: Exception | None = None
        confirmation_error_count = 0
        last_confirmation_error_type: str | None = None
        for check in range(self.confirmation_checks):
            if check:
                await asyncio.sleep(self.confirmation_interval)
            try:
                last_snapshot = await self._inspect_current(kind)
            except Exception as error:
                last_error = error
                confirmation_error_count += 1
                last_confirmation_error_type = type(error).__name__
                continue
            last_error = None
            if (
                last_snapshot.tickets == expected_tickets
                and last_snapshot.gp_balance == expected_gp
            ):
                report = LotteryPurchaseReport(before, amount, last_snapshot)
                if confirmation_error_count:
                    logger.warning(
                        "Lottery purchase confirmation recovered after read errors: "
                        "kind=%s confirmed_attempt=%d/%d error_count=%d "
                        "last_error_type=%s",
                        kind.value,
                        check + 1,
                        self.confirmation_checks,
                        confirmation_error_count,
                        last_confirmation_error_type,
                    )
                logger.debug(
                    "Purchased %d %s tickets for %d GP",
                    amount,
                    kind.value,
                    report.spent_gp,
                )
                return report

        detail = (
            f"last observed tickets={last_snapshot.tickets}, "
            f"GP={last_snapshot.gp_balance}"
            if last_snapshot is not None
            else "no readable post-submit snapshot"
        )
        raise LotterySubmissionError(
            f"Unable to confirm {kind.value} purchase: expected "
            f"tickets={expected_tickets}, GP={expected_gp}; {detail}"
        ) from last_error

    async def _navigate(self, kind: LotteryKind) -> None:
        try:
            await self._open_from_menu(kind)
            return
        except MaintenanceNavigationBlockedError:
            raise
        except _LotteryNavigationSafetyError:
            raise
        except LotteryPageError as error:
            logger.warning(
                "Lottery menu navigation did not open the requested page; "
                "retrying once through the Persistent direct URL: "
                "kind=%s error_type=%s",
                kind.value,
                type(error).__name__,
            )

        await self._open_directly(kind)

    async def _open_from_menu(self, kind: LotteryKind) -> None:
        try:
            bazaar = await self.navigation.select_bazaar(
                Realm.PERSISTENT,
                navigate_first=True,
            )
        except MaintenanceNavigationBlockedError:
            raise
        except Exception as error:
            raise LotteryPageError("Bazaar menu is missing") from error

        route = _LOTTERY_ROUTES[kind]
        menu_xpath = (
            "//*[@id='child_Bazaar']"
            f"//*[@onclick and contains(@onclick, 's=Bazaar') "
            f"and contains(@onclick, 'ss={route}')]"
            f" | //*[@id='child_Bazaar']//a[contains(@href, 's=Bazaar') "
            f"and contains(@href, 'ss={route}')]"
        )
        try:
            elements = await self.page.xpath(menu_xpath, timeout=5)
        except Exception as error:
            raise LotteryPageError(f"Unable to find {kind.value} menu entry") from error
        if not elements:
            raise LotteryPageError(f"Unable to find {kind.value} menu entry")

        try:
            await bazaar.mouse_move()
            await elements[0].mouse_move()
            await self.driver.wait(
                elements[0].mouse_click,
                ischangeurl=True,
            )
        except Exception as error:
            raise LotteryPageError(f"Unable to open {kind.value}") from error

        await self._verify_destination(kind)

    async def _open_directly(self, kind: LotteryKind) -> None:
        await self._ensure_navigation_is_safe("before direct Lottery navigation")
        try:
            await self.driver.get(_LOTTERY_URLS[kind])
        except Exception as error:
            try:
                await self._ensure_navigation_is_safe("after direct Lottery navigation")
            except MaintenanceNavigationBlockedError as blocked:
                raise blocked from error
            except _LotteryNavigationSafetyError as safety_error:
                raise safety_error from error
            raise LotteryPageError(
                f"Unable to open {kind.value} through its direct URL"
            ) from error

        await self._verify_destination(kind)

    async def _ensure_navigation_is_safe(self, context: str) -> None:
        try:
            blocker = await classify_maintenance_navigation_blocker(self.page)
        except Exception as error:
            raise _LotteryNavigationSafetyError(
                f"Unable to verify battle state {context}"
            ) from error
        if blocker is not None:
            raise MaintenanceNavigationBlockedError(blocker)

    async def _verify_destination(self, kind: LotteryKind) -> None:
        await self._ensure_navigation_is_safe(f"after opening {kind.value}")
        try:
            current_url = await self.page.evaluate("window.location.href")
            landed_realm = realm_from_url(current_url)
        except Exception as error:
            raise _LotteryNavigationSafetyError(
                f"Unable to verify the {kind.value} URL"
            ) from error
        if landed_realm is not Realm.PERSISTENT:
            raise _LotteryNavigationSafetyError(
                "Lottery navigation landed in the wrong realm"
            )
        if not isinstance(current_url, str):
            raise _LotteryNavigationSafetyError("Lottery URL is invalid")
        parsed_url = urlsplit(current_url)
        if parsed_url.path != "/":
            raise _LotteryNavigationSafetyError(
                "Lottery navigation landed on an unexpected path"
            )
        query = parse_qs(parsed_url.query, keep_blank_values=True)
        expected_query = {
            "s": ["Bazaar"],
            "ss": [_LOTTERY_ROUTES[kind]],
        }
        if any(query.get(key) != value for key, value in expected_query.items()):
            raise LotteryPageError(
                f"Lottery navigation did not land on the {kind.value} route"
            )

    async def _inspect_current(self, kind: LotteryKind) -> LotterySnapshot:
        try:
            balance_elements = await self.page.xpath(
                "//*[contains(text(), 'You currently have')]", timeout=5
            )
        except Exception as error:
            raise LotteryPageError("Lottery GP balance is missing") from error
        if not balance_elements:
            raise LotteryPageError("Lottery GP balance is missing")
        try:
            ticket_elements = await self.page.xpath(
                "//*[contains(text(), 'You hold')]", timeout=5
            )
        except Exception as error:
            raise LotteryPageError("Lottery ticket count is missing") from error
        if not ticket_elements:
            raise LotteryPageError("Lottery ticket count is missing")

        return LotterySnapshot(
            kind=kind,
            gp_balance=_parse_first_integer(
                balance_elements[0].text, field="GP balance"
            ),
            tickets=_parse_first_integer(ticket_elements[0].text, field="ticket count"),
        )
