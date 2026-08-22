"""Typed, explicit Weapon/Armor Lottery browser operations."""

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
    setup_logger,
    wait_for_page_state,
)
from .urls import HENTAIVERSE_ROOT_URL

logger = setup_logger(__name__)

LOTTERY_TICKET_PRICE_GP = 1_000

_LOTTERY_STATE_SCRIPT = r"""
(() => {
    const nodes = Array.from(document.querySelectorAll("body *"));
    const balance = nodes.find((node) =>
        (node.textContent || "").includes("You currently have")
    );
    const tickets = nodes.find((node) =>
        (node.textContent || "").includes("You hold")
    );
    const error = document.querySelector("p.messagebox_error");
    return {
        balanceText: balance ? balance.textContent : null,
        ticketText: tickets ? tickets.textContent : null,
        hasTicketInput: Boolean(document.getElementById("ticket_temp")),
        canSubmit: typeof submit_buy === "function",
        errorText: error ? error.textContent : null,
    };
})()
"""


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
    balance_text: str | None
    ticket_text: str | None
    has_ticket_input: bool
    can_submit: bool
    error_text: str | None


def _decode_lottery_state(raw: object) -> _LotteryPageState:
    if not isinstance(raw, dict):
        raise LotteryPageError("Lottery state payload is invalid")
    payload = cast(dict[object, object], raw)
    balance_text = payload.get("balanceText")
    ticket_text = payload.get("ticketText")
    has_ticket_input = payload.get("hasTicketInput")
    can_submit = payload.get("canSubmit")
    error_text = payload.get("errorText")
    if (
        (balance_text is not None and not isinstance(balance_text, str))
        or (ticket_text is not None and not isinstance(ticket_text, str))
        or type(has_ticket_input) is not bool
        or type(can_submit) is not bool
        or (error_text is not None and not isinstance(error_text, str))
    ):
        raise LotteryPageError("Lottery state payload is invalid")
    return _LotteryPageState(
        balance_text,
        ticket_text,
        has_ticket_input,
        can_submit,
        error_text,
    )


def _parse_first_integer(text: str, *, field: str) -> int:
    match = re.search(r"\d[\d,]*", text)
    if match is None:
        raise LotteryPageError(f"Unable to parse {field} from Lottery page")
    return int(match.group(0).replace(",", ""))


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

        expected_tickets = before.tickets + amount
        expected_gp = before.gp_balance - cost
        preparation_deadline = Deadline.after(LOCAL_DOM_STATE_TIMEOUT_SECONDS)
        try:
            form_state = await self._read_state(deadline=preparation_deadline)
            if self._snapshot(kind, form_state) != before:
                raise LotteryStateChangedError(
                    f"{kind.value} state changed before purchase; inspect and plan again"
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
                accept=lambda state: state.error_text is not None
                or self._matches(state, expected_gp, expected_tickets),
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
        if state.balance_text is None:
            raise LotteryPageError("Lottery GP balance is missing")
        if state.ticket_text is None:
            raise LotteryPageError("Lottery ticket count is missing")
        return LotterySnapshot(
            kind=kind,
            gp_balance=_parse_first_integer(
                state.balance_text,
                field="GP balance",
            ),
            tickets=_parse_first_integer(
                state.ticket_text,
                field="ticket count",
            ),
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
