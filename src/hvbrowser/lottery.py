"""Typed, explicit Weapon/Armor Lottery browser operations."""

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast
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
    LOCAL_DOM_STATE_TIMEOUT_SECONDS,
    SERVER_STATE_RECEIPT_TIMEOUT_SECONDS,
    Deadline,
    PageStateTimeout,
    evaluate_page,
    invoke_mutation,
    is_browser_generation_error,
    query_page,
    wait_for_page_state,
)
from .urls import HENTAIVERSE_ROOT_URL

logger = logging.getLogger(__name__)

LOTTERY_TICKET_PRICE_GP = 1_000

_LOTTERY_STATE_SCRIPT = r"""
(() => {
    // hvbrowser-lottery-state-v2
    const pageText = document.body ? document.body.innerText : null;
    const error = document.querySelector("p.messagebox_error");
    return {
        pageText,
        hasTicketInput: Boolean(document.getElementById("ticket_temp")),
        canSubmit: typeof submit_buy === "function",
        errorText: error ? error.innerText : null,
    };
})()
"""

_LOTTERY_AMOUNT_PATTERN = r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)"
_LOTTERY_TICKET_COUNT_LABEL = re.compile(r"You\s+hold\b")
_LOTTERY_TICKET_COUNT = re.compile(
    rf"You\s+hold\s+(?P<amount>{_LOTTERY_AMOUNT_PATTERN})\s+tickets?\b"
)
_LOTTERY_GP_BALANCE_LABEL = re.compile(r"You\s+currently\s+have\b")
_LOTTERY_GP_BALANCE = re.compile(
    rf"You\s+currently\s+have\s+(?P<amount>{_LOTTERY_AMOUNT_PATTERN})\s+GP\b"
)
_LOTTERY_TICKET_PRICE_LABEL = re.compile(r"Each\s+ticket\s+costs\b")
_LOTTERY_TICKET_PRICE = re.compile(
    rf"Each\s+ticket\s+costs\s+(?P<amount>{_LOTTERY_AMOUNT_PATTERN})\s+GP\b"
)


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


@dataclass(frozen=True, slots=True)
class _LotteryPageState:
    page_text: str | None
    has_ticket_input: bool
    can_submit: bool
    error_text: str | None


@dataclass(frozen=True, slots=True)
class _LotteryAmounts:
    gp_balance: int
    tickets: int
    ticket_price_gp: int


def _decode_lottery_state(raw: object) -> _LotteryPageState:
    if not isinstance(raw, dict):
        raise LotteryPageError("Lottery state payload is invalid")
    payload = cast(dict[object, object], raw)
    page_text = payload.get("pageText")
    has_ticket_input = payload.get("hasTicketInput")
    can_submit = payload.get("canSubmit")
    error_text = payload.get("errorText")
    if (
        (page_text is not None and not isinstance(page_text, str))
        or type(has_ticket_input) is not bool
        or type(can_submit) is not bool
        or (error_text is not None and not isinstance(error_text, str))
    ):
        raise LotteryPageError("Lottery state payload is invalid")
    return _LotteryPageState(
        page_text,
        has_ticket_input,
        can_submit,
        error_text,
    )


def _parse_unique_lottery_amount(
    page_text: str,
    *,
    label_pattern: re.Pattern[str],
    value_pattern: re.Pattern[str],
    field: str,
) -> int:
    label_count = sum(1 for _ in label_pattern.finditer(page_text))
    matches = tuple(value_pattern.finditer(page_text))
    if label_count != 1 or len(matches) != 1:
        raise LotteryPageError(f"Lottery {field} is missing, malformed, or ambiguous")
    return int(matches[0].group("amount").replace(",", ""))


def _parse_lottery_amounts(page_text: str) -> _LotteryAmounts:
    normalized = page_text.replace("\N{NO-BREAK SPACE}", " ")
    tickets = _parse_unique_lottery_amount(
        normalized,
        label_pattern=_LOTTERY_TICKET_COUNT_LABEL,
        value_pattern=_LOTTERY_TICKET_COUNT,
        field="ticket count",
    )
    gp_balance = _parse_unique_lottery_amount(
        normalized,
        label_pattern=_LOTTERY_GP_BALANCE_LABEL,
        value_pattern=_LOTTERY_GP_BALANCE,
        field="GP balance",
    )
    ticket_price_gp = _parse_unique_lottery_amount(
        normalized,
        label_pattern=_LOTTERY_TICKET_PRICE_LABEL,
        value_pattern=_LOTTERY_TICKET_PRICE,
        field="ticket price",
    )
    if ticket_price_gp != LOTTERY_TICKET_PRICE_GP:
        raise LotteryPageError("Lottery ticket price is unsupported")
    return _LotteryAmounts(gp_balance, tickets, ticket_price_gp)


