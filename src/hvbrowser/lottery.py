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
    MaintenanceNavigationBlocker,
    MaintenanceNavigationContext,
    MaintenanceNavigationObservation,
    observe_maintenance_navigation,
)
from .realm import Realm
from .runtime import (
    is_browser_generation_error,
    setup_logger,
    wait_for_zendriver,
)
from .urls import HENTAIVERSE_ROOT_URL

logger = setup_logger(__name__)

_READ_TIMEOUT_SECONDS = 8.0
_MUTATION_TIMEOUT_SECONDS = 15.0
_SELECTOR_INNER_TIMEOUT_SECONDS = 5.0
_SELECTOR_OUTER_TIMEOUT_SECONDS = 7.0

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
        self.confirmation_checks = confirmation_checks
        self.confirmation_interval = confirmation_interval

    @property
    def page(self) -> Any:
        return self.driver.page

    async def inspect(
        self,
        kind: LotteryKind,
        *,
        context: MaintenanceNavigationContext,
    ) -> LotterySnapshot:
        """Navigate to and inspect one lottery without purchasing tickets."""
        if not isinstance(kind, LotteryKind):
            raise TypeError("kind must be a LotteryKind")
        if not isinstance(context, MaintenanceNavigationContext):
            raise TypeError("context must be a MaintenanceNavigationContext")
        await self._navigate(kind, context=context)
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

        await self._open_directly(
            kind,
            context=MaintenanceNavigationContext.ORDINARY,
        )
        return await self._inspect_current(kind)

    async def purchase(
        self,
        kind: LotteryKind,
        amount: int,
        *,
        context: MaintenanceNavigationContext,
        expected_before: LotterySnapshot | None = None,
    ) -> LotteryPurchaseReport:
        """Purchase exactly ``amount`` tickets and verify tickets and GP."""
        if not isinstance(kind, LotteryKind):
            raise TypeError("kind must be a LotteryKind")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("Lottery purchase amount must be a positive integer")
        if not isinstance(context, MaintenanceNavigationContext):
            raise TypeError("context must be a MaintenanceNavigationContext")
        if expected_before is not None and not isinstance(
            expected_before, LotterySnapshot
        ):
            raise TypeError("expected_before must be a LotterySnapshot or None")

        before = await self.inspect(kind, context=context)
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
            ticket_input = await wait_for_zendriver(
                self.page.select(
                    "#ticket_temp",
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise LotteryPageError("Lottery ticket input is missing") from error
        if ticket_input is None:
            raise LotteryPageError("Lottery ticket input is missing")

        try:
            can_submit = await wait_for_zendriver(
                self.page.evaluate("typeof submit_buy === 'function'"),
                timeout=_READ_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise LotteryPageError("Unable to inspect Lottery purchase API") from error
        if can_submit is not True:
            raise LotteryPageError("Lottery purchase API is missing")

        try:
            await wait_for_zendriver(
                ticket_input.clear_input(),
                timeout=_MUTATION_TIMEOUT_SECONDS,
                owner=ticket_input,
            )
            await wait_for_zendriver(
                ticket_input.send_keys(str(amount)),
                timeout=_MUTATION_TIMEOUT_SECONDS,
                owner=ticket_input,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise LotteryPageError("Unable to prepare Lottery purchase") from error

        try:
            await wait_for_zendriver(
                self.page.evaluate("submit_buy()"),
                timeout=_MUTATION_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
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
                if is_browser_generation_error(error):
                    raise
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

    async def _navigate(
        self,
        kind: LotteryKind,
        *,
        context: MaintenanceNavigationContext = MaintenanceNavigationContext.ORDINARY,
    ) -> None:
        await self._open_directly(kind, context=context)

    async def _open_directly(
        self,
        kind: LotteryKind,
        *,
        context: MaintenanceNavigationContext = MaintenanceNavigationContext.ORDINARY,
    ) -> None:
        await self._ensure_navigation_is_safe(
            Realm.PERSISTENT,
            "before direct Lottery navigation",
            context=context,
        )
        try:
            await self.driver.get(_LOTTERY_URLS[kind])
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            try:
                await self._ensure_navigation_is_safe(
                    Realm.PERSISTENT,
                    "after direct Lottery navigation",
                    context=MaintenanceNavigationContext.ORDINARY,
                )
            except MaintenanceNavigationBlockedError as blocked:
                raise blocked from error
            except _LotteryNavigationSafetyError as safety_error:
                raise safety_error from error
            raise LotteryPageError(
                f"Unable to open {kind.value} through its direct URL"
            ) from error

        await self._verify_destination(kind)

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
            raise _LotteryNavigationSafetyError(
                f"Unable to verify battle state {phase}"
            ) from error
        if observation.realm is not expected_realm:
            raise _LotteryNavigationSafetyError(
                f"Lottery navigation is on an untrusted or wrong realm {phase}"
            )
        expected_path = "/isekai/" if expected_realm is Realm.ISEKAI else "/"
        if urlsplit(observation.url).path != expected_path:
            raise _LotteryNavigationSafetyError(
                f"Lottery navigation is on an unexpected path {phase}"
            )
        may_leave_completion = (
            context is MaintenanceNavigationContext.POST_BATTLE
            and expected_realm is Realm.PERSISTENT
            and observation.blocker is MaintenanceNavigationBlocker.COMPLETION
        )
        if observation.blocker is not None and not may_leave_completion:
            raise MaintenanceNavigationBlockedError(observation.blocker)
        return observation

    async def _verify_destination(self, kind: LotteryKind) -> None:
        observation = await self._ensure_navigation_is_safe(
            Realm.PERSISTENT,
            f"after opening {kind.value}",
            context=MaintenanceNavigationContext.ORDINARY,
        )
        parsed_url = urlsplit(observation.url)
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
            balance_elements = await wait_for_zendriver(
                self.page.xpath(
                    "//*[contains(text(), 'You currently have')]",
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise LotteryPageError("Lottery GP balance is missing") from error
        if not balance_elements:
            raise LotteryPageError("Lottery GP balance is missing")
        try:
            ticket_elements = await wait_for_zendriver(
                self.page.xpath(
                    "//*[contains(text(), 'You hold')]",
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
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
