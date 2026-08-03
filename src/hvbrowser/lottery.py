"""Typed, explicit Weapon/Armor Lottery browser operations."""

import asyncio
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .maintenance_navigation import (
    MaintenanceNavigationBlockedError,
    select_bazaar_for_maintenance,
)
from .runtime import setup_logger

logger = setup_logger(__name__)

LOTTERY_TICKET_PRICE_GP = 1_000


class LotteryKind(StrEnum):
    WEAPON = "Weapon Lottery"
    ARMOR = "Armor Lottery"


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


class LotterySubmissionError(RuntimeError):
    """A ticket purchase was rejected or could not be confirmed."""


class LotteryStateChangedError(RuntimeError):
    """The inspected Lottery state changed before an explicit purchase."""


class _LotteryDriver(Protocol):
    page: Any

    async def gohomepage(self, force: bool = False) -> None: ...


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

    async def inspect(self, kind: LotteryKind) -> LotterySnapshot:
        """Navigate to and inspect one lottery without purchasing tickets."""
        if not isinstance(kind, LotteryKind):
            raise TypeError("kind must be a LotteryKind")
        await self._navigate(kind)
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
        for check in range(self.confirmation_checks):
            if check:
                await asyncio.sleep(self.confirmation_interval)
            try:
                last_snapshot = await self._inspect_current(kind)
            except Exception as error:
                last_error = error
                continue
            last_error = None
            if (
                last_snapshot.tickets == expected_tickets
                and last_snapshot.gp_balance == expected_gp
            ):
                report = LotteryPurchaseReport(before, amount, last_snapshot)
                logger.info(
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
            bazaar = await select_bazaar_for_maintenance(self.driver)
        except MaintenanceNavigationBlockedError:
            raise
        except Exception as error:
            raise LotteryPageError("Bazaar menu is missing") from error
        try:
            elements = await self.page.xpath(
                f"//div[contains(text(), '{kind.value}')]", timeout=5
            )
        except Exception as error:
            raise LotteryPageError(f"Unable to find {kind.value} menu entry") from error
        if not elements:
            raise LotteryPageError(f"Unable to find {kind.value} menu entry")

        try:
            await bazaar.mouse_move()
            await elements[0].mouse_move()
            await elements[0].mouse_click()
            await self.page.wait(1)
        except Exception as error:
            raise LotteryPageError(f"Unable to open {kind.value}") from error

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
