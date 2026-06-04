"""Tests for diagnostics export and the repairs reachability state machine.

Diagnostics: assert the host is redacted and that ``unmatched_field_keys``
surfaces an observed-but-unmapped field for library contributors. Repairs:
drive the reachability poller through the grace window and assert the issue is
raised only after sustained disconnection and cleared on reconnect.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from custom_components.rtl_433 import repairs
from custom_components.rtl_433.const import (
    CONF_HOST,
    CONF_PATH,
    CONF_PORT,
    CONF_RADIO_ID,
    DOMAIN,
)
from custom_components.rtl_433.coordinator import CannotConnect, Rtl433Coordinator
from custom_components.rtl_433.diagnostics import async_get_config_entry_diagnostics
from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

# The coordinator's reachability validator, patched so no socket is opened.
VALIDATE = "custom_components.rtl_433.coordinator.Rtl433Coordinator.validate_connection"


class _FakeCoordinator:
    """Minimal stand-in exposing the runtime state diagnostics reads."""

    def __init__(self) -> None:
        self.host = "secret-host.local"
        self.port = 8433
        self.path = "/ws"
        self.secure = False
        self.connected = True
        self.discovery_enabled = True
        self.availability_timeout = 600
        self.seen_fields = {"temperature_C", "humidity", "made_up_field"}
        self.devices = {}
        self.device_fields = {}
        self.last_seen = {}
        self.available = {}

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}{self.path}"


async def test_diagnostics_redacts_host_and_reports_unmatched(
    hass: HomeAssistant, hub_entry_builder
):
    """Diagnostics redact the host and list observed-but-unmapped field keys."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    coordinator = _FakeCoordinator()
    # The shipped library so lookup() resolves the real fields.
    from custom_components.rtl_433.mapping import load_library

    registry, skip_keys = load_library()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["_library"] = (registry, skip_keys)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["coordinator_loaded"] is True
    # Host is redacted in both the entry data and the connection block.
    assert diag["entry"]["data"][CONF_HOST] != "rtl433.local"
    assert diag["connection"][CONF_HOST] != "secret-host.local"
    assert diag["connection"]["ws_url"] != coordinator.ws_url
    # temperature_C / humidity are mapped; only the made-up field is unmatched.
    assert diag["unmatched_field_keys"] == ["made_up_field"]
    assert "temperature_C" not in diag["unmatched_field_keys"]


async def test_diagnostics_when_coordinator_absent(
    hass: HomeAssistant, hub_entry_builder
):
    """With no loaded coordinator, diagnostics report the static entry only."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["coordinator_loaded"] is False
    assert "connection" not in diag


async def test_reachability_raises_after_grace_and_clears_on_reconnect(
    hass: HomeAssistant, hub_entry_builder
):
    """The repair issue surfaces only after sustained disconnect, then clears."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
    coordinator.connected = False

    # Capture the poll callback the tracker schedules.
    polls: list = []
    real_track = repairs.async_track_time_interval

    def _capture(hass_, action, interval, name=None):
        polls.append(action)
        return real_track(hass_, action, interval, name=name)

    with patch.object(repairs, "async_track_time_interval", _capture):
        unsub = repairs.async_track_hub_reachability(hass, entry, coordinator)

    poll = polls[0]
    issue_reg = ir.async_get(hass)
    issue_id = repairs._unreachable_issue_id(entry)

    start = dt_util.utcnow()
    # First poll: starts the disconnected streak, no issue yet.
    poll(start)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

    # Within the grace window: still no issue.
    poll(start + timedelta(seconds=60))
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

    # Past the grace window: the issue is raised.
    poll(start + timedelta(seconds=120))
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    # Reconnect -> the issue clears on the next poll.
    coordinator.connected = True
    poll(start + timedelta(seconds=150))
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

    unsub()


# --------------------------------------------------------------------------- #
# Low-sample-rate advisory                                                    #
# --------------------------------------------------------------------------- #
def test_sample_rate_looks_low_predicate():
    """The band heuristic flags only a single high-band freq at a low rate."""
    looks_low = repairs._sample_rate_looks_low
    # 915 MHz at the 250k default -> flagged.
    assert looks_low({"center_frequency": 915_000_000, "samp_rate": 250_000})
    # Same band but already widened -> not flagged.
    assert not looks_low({"center_frequency": 915_000_000, "samp_rate": 1_024_000})
    # Low band (433.92 MHz) at 250k -> not flagged.
    assert not looks_low({"center_frequency": 433_920_000, "samp_rate": 250_000})
    # High band but hopping (multiple frequencies) -> never flagged.
    assert not looks_low(
        {
            "center_frequency": 915_000_000,
            "samp_rate": 250_000,
            "frequencies": [433_920_000, 915_000_000],
        }
    )
    # Missing / non-numeric values -> not flagged (defensive).
    assert not looks_low({"samp_rate": 250_000})
    assert not looks_low({"center_frequency": 915_000_000})
    assert not looks_low({"center_frequency": True, "samp_rate": 250_000})


