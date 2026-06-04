"""Repairs surface for the rtl_433 integration.

Scope is deliberately tight (per the plan, repairs cover only *genuinely
actionable* problems, not speculative ones). Two hub-scoped issues live here:

- **server unreachable** — raised when a hub's WebSocket coordinator has been
  unable to stay connected, cleared automatically the moment it comes back.
- **sample rate low for band** — a dismissible advisory raised when a *single*
  high-band frequency (>= 800 MHz) is left at the bare default sample rate; many
  devices still decode fine there, so it is advisory only and edge-triggered so a
  dismissed card is not re-raised while the condition persists.

The coordinator package is intentionally left untouched (it owns no HA-repairs
knowledge). Instead :func:`async_track_hub_reachability` polls the coordinator's
``connected`` flag on an interval, and :func:`async_track_sample_rate` re-reads
``coordinator.meta`` on each ``signal_hub_update``. ``__init__.py`` wires both in
during hub setup and registers their unsubscribes via ``entry.async_on_unload``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval

from .config_flow import CONF_SECURE, async_rebind_hub
from .const import (
    CONF_HOST,
    CONF_PATH,
    CONF_PORT,
    CONF_RADIO_ID,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DOMAIN,
    LOGGER,
    signal_hub_update,
)
from .coordinator import CannotConnect, Rtl433Coordinator

# How often reachability is evaluated. Aligned to be responsive without being
# chatty; the issue only flips on a sustained state change.
_REACHABILITY_INTERVAL = timedelta(seconds=30)

# How long the socket may stay down before the issue is raised. A brief blip
# (reconnect backoff) should not raise an issue, so we require the disconnect to
# persist across at least one full grace window before surfacing it.
_UNREACHABLE_GRACE = timedelta(seconds=90)

# translation_key / issue_id prefix for the unreachable-server issue.
ISSUE_UNREACHABLE = "server_unreachable"

# translation_key for the one-shot "motion moved to binary_sensor" advisory.
# A single, integration-wide issue id (the move affects every motion device the
# same way), so it is never duplicated across hubs or restarts.
ISSUE_MOTION_MOVED = "motion_moved_to_binary_sensor"

# translation_key / issue_id prefix for the "sample rate looks low for this
# band" advisory.
ISSUE_SAMPLE_RATE_LOW = "sample_rate_low_for_band"

# Conservative band heuristic for the sample-rate advisory. rtl_433 does not
# auto-widen the sample rate when retuned via ``/cmd`` (unlike some CLI startup
# paths), so a receiver moved into the upper ISM bands can be left at the bare
# 250 kHz default. We only flag the clear-cut case — a *single* frequency at or
# above 800 MHz captured at or below the default rate — and recommend a wider
# rate; many devices still decode fine at 250k, so this is advisory only.
_HIGH_BAND_MIN_HZ = 800_000_000
_LOW_SAMPLE_RATE_MAX_HZ = 250_000
# The rate we suggest in the advisory text (the common RTL-SDR 1.024 MS/s).
_SUGGESTED_SAMPLE_RATE_HZ = 1_024_000


def _unreachable_issue_id(entry: ConfigEntry) -> str:
    """Return the per-hub issue id for the unreachable-server repair."""
    return f"{ISSUE_UNREACHABLE}_{entry.entry_id}"


def _sample_rate_issue_id(entry: ConfigEntry) -> str:
    """Return the per-hub issue id for the low-sample-rate advisory."""
    return f"{ISSUE_SAMPLE_RATE_LOW}_{entry.entry_id}"


def _sample_rate_looks_low(meta: dict[str, Any]) -> bool:
    """Return whether meta shows a single high-band frequency at a low rate.

    Guards: both values must be present and numeric, and a *hopping* receiver
    (more than one configured frequency) is never flagged — it spans bands and a
    single-rate recommendation would be meaningless.
    """
    hopping = len(meta.get("frequencies", []) or []) > 1
    if hopping:
        return False
    freq = meta.get("center_frequency")
    rate = meta.get("samp_rate")
    if not isinstance(freq, (int, float)) or isinstance(freq, bool):
        return False
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return False
    return freq >= _HIGH_BAND_MIN_HZ and rate <= _LOW_SAMPLE_RATE_MAX_HZ


@callback
def async_raise_motion_moved(hass: HomeAssistant) -> None:
    """Raise the (non-fixable, warning) "motion moved to binary_sensor" advisory.

    The issue id is the stable ``ISSUE_MOTION_MOVED`` so re-raising it (on a
    later startup, or for a second hub) is a no-op rather than a duplicate card.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_MOTION_MOVED,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_MOTION_MOVED,
    )


