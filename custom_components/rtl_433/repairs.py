"""Repairs surface for the rtl_433 integration.

Scope is deliberately tight (per the plan, repairs cover only *genuinely
actionable* problems, not speculative ones): the single issue raised here is
"the configured rtl_433 server is unreachable". It is created when a hub's
WebSocket coordinator has been unable to stay connected and is cleared
automatically the moment the connection comes back.

The coordinator package is intentionally left untouched (it owns no HA-repairs
knowledge). Instead :func:`async_track_hub_reachability` polls the coordinator's
``connected`` flag on an interval and translates sustained disconnection into a
repair issue. ``__init__.py`` wires this in during hub setup and registers the
returned unsubscribe via ``entry.async_on_unload``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, LOGGER
from .coordinator import Rtl433Coordinator

# How often reachability is evaluated. Aligned to be responsive without being
# chatty; the issue only flips on a sustained state change.
_REACHABILITY_INTERVAL = timedelta(seconds=30)

# How long the socket may stay down before the issue is raised. A brief blip
# (reconnect backoff) should not raise an issue, so we require the disconnect to
# persist across at least one full grace window before surfacing it.
_UNREACHABLE_GRACE = timedelta(seconds=90)

# translation_key / issue_id prefix for the unreachable-server issue.
ISSUE_UNREACHABLE = "server_unreachable"


def _unreachable_issue_id(entry: ConfigEntry) -> str:
    """Return the per-hub issue id for the unreachable-server repair."""
    return f"{ISSUE_UNREACHABLE}_{entry.entry_id}"


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


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Return the repair flow for an issue.

    Every issue this integration raises is informational/actionable by fixing
    the underlying condition (e.g. bringing the rtl_433 server back online), so
    a simple confirm-and-dismiss flow is the right surface. The issue also
    self-clears on reconnect, so the confirm dialog mainly lets a user dismiss a
    stale card.
    """
    return ConfirmRepairFlow()


# Re-exported so the wiring module's intent is explicit at the import site.
__all__: list[str] = [
    "async_clear_hub_unreachable",
    "async_create_fix_flow",
    "async_raise_hub_unreachable",
    "async_track_hub_reachability",
]
