"""Typed character-state inspection and stamina recovery operations."""

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

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

logger = logging.getLogger(__name__)

_LEVEL_PATTERN = re.compile(r"(?:^|\s)Lv\.\s*([0-9]+)\s*$")
_STAMINA_PATTERN = re.compile(r"Stamina:\s*([0-9]+)")
_RESTORATIVE_SELECTOR = (
    "img[onclick=\"document.getElementById('recoverform').submit()\"]"
)
_RECOVERY_ERROR_SELECTOR = "p.messagebox_error"
_PLAYER_STATE_SCRIPT = r"""
(() => {
    const level = document.getElementById("level_readout");
    const stamina = document.getElementById("stamina_readout") || Array.from(
        document.querySelectorAll("div")
    ).find((element) => (element.textContent || "").includes("Stamina:"));
    const restorative = document.querySelector(
        'img[onclick="document.getElementById(\'recoverform\').submit()"]'
    );
    const error = document.querySelector("p.messagebox_error");
    return {
        levelText: level ? level.textContent : null,
        staminaText: stamina ? stamina.textContent : null,
        hasStaminaReadout: Boolean(document.getElementById("stamina_readout")),
        restorativeAvailable: Boolean(restorative),
        errorText: error ? error.textContent : null,
    };
})()
"""


class StaminaRecoveryOutcome(StrEnum):
    """Known outcomes of an explicit stamina-recovery attempt."""

    RECOVERED = "recovered"
    NOT_AVAILABLE = "not-available"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    """The visible character state used by maintenance workflows."""

    level: int
    stamina: int


@dataclass(frozen=True, slots=True)
class StaminaRecoveryReport:
    """A confirmed stamina-recovery outcome."""

    outcome: StaminaRecoveryOutcome
    before: int
    after: int | None
    server_message: str | None = None

    @property
    def recovered(self) -> bool:
        return self.outcome is StaminaRecoveryOutcome.RECOVERED


@dataclass(frozen=True, slots=True)
class _PlayerPageState:
    level_text: str | None
    stamina_text: str | None
    has_stamina_readout: bool
    restorative_available: bool
    error_text: str | None


class PlayerPageError(RuntimeError):
    """The page did not expose readable character state or controls."""


class StaminaRecoveryError(RuntimeError):
    """A recovery submission was made but its outcome is unknown."""


class PlayerStateChangedError(RuntimeError):
    """The visible stamina changed after the caller inspected it."""


class _PlayerDriver(Protocol):
    page: Any


def _decode_player_state(raw: object) -> _PlayerPageState:
    if not isinstance(raw, dict):
        raise PlayerPageError("Player state payload is invalid")
    payload = cast(dict[object, object], raw)
    level_text = payload.get("levelText")
    stamina_text = payload.get("staminaText")
    has_stamina_readout = payload.get("hasStaminaReadout")
    restorative_available = payload.get("restorativeAvailable")
    error_text = payload.get("errorText")
    if (
        (level_text is not None and not isinstance(level_text, str))
        or (stamina_text is not None and not isinstance(stamina_text, str))
        or type(has_stamina_readout) is not bool
        or type(restorative_available) is not bool
        or (error_text is not None and not isinstance(error_text, str))
    ):
        raise PlayerPageError("Player state payload is invalid")
    return _PlayerPageState(
        level_text,
        stamina_text,
        has_stamina_readout,
        restorative_available,
        error_text,
    )


def _parse_level(state: _PlayerPageState) -> int:
    text = state.level_text
    if text is None:
        raise PlayerPageError("Unable to find level readout")
    match = _LEVEL_PATTERN.search(text)
    if match is None:
        raise PlayerPageError(f"Unable to parse level from: {text!r}")
    return int(match.group(1))


def _parse_stamina(state: _PlayerPageState) -> int:
    text = state.stamina_text
    if text is None:
        raise PlayerPageError("Unable to find stamina readout")
    match = _STAMINA_PATTERN.search(text)
    if match is None:
        raise PlayerPageError(f"Unable to parse stamina from: {text!r}")
    return int(match.group(1))


