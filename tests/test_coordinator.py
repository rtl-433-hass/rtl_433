"""Behavioral tests for the rtl_433 push coordinator's HA-side adaptation.

The transport half — WebSocket frame parsing, event normalization, reconnect
replay classification, and the HTTP ``/cmd`` getters/setters — now lives in
:class:`pyrtl_433.Rtl433Client` and is tested upstream in the library. This file
covers only what the *coordinator* still owns: applying a client-delivered,
already-classified :class:`~pyrtl_433.normalizer.NormalizedEvent` to per-device
runtime state, the discovery-registration gate, the availability watchdog, the
per-device effective-timeout resolution, and the device-update dispatch.

Events are injected the way the client would deliver them: by invoking the
coordinator's ``on_event`` callback (``_on_client_event``) with a crafted
``NormalizedEvent`` carrying the replay verdict the library already stamped. This
avoids re-testing the library's classification while still exercising every
coordinator-side branch through the real seam. The dispatcher send is patched so
fan-out is asserted without entities.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import patch

from freezegun import freeze_time
from pyrtl_433.normalizer import NormalizedEvent
from pyrtl_433.replay import DISCOVERY_BACKLOG_GRACE
import pytest

from custom_components.rtl_433.const import signal_device_update
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from homeassistant.util import dt as dt_util

DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"
_TRACE_LOGGER = "custom_components.rtl_433"


def _run(hass, coro):
    """Drive an async coordinator method to completion on the hass loop."""
    return hass.loop.run_until_complete(coro)


@pytest.fixture
async def coordinator(hass, hub_entry_builder):
    """Build a coordinator wired to a hub entry, with a 600s timeout.

    Async so construction runs inside the event loop: the coordinator now builds
    its :class:`pyrtl_433.Rtl433Client` in ``__init__`` (injecting HA's shared
    aiohttp session), which requires a running loop.
    """
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    return Rtl433Coordinator(
        hass,
        entry,
        host="rtl433.local",
        availability_timeout=600,
        skip_keys={"model", "id", "channel", "subtype", "time", "mic"},
    )


def _event(
    key="Acurite-606TX-42",
    model="Acurite-606TX",
    *,
    fields=None,
    is_replay=False,
    event_time=None,
) -> NormalizedEvent:
    """Build a client-shaped NormalizedEvent carrier for injection."""
    return NormalizedEvent(
        device_key=key,
        model=model,
        fields={"temperature_C": 21.4} if fields is None else fields,
        is_replay=is_replay,
        event_time=event_time,
    )


def _dev_signals(dispatch) -> list:
    """Pull each per-device dispatched NormalizedEvent."""
    return [
        call.args[2]
        for call in dispatch.call_args_list
        if call.args[1].startswith("rtl_433_device_update")
    ]


# --------------------------------------------------------------------------- #
# Ingest: state update + dispatch for a live event.                            #
# --------------------------------------------------------------------------- #
def test_live_event_records_state_and_dispatches(hass, coordinator):
    """A live event records per-device state and fans out on the device signal."""
    key = "Acurite-606TX-42"
    with patch(DISPATCH) as dispatch:
        coordinator._on_client_event(
            _event(fields={"temperature_C": 21.4, "humidity": 55})
        )

    assert coordinator.devices[key].fields == {"temperature_C": 21.4, "humidity": 55}
    assert coordinator.available[key] is True
    assert key in coordinator.last_seen
    assert coordinator.device_fields[key] == {"temperature_C", "humidity"}
    assert coordinator.seen_fields >= {"temperature_C", "humidity"}

    dispatch.assert_called_once()
    assert dispatch.call_args.args[1] == signal_device_update(
        coordinator.entry.entry_id, key
    )
    assert dispatch.call_args.args[2].is_replay is False


def test_replay_event_seeds_fields_but_not_liveness(hass, coordinator):
    """A replay carrier seeds device/field state but never refreshes liveness."""
    key = "Acurite-606TX-42"
    with patch(DISPATCH) as dispatch:
        coordinator._on_client_event(_event(is_replay=True))

    # Snapshot + fields seed so entities can restore on reconnect...
    assert key in coordinator.devices
    assert coordinator.device_fields[key] == {"temperature_C"}
    assert coordinator.seen_fields >= {"temperature_C"}
    # ...but last_seen / available are untouched (not resurrected by a replay).
    assert key not in coordinator.last_seen
    assert coordinator.available.get(key) is not True
    # The replay flag rides through to the entities unchanged.
    assert _dev_signals(dispatch)[0].is_replay is True


def test_offline_device_not_resurrected_by_replay(hass, coordinator):
    """Watchdog marks a device offline; a replay must not bring it back."""
    key = "Acurite-606TX-42"
    start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    with freeze_time(start), patch(DISPATCH):
        coordinator._on_client_event(_event())
    online_seen = coordinator.last_seen[key]

    with freeze_time(start + timedelta(seconds=601)), patch(DISPATCH):
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False

    # A replay frame arriving after the offline flip does not resurrect it.
    with freeze_time(start + timedelta(seconds=602)), patch(DISPATCH):
        coordinator._on_client_event(_event(is_replay=True))
    assert coordinator.available[key] is False
    assert coordinator.last_seen[key] == online_seen

    # A genuine live frame restores availability.
    with freeze_time(start + timedelta(seconds=700)), patch(DISPATCH):
        coordinator._on_client_event(_event())
    assert coordinator.available[key] is True
    assert coordinator.last_seen[key] != online_seen


# --------------------------------------------------------------------------- #
# Discovery-registration gate.                                                 #
# --------------------------------------------------------------------------- #
def test_new_device_callback_fires_once_when_discovery_enabled(hass, coordinator):
    """The new-device hook fires only on the first sighting of a device."""
    seen: list[tuple[str, str, bool]] = []
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda key, model, is_replay: seen.append(
        (key, model, is_replay)
    )

    with patch(DISPATCH):
        coordinator._on_client_event(_event())
        coordinator._on_client_event(_event())  # second sighting: no new callback

    assert seen == [("Acurite-606TX-42", "Acurite-606TX", False)]


def test_no_callback_when_discovery_disabled_or_unset(hass, coordinator):
    """No registration when discovery is off, and no crash when the hook is None."""
    seen: list[str] = []
    coordinator.discovery_enabled = False
    coordinator.new_device_callback = lambda k, m, r: seen.append(k)
    with patch(DISPATCH):
        coordinator._on_client_event(_event())
    assert seen == []

    # Discovery on but no callback wired -> must not raise.
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = None
    with patch(DISPATCH):
        coordinator._on_client_event(_event())
    assert "Acurite-606TX-42" in coordinator.devices


def test_backlog_event_seeds_state_but_does_not_register(hass, coordinator):
    """A pre-connection backlog frame seeds state without registering the device."""
    seen: list[str] = []
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda k, m, r: seen.append(k)
    conn = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    coordinator._connection_time = conn

    # event_time a clear minute before the connection (outside the grace window).
    backlog = _event(is_replay=True, event_time=conn - timedelta(seconds=60))
    with patch(DISPATCH):
        coordinator._on_client_event(backlog)

    assert seen == []  # not registered (backlog)
    assert "Acurite-606TX-42" in coordinator.devices  # but seeded

    # A later live frame (event_time after connect) registers exactly once.
    live = _event(event_time=conn + timedelta(seconds=5))
    with patch(DISPATCH):
        coordinator._on_client_event(live)
    assert seen == ["Acurite-606TX-42"]


def test_registration_uses_discovery_backlog_grace_boundary(hass, coordinator):
    """A frame exactly at ``connection_time - grace`` still registers (open bound)."""
    seen: list[str] = []
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda k, m, r: seen.append(k)
    conn = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    coordinator._connection_time = conn

    at_grace = _event(event_time=conn - DISCOVERY_BACKLOG_GRACE)
    with patch(DISPATCH):
        coordinator._on_client_event(at_grace)
    assert seen == ["Acurite-606TX-42"]  # not < boundary -> not backlog -> registers


def test_callback_exception_does_not_break_ingest(hass, coordinator):
    """A throwing new_device_callback is caught; the device is still tracked."""
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda k, m, r: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    with patch(DISPATCH):
        coordinator._on_client_event(_event())
    assert "Acurite-606TX-42" in coordinator.devices


# --------------------------------------------------------------------------- #
# Availability watchdog.                                                        #
# --------------------------------------------------------------------------- #
def test_watchdog_flips_unavailable_then_recovers(hass, coordinator):
    """Watchdog marks a silent device unavailable, then a new event recovers it."""
    key = "Acurite-606TX-42"
    start = dt_util.utcnow()
    with freeze_time(start), patch(DISPATCH):
        coordinator._on_client_event(_event())
    assert coordinator.available[key] is True

    # Exactly at the 600s boundary: not yet stale (comparison is >, not >=).
    with freeze_time(start + timedelta(seconds=600)), patch(DISPATCH) as at_bound:
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is True
    at_bound.assert_not_called()

    # One second past: flips unavailable and re-paints the cached frame
    # (is_repaint=True, is_replay=False) so measurement entities re-read.
    with freeze_time(start + timedelta(seconds=601)), patch(DISPATCH) as dispatch:
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False
    repaint = _dev_signals(dispatch)[0]
    assert repaint.is_repaint is True
    assert repaint.is_replay is False

    # A fresh event brings it back online.
    with freeze_time(start + timedelta(seconds=602)), patch(DISPATCH):
        coordinator._on_client_event(_event())
    assert coordinator.available[key] is True


def test_per_device_override_beats_hub_default(hass, coordinator):
    """The effective timeout uses the per-device resolver over the hub default."""
    key = "Acurite-606TX-42"
    coordinator.effective_timeout_resolver = lambda dk: 60 if dk == key else 600
    assert coordinator._effective_timeout(key) == 60

    start = dt_util.utcnow()
    with freeze_time(start), patch(DISPATCH):
        coordinator._on_client_event(_event())

    # 90s of silence exceeds the 60s override (but not the 600s hub default).
    with freeze_time(start + timedelta(seconds=90)), patch(DISPATCH):
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False


def test_effective_timeout_falls_back_on_resolver_error(hass, coordinator):
    """A throwing resolver falls back to the hub default instead of crashing."""

    def boom(_dk: str) -> int:
        raise RuntimeError("resolver exploded")

    coordinator.effective_timeout_resolver = boom
    assert coordinator._effective_timeout("any") == 600


# --------------------------------------------------------------------------- #
# forget_device eviction.                                                       #
# --------------------------------------------------------------------------- #
def test_forget_device_evicts_runtime_state(hass, coordinator):
    """forget_device clears the device from every runtime dict and re-arms discovery."""
    key = "Acurite-606TX-42"
    with patch(DISPATCH):
        coordinator._on_client_event(_event())
    assert key in coordinator.devices

    coordinator.forget_device(key)
    assert key not in coordinator.devices
    assert key not in coordinator.last_seen
    assert key not in coordinator.available
    assert key not in coordinator.device_fields
    assert key not in coordinator._discovered

    # forget on an unknown key is a safe no-op.
    coordinator.forget_device("nonexistent-key")


# --------------------------------------------------------------------------- #
# Coordinator-side DEBUG traces (discovery + unmapped fields).                  #
# --------------------------------------------------------------------------- #
def test_discovery_logs_new_device_line_once(hass, coordinator, caplog):
    """A first sighting logs the discovery DEBUG line once, with via_replay."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda key, model, is_replay: None

    with patch(DISPATCH):
        coordinator._on_client_event(_event())
        coordinator._on_client_event(_event())  # second sighting: no re-log

    lines = [
        m for m in caplog.messages if m.startswith("rtl_433 discovered new device")
    ]
    assert len(lines) == 1
    assert "Acurite-606TX-42" in lines[0]
    assert "model Acurite-606TX" in lines[0]
    assert "via_replay=False" in lines[0]


