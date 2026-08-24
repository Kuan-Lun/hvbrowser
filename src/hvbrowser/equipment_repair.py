"""Typed inspection and repair operations for equipped gear."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast
from urllib.parse import parse_qs, urlsplit

from .maintenance_navigation import (
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationObservation,
    observe_maintenance_navigation,
)
from .realm import Realm, RealmNavigator
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
from .urls import HENTAIVERSE_ISEKAI_ROOT_URL, HENTAIVERSE_ROOT_URL

logger = logging.getLogger(__name__)

_EQUIPMENT_STATE_SCRIPT = """
(() => {
    const equipForm = document.getElementById("equipform");
    const equipList = document.getElementById("equiplist");
    const selectableCount = (
        typeof selectable_count !== "number"
        || !Number.isInteger(selectable_count)
        || selectable_count < 0
    ) ? null : selectable_count;
    const selectedCount = (
        typeof selected_count !== "number"
        || !Number.isInteger(selected_count)
        || selected_count < 0
    ) ? null : selected_count;
    const submit = document.getElementById("equipsubmit");
    const error = document.querySelector("p.messagebox_error");
    return {
        repairSelected: Boolean(document.querySelector(
            '#armory_left a[href*="screen=repair"] .armory_tab.armory_cur'
        )),
        equippedSelected: Boolean(document.querySelector(
            '#filterbar a[href*="filter=equipped"] .cfbs'
        )),
        hasEquipForm: Boolean(equipForm),
        hasEquipList: Boolean(equipList),
        hasEquipCount: Boolean(document.getElementById("equipcount")),
        hasSubmit: Boolean(submit),
        submitDisabled: submit ? Boolean(submit.disabled) : null,
        selectableCount,
        selectedCount,
        errorText: error ? error.textContent : null,
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


@dataclass(frozen=True, slots=True)
class _EquipmentPageState:
    repair_selected: bool
    equipped_selected: bool
    has_equip_form: bool
    has_equip_list: bool
    has_equip_count: bool
    has_submit: bool
    submit_disabled: bool | None
    selectable_count: int | None
    selected_count: int | None
    empty: bool
    row_count: int | None
    error_text: str | None


def _decode_equipment_state(raw: object) -> _EquipmentPageState:
    if not isinstance(raw, dict):
        raise EquipmentRepairPageError("Equipment repair state is invalid")
    payload = cast(dict[object, object], raw)
    repair_selected = payload.get("repairSelected")
    equipped_selected = payload.get("equippedSelected")
    has_equip_form = payload.get("hasEquipForm")
    has_equip_list = payload.get("hasEquipList")
    has_equip_count = payload.get("hasEquipCount")
    has_submit = payload.get("hasSubmit")
    submit_disabled = payload.get("submitDisabled")
    selectable_count = payload.get("selectableCount")
    selected_count = payload.get("selectedCount")
    empty = payload.get("empty")
    row_count = payload.get("rowCount")
    error_text = payload.get("errorText")
    if (
        type(repair_selected) is not bool
        or type(equipped_selected) is not bool
        or type(has_equip_form) is not bool
        or type(has_equip_list) is not bool
        or type(has_equip_count) is not bool
        or type(has_submit) is not bool
        or (submit_disabled is not None and type(submit_disabled) is not bool)
        or (
            selectable_count is not None
            and (type(selectable_count) is not int or selectable_count < 0)
        )
        or (
            selected_count is not None
            and (type(selected_count) is not int or selected_count < 0)
        )
        or type(empty) is not bool
        or (row_count is not None and (type(row_count) is not int or row_count < 0))
        or (error_text is not None and not isinstance(error_text, str))
    ):
        raise EquipmentRepairPageError("Equipment repair state is invalid")
    return _EquipmentPageState(
        repair_selected,
        equipped_selected,
        has_equip_form,
        has_equip_list,
        has_equip_count,
        has_submit,
        submit_disabled,
        selectable_count,
        selected_count,
        empty,
        row_count,
        error_text,
    )


class EquipmentRepairClient:
    """Inspect equipped gear and explicitly repair all eligible items."""

    def __init__(
        self,
        driver: _EquipmentRepairDriver,
        realm: RealmNavigator,
    ) -> None:
        self.driver = driver
        self.realm = realm

    @property
    def page(self) -> Any:
        return self.driver.page

    async def inspect(self) -> EquipmentRepairSnapshot:
        """Navigate to Repair and return a read-only realm-scoped snapshot."""
        realm = await self.realm.current()
        state = await self._navigate(realm)
        snapshot, _ = await self._inspect_current(realm, state=state)
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
        state = await self._navigate(realm)
        before, equipcount = await self._inspect_current(realm, state=state)
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

        selection_deadline = Deadline.after(LOCAL_DOM_STATE_TIMEOUT_SECONDS)
        is_disabled, submit = await self._select_all_and_inspect_submit(
            equipcount,
            deadline=selection_deadline,
        )
        if is_disabled:
            logger.debug("Re-entering Repair tab to verify fresh server state")
            fresh_state = await self._navigate(realm)
            fresh, fresh_equipcount = await self._inspect_current(
                realm,
                state=fresh_state,
            )
            if fresh != before:
                raise EquipmentRepairStateChangedError(
                    "Equipment repair state changed during fresh-state verification"
                )
            if fresh_equipcount is None:
                raise EquipmentRepairPageError(
                    "Equipment count control is missing during fresh-state verification"
                )

            selection_deadline = Deadline.after(LOCAL_DOM_STATE_TIMEOUT_SECONDS)
            is_disabled, submit = await self._select_all_and_inspect_submit(
                fresh_equipcount,
                deadline=selection_deadline,
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

        submission_deadline = Deadline.after(SERVER_STATE_RECEIPT_TIMEOUT_SECONDS)
        try:
            await invoke_mutation(
                submit.mouse_click,
                owner=submit,
                operation="Equipment repair submission",
                deadline=submission_deadline,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairSubmissionError(
                "Equipment repair outcome is unknown"
            ) from error

        try:
            after_state = await wait_for_page_state(
                self.page,
                snapshot_expression=_EQUIPMENT_STATE_SCRIPT,
                decode=_decode_equipment_state,
                accept=lambda current: current.error_text is not None
                or self._repair_count(current) == 0,
                deadline=submission_deadline,
                description="equipment repair completion",
            )
            after, _equipcount_after = await self._inspect_current(
                realm,
                state=after_state,
                deadline=submission_deadline,
            )
        except (PageStateTimeout, EquipmentRepairPageError) as error:
            raise EquipmentRepairSubmissionError(
                "Unable to confirm equipment repair"
            ) from error
        if after_state.error_text is not None:
            message = after_state.error_text.strip() or "repair rejected"
            raise EquipmentRepairSubmissionError(
                f"Equipment repair was rejected: {message}"
            )
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

    async def _navigate(self, realm: Realm) -> _EquipmentPageState:
        return await self._open_repair_directly(realm)

    async def _open_repair_directly(self, realm: Realm) -> _EquipmentPageState:
        await self._ensure_navigation_is_safe(
            realm,
            "before direct Armory navigation",
        )
        try:
            await self.driver.get(_ARMORY_URLS[realm])
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            try:
                await self._ensure_navigation_is_safe(
                    realm,
                    "after direct Armory navigation",
                )
            except MaintenanceNavigationBlockedError as blocked:
                raise blocked from error
            except _EquipmentRepairNavigationSafetyError as safety_error:
                raise safety_error from error
            raise EquipmentRepairPageError(
                "Unable to open The Armory through its direct URL"
            ) from error

        return await self._verify_repair_destination(realm)

    async def _ensure_navigation_is_safe(
        self,
        realm: Realm,
        context: str,
    ) -> MaintenanceNavigationObservation:
        try:
            observation = await observe_maintenance_navigation(self.page)
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise _EquipmentRepairNavigationSafetyError(
                f"Unable to verify battle state {context}"
            ) from error
        if observation.realm is not realm:
            raise _EquipmentRepairNavigationSafetyError(
                "Equipment repair navigation is on an untrusted or wrong realm "
                f"{context}"
            )
        expected_path = "/isekai/" if realm is Realm.ISEKAI else "/"
        if urlsplit(observation.url).path != expected_path:
            raise _EquipmentRepairNavigationSafetyError(
                f"Equipment repair navigation is on an unexpected path {context}"
            )
        if observation.blocker is not None:
            raise MaintenanceNavigationBlockedError(observation.blocker)
        return observation

    async def _verify_repair_destination(
        self,
        realm: Realm,
        *,
        deadline: Deadline | None = None,
    ) -> _EquipmentPageState:
        observation = await self._ensure_navigation_is_safe(
            realm,
            "after opening Repair",
        )
        parsed_url = urlsplit(observation.url)
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
        state = await self._read_state(deadline=deadline)
        return state

    async def _inspect_current(
        self,
        realm: Realm,
        *,
        state: _EquipmentPageState | None = None,
        deadline: Deadline | None = None,
    ) -> tuple[EquipmentRepairSnapshot, Any | None]:
        current = (
            await self._verify_repair_destination(realm, deadline=deadline)
            if state is None
            else state
        )
        self._validate_repair_page_state(current)
        repair_count = self._repair_count(current)
        if current.has_equip_count:
            equipcount = await query_page(
                self.page,
                "#equipform #equipcount",
                deadline=deadline,
            )
            if equipcount is None:
                raise EquipmentRepairPageError(
                    "Equipment count control disappeared during inspection"
                )
            if repair_count is None:
                raise EquipmentRepairPageError(
                    "Equipment repair count state is invalid"
                )
            return EquipmentRepairSnapshot(realm, repair_count), equipcount
        if current.empty and current.row_count == 0 and repair_count in {None, 0}:
            return EquipmentRepairSnapshot(realm, 0), None
        raise EquipmentRepairPageError(
            "Equipment repair count is missing without an empty equipment list"
        )

    async def _read_state(
        self,
        *,
        deadline: Deadline | None = None,
    ) -> _EquipmentPageState:
        try:
            state = _decode_equipment_state(
                await evaluate_page(
                    self.page,
                    _EQUIPMENT_STATE_SCRIPT,
                    deadline=deadline,
                )
            )
        except Exception as error:
            if is_browser_generation_error(error) or isinstance(
                error, EquipmentRepairPageError
            ):
                raise
            raise EquipmentRepairPageError(
                "Unable to inspect equipment repair state"
            ) from error
        self._validate_repair_page_state(state)
        return state

    @staticmethod
    def _validate_repair_page_state(state: _EquipmentPageState) -> None:
        if not state.has_equip_form or not state.has_equip_list:
            raise EquipmentRepairPageError("Equipment repair form or list is missing")
        if not state.repair_selected or not state.equipped_selected:
            raise EquipmentRepairPageError(
                "Armory Repair selected-tab marker or Equipped filter marker is missing"
            )

    @staticmethod
    def _repair_count(state: _EquipmentPageState) -> int | None:
        return state.selectable_count

    async def _select_all_and_inspect_submit(
        self,
        equipcount: Any,
        *,
        deadline: Deadline,
    ) -> tuple[bool, Any]:
        logger.debug("Before select-all click: %r", getattr(equipcount, "text", None))
        try:
            await invoke_mutation(
                equipcount.mouse_click,
                owner=equipcount,
                operation="Equipment repair select-all",
                deadline=deadline,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise EquipmentRepairPageError(
                "Unable to select equipment for repair"
            ) from error
        try:
            state = await wait_for_page_state(
                self.page,
                snapshot_expression=_EQUIPMENT_STATE_SCRIPT,
                decode=_decode_equipment_state,
                accept=lambda current: current.has_submit
                and current.repair_selected
                and current.equipped_selected
                and current.has_equip_form
                and current.has_equip_list
                and current.submit_disabled is not None
                and current.selectable_count is not None
                and current.selected_count == current.selectable_count,
                deadline=deadline,
                description="equipment select-all state",
            )
        except (PageStateTimeout, EquipmentRepairPageError) as error:
            raise EquipmentRepairPageError(
                "Unable to confirm equipment selection for repair"
            ) from error
        submit = await query_page(
            self.page,
            "#equipform #equipsubmit",
            deadline=deadline,
        )
        if submit is None:
            raise EquipmentRepairPageError("Equipment repair submit button disappeared")
        assert state.submit_disabled is not None
        if state.submit_disabled:
            logger.debug(
                "Repair submit disabled after selecting %d items",
                state.selected_count,
            )
        return state.submit_disabled, submit