class PlayerClient:
    """Inspect the current character and explicitly recover stamina."""

    def __init__(self, driver: _PlayerDriver) -> None:
        self.driver = driver

    @property
    def page(self) -> Any:
        return self.driver.page

    async def _read_state(
        self,
        *,
        deadline: Deadline | None = None,
    ) -> _PlayerPageState:
        try:
            return _decode_player_state(
                await evaluate_page(
                    self.page,
                    _PLAYER_STATE_SCRIPT,
                    deadline=deadline,
                )
            )
        except Exception as error:
            if is_browser_generation_error(error) or isinstance(error, PlayerPageError):
                raise
            raise PlayerPageError("Unable to inspect player state") from error

    async def read_level(self) -> int:
        """Return the level displayed by the current HentaiVerse page."""

        return _parse_level(await self._read_state())

    async def read_stamina(self) -> int:
        """Return the stamina displayed by the current HentaiVerse page."""

        return _parse_stamina(await self._read_state())

    async def inspect(self) -> PlayerSnapshot:
        """Return an immutable snapshot without changing character state."""

        state = await self._read_state()
        return PlayerSnapshot(
            level=_parse_level(state),
            stamina=_parse_stamina(state),
        )

    async def recover_stamina(
        self,
        expected_before: int | None = None,
    ) -> StaminaRecoveryReport:
        """Use one restorative and confirm recovery from an observed state change."""

        if expected_before is not None and (
            not isinstance(expected_before, int)
            or isinstance(expected_before, bool)
            or expected_before < 0
        ):
            raise ValueError("expected_before must be a non-negative integer or None")

        initial = await self._read_state()
        before = _parse_stamina(initial)
        if expected_before is not None and before != expected_before:
            raise PlayerStateChangedError(
                "Stamina changed before recovery; inspect and decide again"
            )
        if not initial.has_stamina_readout:
            raise PlayerPageError("Unable to find stamina recovery controls")

        preparation_deadline = Deadline.after(LOCAL_DOM_STATE_TIMEOUT_SECONDS)
        logger.debug("Checking USR RESTORATIVE availability for stamina recovery")
        stamina_readout = await query_page(
            self.page,
            "#stamina_readout",
            deadline=preparation_deadline,
        )
        if stamina_readout is None:
            raise PlayerPageError("Unable to find stamina recovery controls")
        try:
            await invoke_mutation(
                stamina_readout.mouse_move,
                owner=stamina_readout,
                operation="Stamina recovery control hover",
                deadline=preparation_deadline,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise PlayerPageError(
                "Unable to inspect stamina restorative availability"
            ) from error

        hovered = await self._read_state(deadline=preparation_deadline)
        if not hovered.restorative_available:
            return StaminaRecoveryReport(
                StaminaRecoveryOutcome.NOT_AVAILABLE,
                before,
                before,
            )
        restorative = await query_page(
            self.page,
            _RESTORATIVE_SELECTOR,
            deadline=preparation_deadline,
        )
        if restorative is None:
            raise PlayerPageError("Stamina restorative control disappeared")

        receipt_deadline = Deadline.after(SERVER_STATE_RECEIPT_TIMEOUT_SECONDS)
        try:
            await invoke_mutation(
                restorative.mouse_move,
                owner=restorative,
                operation="Stamina restorative hover",
                deadline=receipt_deadline,
            )
            await invoke_mutation(
                restorative.mouse_click,
                owner=restorative,
                operation="Stamina restorative submission",
                deadline=receipt_deadline,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise StaminaRecoveryError("Stamina recovery outcome is unknown") from error

        try:
            after_state = await wait_for_page_state(
                self.page,
                snapshot_expression=_PLAYER_STATE_SCRIPT,
                decode=_decode_player_state,
                accept=lambda state: state.error_text is not None
                or (state.stamina_text is not None and _parse_stamina(state) > before),
                deadline=receipt_deadline,
                description="stamina recovery result",
            )
        except PageStateTimeout as error:
            raise StaminaRecoveryError(
                "Unable to confirm stamina recovery: stamina did not increase"
            ) from error
        except PlayerPageError as error:
            raise StaminaRecoveryError("Unable to confirm stamina recovery") from error

        if after_state.error_text is not None:
            server_message = after_state.error_text.strip()
            if not server_message:
                server_message = "Stamina recovery was rejected"
            logger.warning(
                "USR RESTORATIVE was rejected: server_message=%r",
                server_message,
            )
            try:
                error_element = await query_page(
                    self.page,
                    _RECOVERY_ERROR_SELECTOR,
                    deadline=receipt_deadline,
                )
                if error_element is not None:
                    await invoke_mutation(
                        error_element.click,
                        owner=error_element,
                        operation="Stamina recovery rejection dismissal",
                        deadline=receipt_deadline,
                    )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                logger.debug(
                    "Unable to dismiss stamina recovery rejection: error_type=%s",
                    type(error).__name__,
                )
            return StaminaRecoveryReport(
                StaminaRecoveryOutcome.REJECTED,
                before,
                before,
                server_message,
            )

        after = _parse_stamina(after_state)
        logger.info("Used USR RESTORATIVE to recover stamina: %d -> %d", before, after)
        return StaminaRecoveryReport(
            StaminaRecoveryOutcome.RECOVERED,
            before,
            after,
        )
