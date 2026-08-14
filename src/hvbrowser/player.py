"""Typed character-state inspection and stamina recovery operations."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .runtime import setup_logger

logger = setup_logger(__name__)

_LEVEL_PATTERN = re.compile(r"(?:^|\s)Lv\.\s*([0-9]+)\s*$")
_STAMINA_PATTERN = re.compile(r"Stamina:\s*([0-9]+)")
_RESTORATIVE_XPATH = (
    "//img[@onclick=\"document.getElementById('recoverform').submit()\"]"
)
_RECOVERY_ERROR_XPATH = "//p[contains(@class, 'messagebox_error')]"


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


class PlayerPageError(RuntimeError):
    """The page did not expose readable character state or controls."""


class StaminaRecoveryError(RuntimeError):
    """A recovery submission was made but its outcome is unknown."""


class PlayerStateChangedError(RuntimeError):
    """The visible stamina changed after the caller inspected it."""


class _PlayerDriver(Protocol):
    page: Any


class PlayerClient:
    """Inspect the current character and explicitly recover stamina."""

    def __init__(self, driver: _PlayerDriver) -> None:
        self.driver = driver

    @property
    def page(self) -> Any:
        return self.driver.page

    async def read_level(self) -> int:
        """Return the level displayed by the current HentaiVerse page."""
        try:
            level_readout = await self.page.select("#level_readout", timeout=5)
        except Exception as error:
            raise PlayerPageError("Unable to find level readout") from error
        if level_readout is None:
            raise PlayerPageError("Unable to find level readout")

        text = getattr(level_readout, "text", None)
        if not isinstance(text, str):
            raise PlayerPageError("Level readout text is unavailable")
        match = _LEVEL_PATTERN.search(text)
        if match is None:
            raise PlayerPageError(f"Unable to parse level from: {text!r}")
        return int(match.group(1))

    async def read_stamina(self) -> int:
        """Return the stamina displayed by the current HentaiVerse page."""
        try:
            stamina_elements = await self.page.xpath(
                "//div[contains(text(), 'Stamina:')]", timeout=5
            )
        except Exception as error:
            raise PlayerPageError("Unable to find stamina readout") from error
        if not stamina_elements:
            raise PlayerPageError("Unable to find stamina readout")

        text = getattr(stamina_elements[0], "text", None)
        if not isinstance(text, str):
            raise PlayerPageError("Stamina readout text is unavailable")
        match = _STAMINA_PATTERN.search(text)
        if match is None:
            raise PlayerPageError(f"Unable to parse stamina from: {text!r}")
        return int(match.group(1))

    async def inspect(self) -> PlayerSnapshot:
        """Return an immutable snapshot without changing character state."""
        return PlayerSnapshot(
            level=await self.read_level(),
            stamina=await self.read_stamina(),
        )

    async def recover_stamina(
        self,
        expected_before: int | None = None,
    ) -> StaminaRecoveryReport:
        """Use one restorative and confirm recovery from the stamina readout."""
        if expected_before is not None and (
            not isinstance(expected_before, int)
            or isinstance(expected_before, bool)
            or expected_before < 0
        ):
            raise ValueError("expected_before must be a non-negative integer or None")

        before = await self.read_stamina()
        if expected_before is not None and before != expected_before:
            raise PlayerStateChangedError(
                "Stamina changed before recovery; inspect and decide again"
            )

        logger.debug("Checking USR RESTORATIVE availability for stamina recovery")
        try:
            stamina_readout = await self.page.select("#stamina_readout", timeout=5)
        except Exception as error:
            raise PlayerPageError("Unable to find stamina recovery controls") from error
        if stamina_readout is None:
            raise PlayerPageError("Unable to find stamina recovery controls")

        try:
            await stamina_readout.mouse_move()
            restorative_elements = await self.page.xpath(
                _RESTORATIVE_XPATH,
                timeout=5,
            )
        except Exception as error:
            raise PlayerPageError(
                "Unable to inspect stamina restorative availability"
            ) from error
        if not restorative_elements:
            return StaminaRecoveryReport(
                StaminaRecoveryOutcome.NOT_AVAILABLE,
                before,
                before,
            )

        restorative = restorative_elements[0]
        try:
            await restorative.mouse_move()
            await restorative.mouse_click()
            await self.page.wait(1)
        except Exception as error:
            raise StaminaRecoveryError("Stamina recovery outcome is unknown") from error

        try:
            error_elements = await self.page.xpath(
                _RECOVERY_ERROR_XPATH,
                timeout=2,
            )
        except Exception as error:
            raise StaminaRecoveryError(
                "Unable to determine whether stamina recovery was rejected"
            ) from error
        if error_elements:
            server_message = getattr(error_elements[0], "text", None)
            if not isinstance(server_message, str):
                server_message = "Stamina recovery was rejected"
            logger.warning(
                "USR RESTORATIVE was rejected: server_message=%r",
                server_message,
            )
            try:
                await error_elements[0].click()
            except Exception as error:
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

        try:
            after = await self.read_stamina()
        except PlayerPageError as error:
            raise StaminaRecoveryError("Unable to confirm stamina recovery") from error
        if after <= before:
            raise StaminaRecoveryError(
                "Unable to confirm stamina recovery: stamina did not increase"
            )

        logger.info("Used USR RESTORATIVE to recover stamina: %d -> %d", before, after)
        return StaminaRecoveryReport(
            StaminaRecoveryOutcome.RECOVERED,
            before,
            after,
        )