async def test_sample_rate_advisory_edge_triggered(
    hass: HomeAssistant, hub_entry_builder
):
    """The advisory raises on entering the flagged state and clears on leaving."""
    from custom_components.rtl_433.const import signal_hub_update
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")

    issue_reg = ir.async_get(hass)
    issue_id = repairs._sample_rate_issue_id(entry)

    # Wire the tracker with meta already in the good (low-band) state.
    coordinator.meta = {"center_frequency": 433_920_000, "samp_rate": 250_000}
    unsub = repairs.async_track_sample_rate(hass, entry, coordinator)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

    # Retune into the high band at the default rate -> advisory raised.
    coordinator.meta = {"center_frequency": 915_000_000, "samp_rate": 250_000}
    async_dispatcher_send(hass, signal_hub_update(entry.entry_id))
    await hass.async_block_till_done()
    issue = issue_reg.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders["frequency"] == "915"

    # A user dismissing it while still on a low rate must not re-raise it.
    repairs.async_clear_sample_rate_low(hass, entry)
    async_dispatcher_send(hass, signal_hub_update(entry.entry_id))
    await hass.async_block_till_done()
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

    # Raising the sample rate, then dropping back, re-triggers the edge.
    coordinator.meta = {"center_frequency": 915_000_000, "samp_rate": 1_024_000}
    async_dispatcher_send(hass, signal_hub_update(entry.entry_id))
    await hass.async_block_till_done()
    coordinator.meta = {"center_frequency": 915_000_000, "samp_rate": 250_000}
    async_dispatcher_send(hass, signal_hub_update(entry.entry_id))
    await hass.async_block_till_done()
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    unsub()


# --------------------------------------------------------------------------- #
# Unreachable-hub repair: rebind fix flow                                     #
# --------------------------------------------------------------------------- #
async def test_create_fix_flow_routes_by_issue_id(
    hass: HomeAssistant, hub_entry_builder
):
    """The unreachable issue gets the rebind flow; other issues get confirm."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    unreachable = await repairs.async_create_fix_flow(
        hass, repairs._unreachable_issue_id(entry), None
    )
    assert isinstance(unreachable, repairs.HubRadioReplaceRepairFlow)

    sample_rate = await repairs.async_create_fix_flow(
        hass, repairs._sample_rate_issue_id(entry), None
    )
    assert isinstance(sample_rate, ConfirmRepairFlow)


async def test_rebind_fix_flow_repoints_hub_and_clears_issue(
    hass: HomeAssistant, hub_entry_builder
):
    """Driving the fix flow rebinds the hub in place and clears the issue.

    An adopted hub (stable radio id, populated devices) goes unreachable; the
    repair re-points it at a replacement radio, preserving entry_id and the
    nested devices, and the unreachable card disappears.
    """
    devices = {"acurite-1": {"model": "Acurite", "fields": {}}}
    entry = hub_entry_builder(devices=devices)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="radio-old")

    original_entry_id = entry.entry_id
    repairs.async_raise_hub_unreachable(hass, entry)
    issue_reg = ir.async_get(hass)
    issue_id = repairs._unreachable_issue_id(entry)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    flow = repairs.HubRadioReplaceRepairFlow(entry)
    flow.hass = hass

    # The init step just shows the confirm form.
    init = await flow.async_step_init()
    assert init["type"] == FlowResultType.FORM
    assert init["step_id"] == "confirm"

    user_input = {
        CONF_RADIO_ID: "radio-new",
        CONF_HOST: "rtl433-new.local",
        CONF_PORT: 8433,
        CONF_PATH: "/ws",
        repairs.CONF_SECURE: False,
    }
    # Patch both the reachability check (no socket) and entry setup (no real
    # coordinator) so async_rebind_hub's in-place reload is a no-op.
    with (
        patch(VALIDATE, AsyncMock(return_value=True)),
        patch(
            "custom_components.rtl_433.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    ):
        result = await flow.async_step_confirm(user_input)
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY

    # Same entry, re-pointed at the new radio with its devices intact.
    rebound = hass.config_entries.async_get_entry(original_entry_id)
    assert rebound is not None
    assert rebound.unique_id == "radio-new"
    assert rebound.data[CONF_HOST] == "rtl433-new.local"
    assert rebound.data["devices"] == devices

    # And the unreachable card is gone.
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None


async def test_rebind_fix_flow_cannot_connect_reshows_form(
    hass: HomeAssistant, hub_entry_builder
):
    """A failed connection re-shows the form and leaves the hub unchanged."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="radio-old")

    flow = repairs.HubRadioReplaceRepairFlow(entry)
    flow.hass = hass

    user_input = {
        CONF_RADIO_ID: "radio-new",
        CONF_HOST: "rtl433-new.local",
        CONF_PORT: 8433,
        CONF_PATH: "/ws",
        repairs.CONF_SECURE: False,
    }
    with patch(VALIDATE, AsyncMock(side_effect=CannotConnect("nope"))):
        result = await flow.async_step_confirm(user_input)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": "cannot_connect"}

    # No rebind happened: the entry still points at the old radio/host.
    assert entry.unique_id == "radio-old"
    assert entry.data[CONF_HOST] == "rtl433.local"
