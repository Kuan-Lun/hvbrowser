"""Typed inspection and repair operations for equipped gear."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from .maintenance_navigation import (
    MaintenanceNavigationBlockedError,
    MaintenanceNavigator,
    classify_maintenance_navigation_blocker,
)
from .realm import Realm, RealmNavigator, realm_from_url
from .runtime import (
    is_browser_generation_error,
    setup_logger,
    wait_for_zendriver,
)
from .urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL

logger = setup_logger(__name__)

_READ_TIMEOUT_SECONDS = 8.0
_MUTATION_TIMEOUT_SECONDS = 15.0
_SELECTOR_INNER_TIMEOUT_SECONDS = 5.0
_SELECTOR_OUTER_TIMEOUT_SECONDS = 7.0

_EQUIPMENT_COUNT_XPATH = "//*[@id='equipform']//label[@id='equipcount']"
_REPAIR_SUBMIT_XPATH = "//*[@id='equipform']//input[@id='equipsubmit']"
_ARMORY_MENU_XPATH = (
    "//*[@id='child_Bazaar']"
    "//*[@onclick and contains(@onclick, 's=Bazaar') "
    "and contains(@onclick, 'ss=am')]"
    " | //*[@id='child_Bazaar']//a[contains(@href, 's=Bazaar') "
    "and contains(@href, 'ss=am')]"
)
_REPAIR_TAB_XPATH = (
    "//*[@id='armory_left']/a[contains(@href, 'screen=repair') "
    "and ./*[contains(concat(' ', normalize-space(@class), ' '), "
    "' armory_tab ')]]"
)
_REPAIR_SELECTED_PAGE_XPATH = (
    "//*[@id='armory_outer']["
    ".//*[@id='armory_left']/a[contains(@href, 'screen=repair')]"
    "/*[contains(concat(' ', normalize-space(@class), ' '), ' armory_tab ') "
    "and contains(concat(' ', normalize-space(@class), ' '), ' armory_cur ')] "
    "and .//*[@id='filterbar']/a[contains(@href, 'filter=equipped')]"
    "/*[contains(concat(' ', normalize-space(@class), ' '), ' cfbs ')]"
    "]"
)
_EQUIPMENT_STATE_SCRIPT = """
(() => {
    const equipForm = document.getElementById("equipform");
    const equipList = document.getElementById("equiplist");
    const selectableCount = (
        typeof selectable_count !== "number"
        || !Number.isInteger(selectable_count)
        || selectable_count < 0
    ) ? null : selectable_count;
    return {
        hasEquipForm: Boolean(equipForm),
        hasEquipList: Boolean(equipList),
        selectableCount,
        empty: Boolean(
            equipList
            && (
                equipList.querySelector(".eqempty")
                || equipList.querySelector("tr.eqselall > th > p")
            )
        ),
        rowCount: equipList
            ? equipList.querySelectorAll('tr[onmouseover*="hover_equip"]').length
            : null,
    };
})()
"""
_ARMORY_MENU_VISIBILITY_ATTEMPTS = 20
_ARMORY_MENU_VISIBILITY_INTERVAL_SECONDS = 0.1
_ARMORY_URLS = {
    Realm.PERSISTENT: (
        f"{HENTAIVERSE_ROOT_URL}/" "?s=Bazaar&ss=am&screen=repair&filter=equipped"
    ),
    Realm.ISEKAI: (
        f"{HENTAIVERSE_ISEKAI_ROOT_URL}" "?s=Bazaar&ss=am&screen=repair&filter=equipped"
    ),
}


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


class _EquipmentRepairNavigationSafetyError(EquipmentRepairPageError):
    """The current battle state, origin, or realm could not be trusted."""


class EquipmentRepairSubmissionError(RuntimeError):
    """A repair was submitted but its outcome is unknown or unconfirmed."""


class EquipmentRepairStateChangedError(RuntimeError):
    """Repair state changed after the caller inspected it."""


class _EquipmentRepairDriver(Protocol):
    page: Any

    async def get(self, url: str) -> None: ...

    async def wait(
        self,
        fun: Any,
        ischangeurl: bool,
        sleeptime: int = 1,
        *,
        owner: Any,
        operation_timeout: float,
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
            await wait_for_zendriver(
                submit.mouse_click(),
                timeout=_MUTATION_TIMEOUT_SECONDS,
                owner=submit,
            )
            await wait_for_zendriver(
                self.page.wait(2),
                timeout=_READ_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
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
            await self._open_repair_from_menu(realm)
            return
        except MaintenanceNavigationBlockedError:
            raise
        except _EquipmentRepairNavigationSafetyError:
            raise
        except EquipmentRepairPageError as error:
            logger.warning(
                "Armory menu navigation did not open Repair; retrying through "
                "the realm-scoped direct URL: realm=%s error_type=%s",
                realm.value,
                type(error).__name__,
            )

        await self._open_repair_directly(realm)

    async def _open_repair_from_menu(self, realm: Realm) -> None:
        await self._open_armory_from_menu(realm)
        repair = await self._find_repair_tab()

        try:
            await self.driver.wait(
                repair.click,
                ischangeurl=True,
                owner=repair,
                operation_timeout=_MUTATION_TIMEOUT_SECONDS,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            try:
                await self._ensure_navigation_is_safe("after opening Repair")
            except MaintenanceNavigationBlockedError as blocked:
                raise blocked from error
            except _EquipmentRepairNavigationSafetyError as safety_error:
                raise safety_error from error
            raise EquipmentRepairPageError("Unable to open Repair tab") from error

        await self._verify_repair_destination(realm)

    async def _open_armory_from_menu(self, realm: Realm) -> None:
        try:
            bazaar = await self.maintenance.select_bazaar(
                realm,
                navigate_first=False,
            )
        except MaintenanceNavigationBlockedError:
            raise
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError("Unable to open Bazaar") from error

        try:
            armory_elements = await wait_for_zendriver(
                self.page.xpath(
                    _ARMORY_MENU_XPATH,
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError(
                "Unable to find The Armory menu entry"
            ) from error
        if not armory_elements:
            raise EquipmentRepairPageError("Unable to find The Armory menu entry")

        armory = armory_elements[0]
        try:
            await wait_for_zendriver(
                bazaar.mouse_move(),
                timeout=_MUTATION_TIMEOUT_SECONDS,
                owner=bazaar,
            )
            await self._wait_for_armory_menu(armory)
            await wait_for_zendriver(
                armory.mouse_move(),
                timeout=_MUTATION_TIMEOUT_SECONDS,
                owner=armory,
            )
            await self.driver.wait(
                armory.mouse_click,
                ischangeurl=True,
                owner=armory,
                operation_timeout=_MUTATION_TIMEOUT_SECONDS,
            )
        except EquipmentRepairPageError:
            raise
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError("Unable to open The Armory") from error

    async def _wait_for_armory_menu(self, armory: Any) -> None:
        last_error: Exception | None = None
        for attempt in range(_ARMORY_MENU_VISIBILITY_ATTEMPTS):
            try:
                position = await wait_for_zendriver(
                    armory.get_position(),
                    timeout=_READ_TIMEOUT_SECONDS,
                    owner=armory,
                )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                last_error = error
            else:
                if position is not None:
                    return

            if attempt + 1 < _ARMORY_MENU_VISIBILITY_ATTEMPTS:
                try:
                    await wait_for_zendriver(
                        self.page.wait(_ARMORY_MENU_VISIBILITY_INTERVAL_SECONDS),
                        timeout=_READ_TIMEOUT_SECONDS,
                        owner=self.page,
                    )
                except Exception as error:
                    if is_browser_generation_error(error):
                        raise
                    raise EquipmentRepairPageError(
                        "Unable to wait for The Armory menu entry"
                    ) from error

        page_error = EquipmentRepairPageError(
            "The Armory menu entry did not become visible"
        )
        if last_error is None:
            raise page_error
        raise page_error from last_error

    async def _open_repair_directly(self, realm: Realm) -> None:
        await self._ensure_navigation_is_safe("before direct Armory navigation")
        try:
            await self.driver.get(_ARMORY_URLS[realm])
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            try:
                await self._ensure_navigation_is_safe("after direct Armory navigation")
            except MaintenanceNavigationBlockedError as blocked:
                raise blocked from error
            except _EquipmentRepairNavigationSafetyError as safety_error:
                raise safety_error from error
            raise EquipmentRepairPageError(
                "Unable to open The Armory through its direct URL"
            ) from error

        await self._verify_repair_destination(realm)

    async def _ensure_navigation_is_safe(self, context: str) -> None:
        try:
            blocker = await classify_maintenance_navigation_blocker(self.page)
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise _EquipmentRepairNavigationSafetyError(
                f"Unable to verify battle state {context}"
            ) from error
        if blocker is not None:
            raise MaintenanceNavigationBlockedError(blocker)

    async def _verify_repair_destination(self, realm: Realm) -> None:
        await self._ensure_navigation_is_safe("after opening Repair")
        try:
            current_url = await wait_for_zendriver(
                self.page.evaluate("window.location.href"),
                timeout=_READ_TIMEOUT_SECONDS,
                owner=self.page,
            )
            landed_realm = realm_from_url(current_url)
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise _EquipmentRepairNavigationSafetyError(
                "Unable to verify the Repair URL"
            ) from error
        if landed_realm is not realm:
            raise _EquipmentRepairNavigationSafetyError(
                "Repair navigation landed in the wrong realm"
            )
        if not isinstance(current_url, str):
            raise _EquipmentRepairNavigationSafetyError("Repair URL is invalid")
        parsed_url = urlsplit(current_url)
        expected_path = "/isekai/" if realm is Realm.ISEKAI else "/"
        if parsed_url.path != expected_path:
            raise _EquipmentRepairNavigationSafetyError(
                "Repair navigation landed on an unexpected path"
            )
        query = parse_qs(parsed_url.query, keep_blank_values=True)
        expected_query = {
            "s": ["Bazaar"],
            "ss": ["am"],
            "screen": ["repair"],
        }
        if any(query.get(key) != value for key, value in expected_query.items()):
            raise EquipmentRepairPageError(
                "Repair navigation did not land on the Repair route"
            )
        filter_values = query.get("filter")
        if filter_values is not None and filter_values != ["equipped"]:
            raise EquipmentRepairPageError(
                "Repair navigation did not land on the Equipped filter"
            )
        await self._verify_repair_selected()

    async def _find_repair_tab(self) -> Any:
        try:
            repair_elements = await wait_for_zendriver(
                self.page.xpath(
                    _REPAIR_TAB_XPATH,
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError("Unable to find Repair tab") from error
        if not repair_elements:
            raise EquipmentRepairPageError("Unable to find Repair tab")
        return repair_elements[0]

    async def _verify_repair_selected(self) -> None:
        try:
            selected_tabs = await wait_for_zendriver(
                self.page.xpath(
                    _REPAIR_SELECTED_PAGE_XPATH,
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError(
                "Unable to verify the Armory Repair page"
            ) from error
        if not selected_tabs:
            raise EquipmentRepairPageError(
                "Armory Repair selected-tab marker or Equipped filter marker is missing"
            )

    async def _inspect_current(
        self,
        realm: Realm,
    ) -> tuple[EquipmentRepairSnapshot, Any | None]:
        await self._verify_repair_destination(realm)

        try:
            equipcount_elements = await wait_for_zendriver(
                self.page.xpath(
                    _EQUIPMENT_COUNT_XPATH,
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError(
                "Unable to inspect equipment repair count"
            ) from error

        try:
            raw_state = await wait_for_zendriver(
                self.page.evaluate(_EQUIPMENT_STATE_SCRIPT),
                timeout=_READ_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError(
                "Unable to inspect equipment repair count"
            ) from error
        if not isinstance(raw_state, dict):
            raise EquipmentRepairPageError("Equipment repair count state is invalid")
        repair_count = raw_state.get("selectableCount")
        empty = raw_state.get("empty")
        row_count = raw_state.get("rowCount")
        has_equip_form = raw_state.get("hasEquipForm")
        has_equip_list = raw_state.get("hasEquipList")
        if (
            (repair_count is not None and type(repair_count) is not int)
            or type(empty) is not bool
            or (row_count is not None and (type(row_count) is not int or row_count < 0))
            or has_equip_form is not True
            or has_equip_list is not True
        ):
            raise EquipmentRepairPageError("Equipment repair count state is invalid")
        if equipcount_elements:
            if repair_count is None or repair_count < 0:
                raise EquipmentRepairPageError(
                    "Equipment repair count state is invalid"
                )
            return EquipmentRepairSnapshot(realm, repair_count), equipcount_elements[0]
        if empty and row_count == 0 and repair_count in {None, 0}:
            return EquipmentRepairSnapshot(realm, 0), None
        raise EquipmentRepairPageError(
            "Equipment repair count is missing without an empty equipment list"
        )

    async def _select_all_and_inspect_submit(self, equipcount: Any) -> tuple[bool, Any]:
        logger.debug("Before select-all click: %r", getattr(equipcount, "text", None))
        try:
            await self.driver.wait(
                equipcount.mouse_click,
                ischangeurl=False,
                owner=equipcount,
                operation_timeout=_MUTATION_TIMEOUT_SECONDS,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError(
                "Unable to select equipment for repair"
            ) from error

        try:
            submit_elements = await wait_for_zendriver(
                self.page.xpath(
                    _REPAIR_SUBMIT_XPATH,
                    timeout=_SELECTOR_INNER_TIMEOUT_SECONDS,
                ),
                timeout=_SELECTOR_OUTER_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError(
                "Unable to inspect equipment repair submission"
            ) from error
        if not submit_elements:
            raise EquipmentRepairPageError("Equipment repair submit button is missing")

        try:
            is_disabled = await wait_for_zendriver(
                self.page.evaluate("document.getElementById('equipsubmit').disabled"),
                timeout=_READ_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError(
                "Unable to inspect equipment repair submit state"
            ) from error
        if type(is_disabled) is not bool:
            raise EquipmentRepairPageError("Equipment repair submit state is invalid")

        if is_disabled and logger.isEnabledFor(logging.DEBUG):
            try:
                debug_state = await wait_for_zendriver(
                    self.page.evaluate("""
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
                        """),
                    timeout=_READ_TIMEOUT_SECONDS,
                    owner=self.page,
                )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
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
