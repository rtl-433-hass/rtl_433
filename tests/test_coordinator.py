"""Tests for the rtl_433 push coordinator's frame handling and watchdog.

We never open a real socket: frames are fed straight into the coordinator's
``_handle_text_frame`` (the same method ``_read_frames`` calls for TEXT frames),
and the availability watchdog is invoked directly with time advanced via
freezegun. The dispatcher send is patched so we can assert fan-out without
entities.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time
import pytest

from custom_components.rtl_433.const import signal_device_update, signal_hub_update
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from homeassistant.util import dt as dt_util

DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"


@pytest.fixture
def coordinator(hass, hub_entry_builder):
    """Build a coordinator wired to a hub entry, with a short timeout."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    return Rtl433Coordinator(
        hass,
        entry,
        host="rtl433.local",
        availability_timeout=600,
        skip_keys={"model", "id", "channel", "subtype", "time", "mic"},
    )


def test_parses_frame_updates_state_and_dispatches(hass, coordinator):
    """A valid JSON text frame normalizes, records state, and dispatches."""
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "2026-05-25 10:00:00", "model": "Acurite-606TX", '
            '"id": 42, "temperature_C": 21.4, "humidity": 55}'
        )

    key = "Acurite-606TX-42"
    assert key in coordinator.devices
    assert coordinator.devices[key].fields == {"temperature_C": 21.4, "humidity": 55}
    assert coordinator.available[key] is True
    assert key in coordinator.last_seen
    assert coordinator.device_fields[key] == {"temperature_C", "humidity"}
    assert coordinator.seen_fields >= {"temperature_C", "humidity"}

    # Dispatched on the per-device signal with the normalized event.
    dispatch.assert_called_once()
    signal = dispatch.call_args.args[1]
    assert signal == signal_device_update(coordinator.entry.entry_id, key)


def test_ignores_empty_and_malformed_frames(hass, coordinator):
    """Empty, whitespace, non-JSON, and non-object frames are dropped quietly."""
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame("")
        coordinator._handle_text_frame("   ")
        coordinator._handle_text_frame("not json at all")
        coordinator._handle_text_frame("{bad json")
        coordinator._handle_text_frame("[1, 2, 3]")  # valid JSON, not an object
        coordinator._handle_text_frame("42")  # valid JSON scalar

    assert coordinator.devices == {}
    dispatch.assert_not_called()


def test_meta_frame_ignored(hass, coordinator):
    """An SDR/meta frame is not a device event: no device, no seen_fields."""
    with patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"center_frequency": 433920000, "samp_rate": 250000, '
            '"frequencies": [433920000], "hop_times": [600]}'
        )

    assert coordinator.devices == {}
    assert coordinator.seen_fields == set()


def test_stats_frame_ignored(hass, coordinator):
    """A server-stats frame is not a device event: no device, no seen_fields."""
    with patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"enabled": 5, "since": "2026-05-26T10:00:00", '
            '"frames": {"count": 3, "fsk": 1, "events": 9}}'
        )

    assert coordinator.devices == {}
    assert coordinator.seen_fields == set()


def test_rpc_result_error_frames_ignored(hass, coordinator):
    """RPC result/error frames are not device events: no device, no seen_fields."""
    with patch(DISPATCH):
        coordinator._handle_text_frame('{"result": "ok"}')
        coordinator._handle_text_frame('{"error": "bad"}')

    assert coordinator.devices == {}
    assert coordinator.seen_fields == set()


def test_shutdown_frame_flips_connectivity_and_emits(hass, coordinator):
    """A shutdown frame flips connectivity off and emits the hub-update signal."""
    coordinator.connected = True
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame('{"shutdown": "goodbye"}')

    assert coordinator.connected is False
    assert coordinator.devices == {}
    assert coordinator.seen_fields == set()

    hub_signal = signal_hub_update(coordinator.entry.entry_id)
    sent_signals = [call.args[1] for call in dispatch.call_args_list]
    assert hub_signal in sent_signals


def test_model_less_identity_event_creates_device(hass, coordinator):
    """A frame with an identity key but no model still creates its device."""
    with patch(DISPATCH):
        coordinator._handle_text_frame('{"channel": 1, "temperature_C": 5}')

    assert "unknown-ch1" in coordinator.devices


def test_new_device_callback_fires_once_when_discovery_enabled(hass, coordinator):
    """The new-device hook fires only on the first sighting of a device."""
    seen: list[tuple[str, str]] = []
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda key, model: seen.append((key, model))

    frame = '{"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4}'
    with patch(DISPATCH):
        coordinator._handle_text_frame(frame)
        coordinator._handle_text_frame(frame)  # second sighting: no new callback

    assert seen == [("Acurite-606TX-42", "Acurite-606TX")]


def test_watchdog_flips_unavailable_then_recovers(hass, coordinator):
    """Watchdog marks a silent device unavailable, then a new event recovers it."""
    key = "Acurite-606TX-42"
    frame = '{"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4}'

    start = dt_util.utcnow()
    with freeze_time(start), patch(DISPATCH):
        coordinator._handle_text_frame(frame)
    assert coordinator.available[key] is True

    # Still within the 600s window: stays available.
    with freeze_time(start + timedelta(seconds=300)), patch(DISPATCH):
        hass.loop.run_until_complete(coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is True

    # Past the timeout: watchdog flips to unavailable and re-dispatches.
    with (
        freeze_time(start + timedelta(seconds=601)),
        patch(DISPATCH) as dispatch,
    ):
        hass.loop.run_until_complete(coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False
    dispatch.assert_called_once()

    # A fresh event brings it back online.
    with freeze_time(start + timedelta(seconds=602)), patch(DISPATCH):
        coordinator._handle_text_frame(frame)
    assert coordinator.available[key] is True


def test_per_device_override_beats_hub_default(hass, coordinator):
    """The effective timeout uses the per-device resolver over the hub default."""
    key = "Acurite-606TX-42"
    frame = '{"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4}'

    # Resolver returns a short 60s override for this device.
    coordinator.effective_timeout_resolver = lambda dk: 60 if dk == key else 600
    assert coordinator._effective_timeout(key) == 60

    start = dt_util.utcnow()
    with freeze_time(start), patch(DISPATCH):
        coordinator._handle_text_frame(frame)

    # 90s of silence exceeds the 60s override (but not the 600s hub default),
    # so the device must be unavailable -> the override won.
    with freeze_time(start + timedelta(seconds=90)), patch(DISPATCH):
        hass.loop.run_until_complete(coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False


def test_effective_timeout_falls_back_on_resolver_error(hass, coordinator):
    """A throwing resolver falls back to the hub default instead of crashing."""

    def boom(_dk: str) -> int:
        raise RuntimeError("resolver exploded")

    coordinator.effective_timeout_resolver = boom
    assert coordinator._effective_timeout("any") == 600
