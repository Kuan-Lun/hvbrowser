"""Typed inspection and repair operations for equipped gear."""

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .maintenance_navigation import (
    MaintenanceNavigationBlockedError,
    MaintenanceNavigator,
)
from .realm import Realm, RealmNavigator
from .runtime import setup_logger

logger = setup_logger(__name__)

_EQUIPMENT_COUNT_XPATH = "//label[@id='equipcount']"
_REPAIR_SUBMIT_XPATH = "//input[@id='equipsubmit']"
_REPAIR_TAB_XPATH = (
    "//div[contains(@class, 'armory_tab') "
    "and contains(normalize-space(.), 'Repair')]"
)
_EQUIPMENT_COUNT_PATTERN = re.compile(r"Selected [0-9]+ of ([0-9]+) matching")


class EquipmentRepairOutcome(StrEnum):
    """Known outcomes of an equipment-repair operation."""

    NO_REPAIR_NEEDED = "no-repair-needed"
    REPAIRED = "repaired"
    MATERIALS_UNAVAILABLE = "materials-unavailable"


@dataclass(frozen=True, slots=True)
class EquipmentRepairSnapshot:
    """The number of matching equipped items on one realm's Repair tab."""

    realm: Realm
    repair_count: int


@dataclass(frozen=True, slots=True)
class EquipmentRepairReport:
    """A confirmed repair result or a known inability to repair."""

    outcome: EquipmentRepairOutcome
    before: EquipmentRepairSnapshot
    after: EquipmentRepairSnapshot

    @property
    def ready(self) -> bool:
        return self.outcome in {
            EquipmentRepairOutcome.NO_REPAIR_NEEDED,
            EquipmentRepairOutcome.REPAIRED,
        }


class EquipmentRepairPageError(RuntimeError):
    """The Armory Repair page did not expose the expected structure."""


class EquipmentRepairSubmissionError(RuntimeError):
    """A repair was submitted but its outcome is unknown or unconfirmed."""


class EquipmentRepairStateChangedError(RuntimeError):
    """Repair state changed after the caller inspected it."""


class _EquipmentRepairDriver(Protocol):
    page: Any

    async def wait(
        self,
        fun: Any,
        ischangeurl: bool,
        sleeptime: int = 1,
    ) -> None: ...