def test_unmapped_field_logged_once_per_device_field(hass, coordinator, caplog):
    """A field with no library descriptor logs once per (device, field)."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    coordinator.known_field_keys = frozenset({"temperature_C"})

    event = _event(fields={"temperature_C": 21.4, "mystery_field": 7})
    with patch(DISPATCH):
        coordinator._on_client_event(event)
        coordinator._on_client_event(event)  # identical -> no second log

    lines = [m for m in caplog.messages if "reported unmapped field(s)" in m]
    assert len(lines) == 1
    assert "Acurite-606TX-42" in lines[0]
    assert "mystery_field" in lines[0]
    assert "temperature_C" not in lines[0]


def test_unmapped_field_not_logged_when_library_empty(hass, coordinator, caplog):
    """With an empty ``known_field_keys`` the unmapped-field line is suppressed."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    coordinator.known_field_keys = frozenset()

    with patch(DISPATCH):
        coordinator._on_client_event(_event(fields={"mystery_field": 7}))

    assert [m for m in caplog.messages if "reported unmapped field(s)" in m] == []


# --------------------------------------------------------------------------- #
# Config-flow connectivity check delegates to the library.                     #
# --------------------------------------------------------------------------- #
def test_validate_connection_delegates_to_client(hass):
    """validate_connection forwards to the library client with the HA session."""
    from pyrtl_433 import CannotConnect

    with patch(
        "custom_components.rtl_433.coordinator.base.Rtl433Client.validate_connection",
    ) as validate:
        validate.return_value = True
        result = _run(
            hass,
            Rtl433Coordinator.validate_connection(hass, "rtl433.local", 8433, "/ws"),
        )
    assert result is True
    validate.assert_called_once()

    # The library's CannotConnect is re-exported from the coordinator package.
    from custom_components.rtl_433.coordinator.base import CannotConnect as ReExported

    assert ReExported is CannotConnect