class LotteryClient:
    """Inspect one lottery and submit only an explicit ticket quantity."""

    def __init__(self, driver: _LotteryDriver) -> None:
        self.driver = driver

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
        try:
            return await self.inspect_once(kind, context=context)
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

    async def inspect_once(
        self,
        kind: LotteryKind,
        *,
        context: MaintenanceNavigationContext,
    ) -> LotterySnapshot:
        """Navigate and inspect exactly once for fail-closed probe composition."""
        if not isinstance(kind, LotteryKind):
            raise TypeError("kind must be a LotteryKind")
        if not isinstance(context, MaintenanceNavigationContext):
            raise TypeError("context must be a MaintenanceNavigationContext")
        await self._navigate(kind, context=context)
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

        expected_tickets = before.tickets + amount
        expected_gp = before.gp_balance - cost
        preparation_deadline = Deadline.after(LOCAL_DOM_STATE_TIMEOUT_SECONDS)
        try:
            form_state = await self._read_state(deadline=preparation_deadline)
            if self._snapshot(kind, form_state) != before:
                raise LotteryStateChangedError(
                    f"{kind.value} state changed before purchase; "
                    "inspect and plan again"
                )
            if not form_state.has_ticket_input:
                raise LotteryPageError("Lottery ticket input is missing")
            if not form_state.can_submit:
                raise LotteryPageError("Lottery purchase API is missing")
            ticket_input = await query_page(
                self.page,
                "#ticket_temp",
                deadline=preparation_deadline,
            )
            if ticket_input is None:
                raise LotteryPageError("Lottery ticket input is missing")
            await invoke_mutation(
                ticket_input.clear_input,
                owner=ticket_input,
                operation="Lottery ticket input reset",
                deadline=preparation_deadline,
            )
            await invoke_mutation(
                lambda: ticket_input.send_keys(str(amount)),
                owner=ticket_input,
                operation="Lottery ticket amount entry",
                deadline=preparation_deadline,
            )
        except Exception as error:
            if is_browser_generation_error(error) or isinstance(
                error,
                LotteryStateChangedError | LotteryPageError,
            ):
                raise
            raise LotteryPageError("Unable to prepare Lottery purchase") from error

        receipt_deadline = Deadline.after(SERVER_STATE_RECEIPT_TIMEOUT_SECONDS)
        try:
            await invoke_mutation(
                lambda: self.page.evaluate("submit_buy()"),
                owner=self.page,
                operation=f"{kind.value} purchase submission",
                deadline=receipt_deadline,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise LotterySubmissionError(
                f"{kind.value} purchase outcome is unknown"
            ) from error

        try:
            after_state = await wait_for_page_state(
                self.page,
                snapshot_expression=_LOTTERY_STATE_SCRIPT,
                decode=_decode_lottery_state,
                accept=lambda state: (
                    state.error_text is not None
                    or self._matches(state, expected_gp, expected_tickets)
                ),
                deadline=receipt_deadline,
                description=f"{kind.value} purchase result",
            )
        except PageStateTimeout as error:
            raise LotterySubmissionError(
                f"Unable to confirm {kind.value} purchase: expected "
                f"tickets={expected_tickets}, GP={expected_gp}"
            ) from error
        except LotteryPageError as error:
            raise LotterySubmissionError(
                f"Unable to confirm {kind.value} purchase"
            ) from error

        if after_state.error_text is not None:
            message = after_state.error_text.strip() or "purchase rejected"
            raise LotterySubmissionError(f"{kind.value} purchase rejected: {message}")
        after = self._snapshot(kind, after_state)
        report = LotteryPurchaseReport(before, amount, after)
        logger.debug(
            "Purchased %d %s tickets for %d GP",
            amount,
            kind.value,
            report.spent_gp,
        )
        return report

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
        return self._snapshot(kind, await self._read_state())

    async def _read_state(
        self,
        *,
        deadline: Deadline | None = None,
    ) -> _LotteryPageState:
        try:
            return _decode_lottery_state(
                await evaluate_page(
                    self.page,
                    _LOTTERY_STATE_SCRIPT,
                    deadline=deadline,
                )
            )
        except Exception as error:
            if is_browser_generation_error(error) or isinstance(
                error, LotteryPageError
            ):
                raise
            raise LotteryPageError("Unable to inspect Lottery state") from error

    @staticmethod
    def _snapshot(kind: LotteryKind, state: _LotteryPageState) -> LotterySnapshot:
        if state.page_text is None:
            raise LotteryPageError("Lottery page text is missing")
        amounts = _parse_lottery_amounts(state.page_text)
        return LotterySnapshot(
            kind=kind,
            gp_balance=amounts.gp_balance,
            tickets=amounts.tickets,
            ticket_price_gp=amounts.ticket_price_gp,
        )

    @staticmethod
    def _matches(
        state: _LotteryPageState,
        expected_gp: int,
        expected_tickets: int,
    ) -> bool:
        try:
            snapshot = LotteryClient._snapshot(LotteryKind.WEAPON, state)
        except LotteryPageError:
            return False
        return (
            snapshot.gp_balance == expected_gp and snapshot.tickets == expected_tickets
        )