class EquipmentRepairClient:
    """Inspect equipped gear and explicitly repair all eligible items."""

    def __init__(
        self,
        driver: _EquipmentRepairDriver,
        realm: RealmNavigator,
        maintenance: MaintenanceNavigator,
    ) -> None:
        self.driver = driver
        self.realm = realm
        self.maintenance = maintenance

    @property
    def page(self) -> Any:
        return self.driver.page

    async def inspect(self) -> EquipmentRepairSnapshot:
        """Navigate to Repair and return a read-only realm-scoped snapshot."""
        realm = await self.realm.current()
        await self._navigate(realm)
        snapshot, _ = await self._inspect_current(realm)
        return snapshot

    async def repair_all(
        self,
        expected_before: EquipmentRepairSnapshot | None = None,
    ) -> EquipmentRepairReport:
        """Repair all matching gear and confirm that none remains."""
        if expected_before is not None and not isinstance(
            expected_before, EquipmentRepairSnapshot
        ):
            raise TypeError(
                "expected_before must be an EquipmentRepairSnapshot or None"
            )

        realm = await self.realm.current()
        await self._navigate(realm)
        before, equipcount = await self._inspect_current(realm)
        if expected_before is not None and before != expected_before:
            raise EquipmentRepairStateChangedError(
                "Equipment repair state changed; inspect and decide again"
            )
        if before.repair_count == 0:
            return EquipmentRepairReport(
                EquipmentRepairOutcome.NO_REPAIR_NEEDED,
                before,
                before,
            )
        if equipcount is None:
            raise EquipmentRepairPageError(
                "Equipment count control is missing for non-zero repair state"
            )

        is_disabled, submit = await self._select_all_and_inspect_submit(equipcount)
        if is_disabled:
            logger.debug("Re-entering Repair tab to verify fresh server state")
            await self._navigate(realm)
            fresh, fresh_equipcount = await self._inspect_current(realm)
            if fresh != before:
                raise EquipmentRepairStateChangedError(
                    "Equipment repair state changed during fresh-state verification"
                )
            if fresh_equipcount is None:
                raise EquipmentRepairPageError(
                    "Equipment count control is missing during fresh-state verification"
                )

            is_disabled, submit = await self._select_all_and_inspect_submit(
                fresh_equipcount
            )
            if is_disabled:
                logger.info(
                    "Equipment repair materials are unavailable: repair_count=%d",
                    fresh.repair_count,
                )
                return EquipmentRepairReport(
                    EquipmentRepairOutcome.MATERIALS_UNAVAILABLE,
                    before,
                    fresh,
                )
            logger.warning(
                "Repair submit became enabled after re-entering Repair; "
                "the first disabled observation was stale"
            )

        try:
            await submit.mouse_click()
            await self.page.wait(2)
        except Exception as error:
            raise EquipmentRepairSubmissionError(
                "Equipment repair outcome is unknown"
            ) from error

        try:
            after, _equipcount_after = await self._inspect_current(realm)
        except EquipmentRepairPageError as error:
            raise EquipmentRepairSubmissionError(
                "Unable to confirm equipment repair"
            ) from error
        if after.repair_count != 0:
            raise EquipmentRepairSubmissionError(
                "Unable to confirm equipment repair: "
                f"{after.repair_count} matching items remain"
            )

        logger.info("Repaired equipment: %d -> 0", before.repair_count)
        return EquipmentRepairReport(
            EquipmentRepairOutcome.REPAIRED,
            before,
            after,
        )

    async def _navigate(self, realm: Realm) -> None:
        try:
            bazaar = await self.maintenance.select_bazaar(
                realm,
                navigate_first=False,
            )
        except MaintenanceNavigationBlockedError:
            raise
        except Exception as error:
            raise EquipmentRepairPageError("Unable to open Bazaar") from error

        try:
            armory_elements = await self.page.xpath(
                "//div[contains(text(), 'The Armory')]", timeout=5
            )
        except Exception as error:
            raise EquipmentRepairPageError(
                "Unable to find The Armory menu entry"
            ) from error
        if not armory_elements:
            raise EquipmentRepairPageError("Unable to find The Armory menu entry")

        try:
            await bazaar.mouse_move()
            await armory_elements[0].mouse_move()
            await self.driver.wait(
                armory_elements[0].mouse_click,
                ischangeurl=True,
            )
        except Exception as error:
            raise EquipmentRepairPageError("Unable to open The Armory") from error

        try:
            repair_elements = await self.page.xpath(
                _REPAIR_TAB_XPATH,
                timeout=5,
            )
        except Exception as error:
            raise EquipmentRepairPageError("Unable to find Repair tab") from error
        if not repair_elements:
            raise EquipmentRepairPageError("Unable to find Repair tab")

        try:
            await self.driver.wait(
                repair_elements[0].click,
                ischangeurl=True,
            )
        except Exception as error:
            raise EquipmentRepairPageError("Unable to open Repair tab") from error

    async def _inspect_current(
        self,
        realm: Realm,
    ) -> tuple[EquipmentRepairSnapshot, Any | None]:
        try:
            repair_page_markers = await self.page.xpath(
                _REPAIR_TAB_XPATH,
                timeout=5,
            )
        except Exception as error:
            raise EquipmentRepairPageError(
                "Unable to verify the Armory Repair page"
            ) from error
        if not repair_page_markers:
            raise EquipmentRepairPageError("Armory Repair page marker is missing")

        try:
            equipcount_elements = await self.page.xpath(
                _EQUIPMENT_COUNT_XPATH,
                timeout=5,
            )
        except Exception as error:
            raise EquipmentRepairPageError(
                "Unable to inspect equipment repair count"
            ) from error
        if not equipcount_elements:
            return EquipmentRepairSnapshot(realm, 0), None

        equipcount = equipcount_elements[0]
        text = getattr(equipcount, "text", None)
        if not isinstance(text, str):
            raise EquipmentRepairPageError("Equipment repair count text is missing")
        match = _EQUIPMENT_COUNT_PATTERN.search(text)
        if match is None:
            raise EquipmentRepairPageError(
                f"Unable to parse equipment repair count from: {text!r}"
            )
        return EquipmentRepairSnapshot(realm, int(match.group(1))), equipcount

    async def _select_all_and_inspect_submit(self, equipcount: Any) -> tuple[bool, Any]:
        logger.debug("Before select-all click: %r", getattr(equipcount, "text", None))
        try:
            await self.driver.wait(equipcount.mouse_click, ischangeurl=False)
        except Exception as error:
            raise EquipmentRepairPageError(
                "Unable to select equipment for repair"
            ) from error

        try:
            submit_elements = await self.page.xpath(
                _REPAIR_SUBMIT_XPATH,
                timeout=5,
            )
        except Exception as error:
            raise EquipmentRepairPageError(
                "Unable to inspect equipment repair submission"
            ) from error
        if not submit_elements:
            raise EquipmentRepairPageError("Equipment repair submit button is missing")

        try:
            is_disabled = await self.page.evaluate(
                "document.getElementById('equipsubmit').disabled"
            )
        except Exception as error:
            raise EquipmentRepairPageError(
                "Unable to inspect equipment repair submit state"
            ) from error
        if type(is_disabled) is not bool:
            raise EquipmentRepairPageError("Equipment repair submit state is invalid")

        if is_disabled and logger.isEnabledFor(logging.DEBUG):
            try:
                debug_state = await self.page.evaluate("""
                    JSON.stringify({
                        selected_count: selected_count,
                        selectable_count: selectable_count,
                        block_submit: block_submit,
                        materials: (() => {
                            const totals = {};
                            for (const el of document.querySelectorAll('input[name="eqids[]"]')) {
                                if (el.checked && eqitems[el.value]) {
                                    for (const m in eqitems[el.value].m) {
                                        totals[m] = (totals[m] || 0) + eqitems[el.value].m[m];
                                    }
                                }
                            }
                            return Object.entries(totals).map(([id, need]) => ({
                                id,
                                name: itemdata[id] ? itemdata[id].n : undefined,
                                need,
                                have: itemdata[id] ? itemdata[id].c : undefined,
                            }));
                        })(),
                    })
                    """)
            except Exception as error:
                logger.debug(
                    "Repair submit diagnostic probe failed: error_type=%s",
                    type(error).__name__,
                )
            else:
                logger.debug(
                    "Repair submit disabled at current observation: state=%s",
                    debug_state,
                )

        return is_disabled, submit_elements[0]