@callback
def async_raise_sample_rate_low(
    hass: HomeAssistant, entry: ConfigEntry, meta: dict[str, Any]
) -> None:
    """Raise the dismissible low-sample-rate advisory for a hub.

    Placeholders carry the concrete frequency (MHz) and current/suggested sample
    rates so the card is actionable. ``is_fixable`` so the user can dismiss it
    with a confirm dialog; the edge-triggered tracker will not re-raise it while
    the condition persists.
    """
    freq_mhz = f"{float(meta.get('center_frequency', 0)) / 1_000_000:g}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        _sample_rate_issue_id(entry),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_SAMPLE_RATE_LOW,
        translation_placeholders={
            "title": entry.title,
            "frequency": freq_mhz,
            "sample_rate": str(int(meta.get("samp_rate", 0))),
            "suggested": str(_SUGGESTED_SAMPLE_RATE_HZ),
        },
    )


@callback
def async_clear_sample_rate_low(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the low-sample-rate advisory for a hub (no-op if absent)."""
    ir.async_delete_issue(hass, DOMAIN, _sample_rate_issue_id(entry))


@callback
def async_track_sample_rate(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Rtl433Coordinator,
) -> Callable[[], None]:
    """Raise / clear the low-sample-rate advisory as the hub's meta changes.

    Edge-triggered off ``signal_hub_update`` (which fires on every meta refresh):
    the advisory is raised once when the receiver enters the flagged state and
    cleared when it leaves it, so a card the user dismisses while still on a low
    rate is not immediately re-raised. Returns an unsubscribe callable.
    """
    state: dict[str, bool] = {"flagged": False}

    @callback
    def _evaluate(*_: Any) -> None:
        low = _sample_rate_looks_low(coordinator.meta)
        if low and not state["flagged"]:
            state["flagged"] = True
            async_raise_sample_rate_low(hass, entry, coordinator.meta)
        elif not low and state["flagged"]:
            state["flagged"] = False
            async_clear_sample_rate_low(hass, entry)

    _evaluate()  # meta may already be populated by the time we wire up
    return async_dispatcher_connect(hass, signal_hub_update(entry.entry_id), _evaluate)


@callback
def async_raise_hub_unreachable(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create (or refresh) the "server unreachable" repair issue for a hub."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _unreachable_issue_id(entry),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_UNREACHABLE,
        translation_placeholders={"title": entry.title},
    )


@callback
def async_clear_hub_unreachable(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the "server unreachable" repair issue for a hub (no-op if absent)."""
    ir.async_delete_issue(hass, DOMAIN, _unreachable_issue_id(entry))


@callback
def async_track_hub_reachability(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Rtl433Coordinator,
) -> Callable[[], None]:
    """Poll the coordinator's connection state and manage the repair issue.

    Returns an unsubscribe callable (register it via ``entry.async_on_unload``).
    The issue is raised only once the socket has been continuously disconnected
    for longer than the grace window (so reconnect backoff blips do not nag),
    and is cleared immediately on the first poll that sees it reconnected.
    """
    # ``None`` until we first observe a disconnect; holds the time the current
    # disconnected streak began so we can apply the grace window.
    state: dict[str, datetime | None] = {"disconnected_since": None}

    @callback
    def _poll(now: datetime) -> None:
        if coordinator.connected:
            if state["disconnected_since"] is not None:
                LOGGER.debug(
                    "rtl_433 hub %s reachable again; clearing repair issue",
                    entry.entry_id,
                )
            state["disconnected_since"] = None
            async_clear_hub_unreachable(hass, entry)
            return

        if state["disconnected_since"] is None:
            state["disconnected_since"] = now
            return

        if now - state["disconnected_since"] >= _UNREACHABLE_GRACE:
            async_raise_hub_unreachable(hass, entry)

    return async_track_time_interval(
        hass,
        _poll,
        _REACHABILITY_INTERVAL,
        name=f"rtl_433 reachability {entry.entry_id}",
    )


class HubRadioReplaceRepairFlow(RepairsFlow):
    """Fix flow for an unreachable hub: re-point it at a replacement radio.

    The dead radio is exactly what raised this issue, so this is the natural
    recovery surface. Leaving the fields unchanged simply revalidates and clears
    the card; entering a new radio id re-points the hub (preserving entry_id, so
    devices/entities/history survive) via the shared rebind helper.
    """

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> Any:
        entry = self._entry
        data = entry.data
        schema = vol.Schema(
            {
                vol.Optional(CONF_RADIO_ID, default=entry.unique_id or ""): str,
                vol.Required(CONF_HOST, default=data.get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=data.get(CONF_PORT, DEFAULT_PORT)): int,
                vol.Required(CONF_PATH, default=data.get(CONF_PATH, DEFAULT_PATH)): str,
                vol.Optional(CONF_SECURE, default=data.get(CONF_SECURE, False)): bool,
            }
        )
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            path = user_input[CONF_PATH]
            secure = user_input[CONF_SECURE]
            try:
                await Rtl433Coordinator.validate_connection(
                    self.hass, host, port, path, secure=secure
                )
            except CannotConnect:
                return self.async_show_form(
                    step_id="confirm",
                    data_schema=schema,
                    errors={"base": "cannot_connect"},
                    description_placeholders={"title": entry.title},
                )
            new_uid = (user_input.get(CONF_RADIO_ID) or "").strip() or (
                entry.unique_id or ""
            )
            status = await async_rebind_hub(
                self.hass,
                entry,
                new_uid,
                {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_PATH: path,
                    CONF_SECURE: secure,
                },
                title=f"rtl_433 ({host})",
            )
            if status == "already_configured":
                return self.async_show_form(
                    step_id="confirm",
                    data_schema=schema,
                    errors={"base": "id_in_use"},
                    description_placeholders={"title": entry.title},
                )
            async_clear_hub_unreachable(self.hass, entry)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=schema,
            description_placeholders={"title": entry.title},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Return the repair flow for an issue.

    The ``server_unreachable`` issue gets an actionable rebind flow (the dead
    radio is what raised it, so this is the natural recovery surface). Every
    other issue is informational/dismissible, so a simple confirm-and-dismiss
    flow is the right surface; those issues also self-clear, so the confirm
    dialog mainly lets a user dismiss a stale card.
    """
    if issue_id.startswith(ISSUE_UNREACHABLE):
        entry_id = issue_id[len(ISSUE_UNREACHABLE) + 1 :]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None:
            return HubRadioReplaceRepairFlow(entry)
    return ConfirmRepairFlow()


# Re-exported so the wiring module's intent is explicit at the import site.
__all__: list[str] = [
    "async_clear_hub_unreachable",
    "async_clear_sample_rate_low",
    "async_create_fix_flow",
    "async_raise_hub_unreachable",
    "async_raise_motion_moved",
    "async_raise_sample_rate_low",
    "async_track_hub_reachability",
    "async_track_sample_rate",
]
