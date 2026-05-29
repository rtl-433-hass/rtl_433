"""Mutation-killing tests for custom_components/rtl_433/coordinator/base.py.

These tests are designed to kill specific mutants in base.py by asserting:
- Exact values (not just truthiness)
- Both branches of every conditional
- Boundary conditions (timeout comparisons, threshold comparisons)
- Frame classification decisions (live vs replay)
- Dispatched signals and payloads
- Device add/evict decisions
- Availability state transitions
- HTTP getter outputs and failure handling
- Managed SDR desired-state read/write API

No source files are modified. All time-based tests use freezegun for
determinism.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from freezegun import freeze_time
import pytest

from custom_components.rtl_433.const import signal_device_update, signal_hub_update
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.coordinator.base import (
    CannotConnect,
    _build_cmd_url,
    _build_ws_url,
)
from custom_components.rtl_433.sdr_settings import (
    KEY_CENTER_FREQUENCY,
    KEY_CONVERSION_MODE,
    KEY_GAIN_AUTO,
    KEY_GAIN_DB,
    KEY_HOP_INTERVAL,
    KEY_PPM_ERROR,
    KEY_SAMPLE_RATE,
)
from homeassistant.util import dt as dt_util

DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_META_RESULT = {
    "center_frequency": 433920000,
    "samp_rate": 250000,
    "conversion_mode": 0,
    "frequencies": [433920000, 868000000],
    "hop_times": [600, 30],
}
_STATS_RESULT = {
    "enabled": 5,
    "since": "2026-05-26T10:00:00",
    "frames": {"count": 3, "fsk": 1, "events": 9},
    "stats": [],
}
_META_BODY = {"result": _META_RESULT}
_STATS_BODY = {"result": _STATS_RESULT}


def _mock_cmd(aioclient_mock, *, host="rtl433.local", port=8433, gain="32.8", ppm=2):
    """Register per-command /cmd GET stubs."""
    url = f"http://{host}:{port}/cmd"
    aioclient_mock.get(url, params={"cmd": "get_meta"}, json=_META_BODY)
    aioclient_mock.get(url, params={"cmd": "get_gain"}, json={"result": gain})
    aioclient_mock.get(url, params={"cmd": "get_ppm_error"}, json={"result": ppm})
    aioclient_mock.get(url, params={"cmd": "get_stats"}, json=_STATS_BODY)


def _run(hass, coro):
    """Drive an async coordinator method to completion on the hass loop."""
    return hass.loop.run_until_complete(coro)


@pytest.fixture
def coordinator(hass, hub_entry_builder):
    """Build a coordinator wired to a hub entry, with a 600s timeout."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    return Rtl433Coordinator(
        hass,
        entry,
        host="rtl433.local",
        availability_timeout=600,
        skip_keys={"model", "id", "channel", "subtype", "time", "mic"},
    )


def _dispatched_replay_flags(dispatch) -> list[bool]:
    """Pull the is_replay flag off each per-device dispatched event."""
    return [
        call.args[2].is_replay
        for call in dispatch.call_args_list
        if call.args[1].startswith("rtl_433_device_update")
    ]


# ---------------------------------------------------------------------------
# URL builder tests
# ---------------------------------------------------------------------------


def test_build_ws_url_plain():
    """ws:// URL with a path already starting with /."""
    assert _build_ws_url("host", 8433, "/ws") == "ws://host:8433/ws"


def test_build_ws_url_no_leading_slash():
    """A path without a leading / gets one prepended."""
    assert _build_ws_url("host", 8433, "ws") == "ws://host:8433/ws"


def test_build_ws_url_secure():
    """secure=True produces wss:// not ws://."""
    assert _build_ws_url("host", 8433, "/ws", secure=True) == "wss://host:8433/ws"


def test_build_ws_url_secure_false():
    """secure=False produces ws:// (explicit False, not default)."""
    assert (
        _build_ws_url("host", 9000, "/events", secure=False) == "ws://host:9000/events"
    )


def test_build_cmd_url_plain():
    """http:// URL always points to /cmd at server root."""
    assert _build_cmd_url("rtl433.local", 8433) == "http://rtl433.local:8433/cmd"


def test_build_cmd_url_secure():
    """secure=True switches to https://."""
    assert (
        _build_cmd_url("rtl433.local", 8433, secure=True)
        == "https://rtl433.local:8433/cmd"
    )


def test_ws_url_property(hass, hub_entry_builder):
    """The ws_url property returns the correctly formed WebSocket URL."""
    entry = hub_entry_builder(host="myhost", port=1234, path="/mypath", secure=False)
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="myhost", port=1234, path="/mypath")
    assert coord.ws_url == "ws://myhost:1234/mypath"


def test_ws_url_property_secure(hass, hub_entry_builder):
    """The ws_url property uses wss:// when secure=True."""
    entry = hub_entry_builder(host="myhost", port=1234, secure=True)
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="myhost", port=1234, secure=True)
    assert coord.ws_url == "wss://myhost:1234/ws"


# ---------------------------------------------------------------------------
# _unwrap_result
# ---------------------------------------------------------------------------


def test_unwrap_result_with_result_key():
    """A {'result': value} envelope is unwrapped to the inner value."""
    assert Rtl433Coordinator._unwrap_result({"result": 42}) == 42


def test_unwrap_result_with_result_none():
    """{'result': None} unwraps to None (not the dict itself)."""
    assert Rtl433Coordinator._unwrap_result({"result": None}) is None


def test_unwrap_result_bare_dict_no_result_key():
    """A dict without 'result' is returned as-is."""
    d = {"other": "value"}
    assert Rtl433Coordinator._unwrap_result(d) is d


def test_unwrap_result_bare_string():
    """A bare string is returned as-is."""
    assert Rtl433Coordinator._unwrap_result("bare") == "bare"


def test_unwrap_result_bare_int():
    """A bare integer is returned as-is."""
    assert Rtl433Coordinator._unwrap_result(99) == 99


def test_unwrap_result_none():
    """None is returned as-is (not treated as a result envelope)."""
    assert Rtl433Coordinator._unwrap_result(None) is None


def test_unwrap_result_string_gain():
    """A gain string in a result envelope is unwrapped correctly."""
    assert Rtl433Coordinator._unwrap_result({"result": "32.8"}) == "32.8"


def test_unwrap_result_empty_string_gain():
    """Empty string gain (auto) in a result envelope is unwrapped correctly."""
    assert Rtl433Coordinator._unwrap_result({"result": ""}) == ""


# ---------------------------------------------------------------------------
# _refresh_meta: edge cases for mutant-killing
# ---------------------------------------------------------------------------


def test_refresh_meta_single_hop_time_derives_hop_interval(
    hass, coordinator, aioclient_mock
):
    """hop_interval is derived from hop_times[0] when the list is non-empty."""
    meta = {
        "result": {
            "center_frequency": 433920000,
            "samp_rate": 250000,
            "conversion_mode": 0,
            "frequencies": [433920000],
            "hop_times": [300],
        }
    }
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_meta"}, json=meta
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_gain"}, json={"result": ""}
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_ppm_error"},
        json={"result": 0},
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    assert coordinator.meta["hop_interval"] == 300


def test_refresh_meta_empty_hop_times_no_hop_interval(
    hass, coordinator, aioclient_mock
):
    """An empty hop_times list does NOT produce a hop_interval key."""
    meta = {
        "result": {
            "center_frequency": 433920000,
            "samp_rate": 250000,
            "hop_times": [],
        }
    }
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_meta"}, json=meta
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_gain"},
        json={"result": None},
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_ppm_error"},
        json={"result": None},
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    assert "hop_interval" not in coordinator.meta


def test_refresh_meta_no_hop_times_key_no_hop_interval(
    hass, coordinator, aioclient_mock
):
    """A meta without a hop_times key does NOT produce a hop_interval."""
    meta = {"result": {"center_frequency": 433920000, "samp_rate": 250000}}
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_meta"}, json=meta
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_gain"},
        json={"result": None},
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_ppm_error"},
        json={"result": None},
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    assert "hop_interval" not in coordinator.meta


def test_refresh_meta_bool_ppm_excluded(hass, coordinator, aioclient_mock):
    """A bool ppm_error value is NOT added to meta (bool is an int subclass)."""
    meta = {"result": {"center_frequency": 433920000}}
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_meta"}, json=meta
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_gain"},
        json={"result": None},
    )
    # True is isinstance(True, int) but also isinstance(True, bool) -> excluded
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_ppm_error"},
        json={"result": True},
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    assert "ppm_error" not in coordinator.meta


def test_refresh_meta_int_ppm_zero_included(hass, coordinator, aioclient_mock):
    """ppm_error=0 (int, not bool) IS added to meta."""
    meta = {"result": {"center_frequency": 433920000}}
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_meta"}, json=meta
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_gain"},
        json={"result": None},
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_ppm_error"},
        json={"result": 0},
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    assert coordinator.meta["ppm_error"] == 0


def test_refresh_meta_int_gain_not_added(hass, coordinator, aioclient_mock):
    """A non-string gain value (int) is NOT added to meta (only strings accepted)."""
    meta = {"result": {"center_frequency": 433920000}}
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_meta"}, json=meta
    )
    # gain=40 as int -> not isinstance(gain, str) -> not added
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_gain"}, json={"result": 40}
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_ppm_error"},
        json={"result": None},
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    assert "gain" not in coordinator.meta


def test_refresh_meta_none_gain_not_added(hass, coordinator, aioclient_mock):
    """A null gain value is NOT added to meta."""
    meta = {"result": {"center_frequency": 433920000}}
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_meta"}, json=meta
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_gain"},
        json={"result": None},
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_ppm_error"},
        json={"result": None},
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    assert "gain" not in coordinator.meta


def test_refresh_meta_merges_with_existing(hass, coordinator, aioclient_mock):
    """_refresh_meta merges new keys into existing meta (not a full replacement)."""
    coordinator.meta = {"gain": "32.8", "some_old_key": "preserved"}
    meta = {"result": {"center_frequency": 500000000}}
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_meta"}, json=meta
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_gain"},
        json={"result": "40"},
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_ppm_error"},
        json={"result": None},
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    # New key from meta
    assert coordinator.meta["center_frequency"] == 500000000
    # Gain updated from gain getter
    assert coordinator.meta["gain"] == "40"
    # Old key preserved
    assert coordinator.meta["some_old_key"] == "preserved"


def test_refresh_meta_no_new_data_no_emit(hass, coordinator, aioclient_mock):
    """When all three getters return None (no usable data), no hub signal is emitted."""
    # All getters fail -> None
    aioclient_mock.get("http://rtl433.local:8433/cmd", status=500)

    with patch(DISPATCH) as dispatch:
        _run(hass, coordinator._refresh_meta())

    # No new meta -> no emit
    assert coordinator.meta == {}
    hub_signal = signal_hub_update(coordinator.entry.entry_id)
    sent = [c.args[1] for c in dispatch.call_args_list]
    assert hub_signal not in sent


def test_refresh_stats_none_no_update(hass, coordinator, aioclient_mock):
    """When get_stats returns None (getter fails), stats is not updated."""
    coordinator.stats = {"frames": {"events": 5}}
    aioclient_mock.get("http://rtl433.local:8433/cmd", status=500)

    with patch(DISPATCH) as dispatch:
        _run(hass, coordinator._refresh_stats())

    # Unchanged
    assert coordinator.stats == {"frames": {"events": 5}}
    dispatch.assert_not_called()


def test_refresh_stats_non_dict_not_stored(hass, coordinator, aioclient_mock):
    """A non-dict stats response is not stored (isinstance guard)."""
    coordinator.stats = {"old": "data"}
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_stats"},
        json={"result": "not_a_dict"},
    )

    with patch(DISPATCH) as dispatch:
        _run(hass, coordinator._refresh_stats())

    assert coordinator.stats == {"old": "data"}
    dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# _classify_frame: routing decisions
# ---------------------------------------------------------------------------


def test_classify_frame_routes_model_event(hass, coordinator):
    """A frame with 'model' key routes to _process_event."""
    with patch.object(coordinator, "_process_event") as mock_proc:
        coordinator._classify_frame({"model": "TestModel", "temperature_C": 22.0})
    mock_proc.assert_called_once()


def test_classify_frame_routes_id_only_event(hass, coordinator):
    """A frame with 'id' but no model still routes to _process_event (identity key)."""
    with patch.object(coordinator, "_process_event") as mock_proc:
        coordinator._classify_frame({"id": 42, "temperature_C": 5.0})
    mock_proc.assert_called_once()


def test_classify_frame_routes_channel_event(hass, coordinator):
    """A frame with 'channel' routes to _process_event."""
    with patch.object(coordinator, "_process_event") as mock_proc:
        coordinator._classify_frame({"channel": 1, "humidity": 65})
    mock_proc.assert_called_once()


def test_classify_frame_routes_subtype_event(hass, coordinator):
    """A frame with 'subtype' routes to _process_event."""
    with patch.object(coordinator, "_process_event") as mock_proc:
        coordinator._classify_frame({"subtype": "A", "rssi": -80})
    mock_proc.assert_called_once()


def test_classify_frame_shutdown_routes_to_handle_shutdown(hass, coordinator):
    """A shutdown frame routes to _handle_shutdown, not _process_event."""
    with (
        patch.object(coordinator, "_process_event") as mock_proc,
        patch.object(coordinator, "_handle_shutdown") as mock_shut,
    ):
        coordinator._classify_frame({"shutdown": "goodbye"})
    mock_proc.assert_not_called()
    mock_shut.assert_called_once()


def test_classify_frame_meta_frame_ignored(hass, coordinator):
    """A meta-only frame (no model/id/channel/subtype/shutdown) is dropped."""
    with (
        patch.object(coordinator, "_process_event") as mock_proc,
        patch.object(coordinator, "_handle_shutdown") as mock_shut,
    ):
        coordinator._classify_frame(
            {"center_frequency": 433920000, "samp_rate": 250000}
        )
    mock_proc.assert_not_called()
    mock_shut.assert_not_called()


def test_classify_frame_result_frame_ignored(hass, coordinator):
    """A {'result': 'ok'} RPC frame is dropped (no model/identity/shutdown)."""
    with (
        patch.object(coordinator, "_process_event") as mock_proc,
        patch.object(coordinator, "_handle_shutdown") as mock_shut,
    ):
        coordinator._classify_frame({"result": "ok"})
    mock_proc.assert_not_called()
    mock_shut.assert_not_called()


def test_classify_frame_model_none_not_event(hass, coordinator):
    """A frame with model=None and no other identity keys is NOT an event."""
    # model=None means event.get("model") is None -> is_event = False
    # and none of the identity keys are present -> is_event = False
    with (
        patch.object(coordinator, "_process_event") as mock_proc,
        patch.object(coordinator, "_handle_shutdown") as mock_shut,
    ):
        coordinator._classify_frame({"model": None, "temperature_C": 5.0})
    mock_proc.assert_not_called()
    mock_shut.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_shutdown
# ---------------------------------------------------------------------------


def test_handle_shutdown_when_not_connected(hass, coordinator):
    """Shutdown when already disconnected: connected stays False, hub signal sent."""
    coordinator.connected = False
    with patch(DISPATCH) as dispatch:
        coordinator._handle_shutdown()

    assert coordinator.connected is False
    hub_signal = signal_hub_update(coordinator.entry.entry_id)
    sent = [c.args[1] for c in dispatch.call_args_list]
    assert hub_signal in sent


def test_handle_shutdown_when_connected(hass, coordinator):
    """Shutdown when connected: flips to False and emits hub-update signal."""
    coordinator.connected = True
    with patch(DISPATCH) as dispatch:
        coordinator._handle_shutdown()

    assert coordinator.connected is False
    hub_signal = signal_hub_update(coordinator.entry.entry_id)
    sent = [c.args[1] for c in dispatch.call_args_list]
    assert hub_signal in sent


# ---------------------------------------------------------------------------
# _emit_hub_update
# ---------------------------------------------------------------------------


def test_emit_hub_update_sends_correct_signal(hass, coordinator):
    """_emit_hub_update sends exactly the hub-update signal for this entry."""
    with patch(DISPATCH) as dispatch:
        coordinator._emit_hub_update()

    dispatch.assert_called_once()
    assert dispatch.call_args.args[1] == signal_hub_update(coordinator.entry.entry_id)


# ---------------------------------------------------------------------------
# forget_device
# ---------------------------------------------------------------------------


def test_forget_device_removes_all_state(hass, coordinator):
    """forget_device clears the device from all four runtime dicts."""
    key = "Acurite-606TX-42"
    with freeze_time("2026-05-25 10:00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25 10:00:00", "model": "Acurite-606TX", '
            '"id": 42, "temperature_C": 21.4}'
        )

    # All four dicts populated
    assert key in coordinator.devices
    assert key in coordinator.last_seen
    assert key in coordinator.available
    assert key in coordinator.device_fields

    coordinator.forget_device(key)

    assert key not in coordinator.devices
    assert key not in coordinator.last_seen
    assert key not in coordinator.available
    assert key not in coordinator.device_fields


def test_forget_device_unknown_key_is_safe(hass, coordinator):
    """forget_device on a key never seen does not raise."""
    coordinator.forget_device("nonexistent-key")  # must not raise


def test_forget_device_leaves_other_devices_intact(hass, coordinator):
    """forget_device removes only the specified device, not others."""
    with freeze_time("2026-05-25 10:00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25 10:00:00", "model": "Acurite-606TX", '
            '"id": 1, "temperature_C": 21.0}'
        )
        coordinator._handle_text_frame(
            '{"time": "2026-05-25 10:00:00", "model": "Acurite-606TX", '
            '"id": 2, "temperature_C": 22.0}'
        )

    coordinator.forget_device("Acurite-606TX-1")

    assert "Acurite-606TX-1" not in coordinator.devices
    assert "Acurite-606TX-2" in coordinator.devices


# ---------------------------------------------------------------------------
# _process_event: state updates, availability, field tracking
# ---------------------------------------------------------------------------


def test_process_event_stamps_exact_utcnow_as_last_seen(hass, coordinator):
    """last_seen is stamped with utcnow() at the time of the live event."""
    frozen = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    with freeze_time(frozen), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", "id": 1, "val": 5}'
        )

    assert coordinator.last_seen["TestModel-1"] == frozen


def test_process_event_replay_does_not_stamp_last_seen(hass, coordinator):
    """A replayed frame (is_replay=True) does not update last_seen."""
    key = "TestModel-1"
    # First, make the device known with a live event
    t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    with freeze_time(t0), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", "id": 1, "val": 5}'
        )
    seen_before = coordinator.last_seen[key]

    # Now feed the same frame again (at == mark -> already-seen replay)
    t1 = t0 + timedelta(seconds=5)
    with freeze_time(t1), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", "id": 1, "val": 5}'
        )

    # last_seen must not change on replay
    assert coordinator.last_seen[key] == seen_before


def test_process_event_replay_does_not_set_available_true(hass, coordinator):
    """A replayed frame does not flip an unavailable device back to available."""
    key = "TestModel-1"
    t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    with freeze_time(t0), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", "id": 1, "val": 5}'
        )

    # Watchdog marks unavailable
    with freeze_time(t0 + timedelta(seconds=700)), patch(DISPATCH):
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False

    # Replay frame (same time == mark)
    with freeze_time(t0 + timedelta(seconds=701)), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", "id": 1, "val": 5}'
        )

    # Still unavailable
    assert coordinator.available[key] is False


def test_process_event_sets_available_true_on_live(hass, coordinator):
    """A live event sets available[key] = True (exact value, not just truthy)."""
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", "id": 1, "val": 5}'
        )
    assert coordinator.available["TestModel-1"] is True


def test_process_event_device_fields_accumulate(hass, coordinator):
    """device_fields accumulates across multiple events for the same device."""
    key = "TestModel-1"
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", "id": 1, '
            '"temperature_C": 21.4}'
        )
    assert coordinator.device_fields[key] == {"temperature_C"}

    # Second event adds humidity
    with freeze_time("2026-05-25T10:00:10+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:10Z", "model": "TestModel", "id": 1, '
            '"humidity": 55}'
        )
    assert coordinator.device_fields[key] == {"temperature_C", "humidity"}


def test_process_event_seen_fields_union_across_devices(hass, coordinator):
    """seen_fields is a union across all devices (not per-device)."""
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "SensorA", "id": 1, '
            '"temperature_C": 21.4}'
        )
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "SensorB", "id": 2, '
            '"humidity": 55}'
        )
    assert coordinator.seen_fields >= {"temperature_C", "humidity"}


def test_process_event_no_discovery_callback_when_disabled(hass, coordinator):
    """New device callback does NOT fire when discovery_enabled=False."""
    seen = []
    coordinator.discovery_enabled = False
    coordinator.new_device_callback = lambda k, m: seen.append((k, m))

    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "NewDevice", "id": 1, "val": 5}'
        )

    assert seen == []


def test_process_event_no_callback_when_callback_is_none(hass, coordinator):
    """No exception when new_device_callback is None (the default)."""
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = None  # default

    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "NewDevice", "id": 1, "val": 5}'
        )
    # No error -> ok


def test_process_event_callback_exception_does_not_break_processing(hass, coordinator):
    """A throwing new_device_callback is caught; device is still added."""
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda k, m: (_ for _ in ()).throw(
        RuntimeError("boom")
    )

    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "NewDevice", "id": 1, "val": 5}'
        )

    # Device is still tracked despite callback error
    assert "NewDevice-1" in coordinator.devices


def test_process_event_dispatches_normalized_event_on_device_signal(hass, coordinator):
    """_process_event dispatches with the correct per-device signal."""
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Acurite-606TX", '
            '"id": 42, "temperature_C": 21.4}'
        )

    # Should dispatch on device-specific signal
    key = "Acurite-606TX-42"
    expected_signal = signal_device_update(coordinator.entry.entry_id, key)
    signals = [c.args[1] for c in dispatch.call_args_list]
    assert expected_signal in signals


def test_process_event_dispatch_carries_normalized_event(hass, coordinator):
    """The dispatched object is a NormalizedEvent with correct fields and model."""
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Acurite-606TX", '
            '"id": 42, "temperature_C": 21.4, "humidity": 55}'
        )

    # Find device-update call
    key = "Acurite-606TX-42"
    sig = signal_device_update(coordinator.entry.entry_id, key)
    dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
    assert len(dev_calls) == 1
    normalized = dev_calls[0].args[2]
    assert normalized.model == "Acurite-606TX"
    assert normalized.fields == {"temperature_C": 21.4, "humidity": 55}
    assert normalized.is_replay is False


def test_process_event_updates_devices_dict(hass, coordinator):
    """devices[key] is updated with the latest normalized event on each frame."""
    key = "Acurite-606TX-42"
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Acurite-606TX", '
            '"id": 42, "temperature_C": 21.4}'
        )
    assert coordinator.devices[key].fields == {"temperature_C": 21.4}

    with freeze_time("2026-05-25T10:00:10+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:10Z", "model": "Acurite-606TX", '
            '"id": 42, "temperature_C": 22.0}'
        )
    assert coordinator.devices[key].fields == {"temperature_C": 22.0}


def test_process_event_new_device_callback_fires_only_on_first(hass, coordinator):
    """Discovery callback fires only for new (previously unseen) devices."""
    seen: list[str] = []
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda k, m: seen.append(k)

    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
        )
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:01Z", "model": "Dev", "id": 1, "val": 2}'
        )
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:02Z", "model": "Dev", "id": 1, "val": 3}'
        )

    assert seen == ["Dev-1"]  # fired exactly once


# ---------------------------------------------------------------------------
# _dispatch: is_replay override
# ---------------------------------------------------------------------------


def test_dispatch_override_is_replay_false_on_replay_event(hass, coordinator):
    """Explicit is_replay=False overrides a cached replay-flagged event."""
    from custom_components.rtl_433.normalizer import NormalizedEvent

    # Build a NormalizedEvent that is flagged as a replay
    replay_event = NormalizedEvent(
        device_key="TestModel-1",
        model="TestModel",
        fields={"val": 5},
        is_replay=True,
    )

    with patch(DISPATCH) as dispatch:
        coordinator._dispatch("TestModel-1", replay_event, is_replay=False)

    # The dispatched event should have is_replay=False (override applied)
    dispatched = dispatch.call_args.args[2]
    assert dispatched.is_replay is False


def test_dispatch_override_is_replay_true_on_live_event(hass, coordinator):
    """Explicit is_replay=True overrides a live-flagged event."""
    from custom_components.rtl_433.normalizer import NormalizedEvent

    live_event = NormalizedEvent(
        device_key="TestModel-1",
        model="TestModel",
        fields={"val": 5},
        is_replay=False,
    )

    with patch(DISPATCH) as dispatch:
        coordinator._dispatch("TestModel-1", live_event, is_replay=True)

    dispatched = dispatch.call_args.args[2]
    assert dispatched.is_replay is True


def test_dispatch_no_override_honors_event_flag(hass, coordinator):
    """When is_replay=None (default), the event's own flag is used unchanged."""
    from custom_components.rtl_433.normalizer import NormalizedEvent

    replay_event = NormalizedEvent(
        device_key="TestModel-1",
        model="TestModel",
        fields={"val": 5},
        is_replay=True,
    )

    with patch(DISPATCH) as dispatch:
        coordinator._dispatch("TestModel-1", replay_event)  # no override

    dispatched = dispatch.call_args.args[2]
    assert dispatched.is_replay is True


def test_dispatch_override_same_flag_no_rebuild(hass, coordinator):
    """When the override matches the existing flag, no rebuild (same object)."""
    from custom_components.rtl_433.normalizer import NormalizedEvent

    live_event = NormalizedEvent(
        device_key="TestModel-1",
        model="TestModel",
        fields={"val": 5},
        is_replay=False,
    )

    with patch(DISPATCH) as dispatch:
        coordinator._dispatch("TestModel-1", live_event, is_replay=False)

    dispatched = dispatch.call_args.args[2]
    # Same object identity (no dataclasses.replace needed)
    assert dispatched is live_event


def test_dispatch_sends_correct_signal(hass, coordinator):
    """_dispatch sends on the per-device signal for this hub+device combo."""
    from custom_components.rtl_433.normalizer import NormalizedEvent

    event = NormalizedEvent(device_key="Dev-1", model="Dev", fields={"x": 1})
    key = "Dev-1"

    with patch(DISPATCH) as dispatch:
        coordinator._dispatch(key, event)

    assert dispatch.call_args.args[1] == signal_device_update(
        coordinator.entry.entry_id, key
    )


# ---------------------------------------------------------------------------
# Availability watchdog: boundary conditions
# ---------------------------------------------------------------------------


def test_watchdog_exactly_at_timeout_not_stale(hass, coordinator):
    """At exactly the timeout boundary, the device is NOT marked unavailable."""
    key = "TestModel-1"
    start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    with freeze_time(start), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", '
            '"id": 1, "temperature_C": 20.0}'
        )
    assert coordinator.available[key] is True

    # Exactly at 600s: (now - seen) == timedelta(seconds=600), NOT > -> not stale
    with freeze_time(start + timedelta(seconds=600)), patch(DISPATCH) as dispatch:
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

    assert coordinator.available[key] is True
    dispatch.assert_not_called()


def test_watchdog_one_second_past_timeout_is_stale(hass, coordinator):
    """At 601s (one past the 600s timeout), device IS marked unavailable."""
    key = "TestModel-1"
    start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    with freeze_time(start), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", '
            '"id": 1, "temperature_C": 20.0}'
        )

    with freeze_time(start + timedelta(seconds=601)), patch(DISPATCH) as dispatch:
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

    assert coordinator.available[key] is False
    dispatch.assert_called_once()


def test_watchdog_already_unavailable_no_redispatch(hass, coordinator):
    """A device already unavailable is not re-dispatched by the watchdog."""
    key = "TestModel-1"
    start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    with freeze_time(start), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", '
            '"id": 1, "temperature_C": 20.0}'
        )

    # First watchdog trip: flips to unavailable and dispatches once
    with freeze_time(start + timedelta(seconds=700)), patch(DISPATCH) as d1:
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False
    d1.assert_called_once()

    # Second watchdog trip: already unavailable, should NOT re-dispatch
    with freeze_time(start + timedelta(seconds=800)), patch(DISPATCH) as d2:
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False
    d2.assert_not_called()


def test_watchdog_dispatches_with_is_replay_false(hass, coordinator):
    """The watchdog re-dispatch always uses is_replay=False, never replay."""
    key = "TestModel-1"
    start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    with freeze_time(start), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "TestModel", '
            '"id": 1, "temperature_C": 20.0}'
        )

    with freeze_time(start + timedelta(seconds=700)), patch(DISPATCH) as dispatch:
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

    sig = signal_device_update(coordinator.entry.entry_id, key)
    dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
    assert len(dev_calls) == 1
    assert dev_calls[0].args[2].is_replay is False


def test_watchdog_default_available_true_for_no_prior_event(hass, coordinator):
    """available.get(key, True) defaults to True when no prior state -> if stale it flips."""
    # Manually inject last_seen without ever having set available
    key = "ManualDevice-1"
    t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    coordinator.last_seen[key] = t0
    # available[key] not set -> defaults to True in the watchdog

    with freeze_time(t0 + timedelta(seconds=700)), patch(DISPATCH):
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

    # Should have flipped to unavailable
    assert coordinator.available[key] is False


def test_watchdog_skips_devices_not_in_last_seen(hass, coordinator):
    """Devices without a last_seen entry are not evaluated by the watchdog."""
    # Put a device in 'devices' but NOT in last_seen
    from custom_components.rtl_433.normalizer import NormalizedEvent

    key = "Phantom-1"
    coordinator.devices[key] = NormalizedEvent(
        device_key=key, model="Phantom", fields={}
    )
    coordinator.available[key] = True
    # last_seen NOT set

    with freeze_time("2026-05-25T20:00:00+00:00"), patch(DISPATCH) as dispatch:
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))

    # Watchdog iterates last_seen, so Phantom-1 is not touched
    assert coordinator.available[key] is True
    dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# _effective_timeout
# ---------------------------------------------------------------------------


def test_effective_timeout_no_resolver_returns_hub_default(hass, coordinator):
    """Without a resolver, returns coordinator.availability_timeout exactly."""
    coordinator.effective_timeout_resolver = None
    assert coordinator._effective_timeout("any-key") == 600


def test_effective_timeout_resolver_called_with_correct_key(hass, coordinator):
    """The resolver is called with the exact device_key."""
    received = []

    def resolver(dk: str) -> int:
        received.append(dk)
        return 120

    coordinator.effective_timeout_resolver = resolver
    result = coordinator._effective_timeout("Acurite-606TX-42")
    assert received == ["Acurite-606TX-42"]
    assert result == 120


def test_effective_timeout_resolver_returns_exact_value(hass, coordinator):
    """The resolver's returned value is used verbatim, not the hub default."""
    coordinator.effective_timeout_resolver = lambda dk: 999
    assert coordinator._effective_timeout("anything") == 999


def test_effective_timeout_resolver_exception_returns_hub_default(hass, coordinator):
    """A resolver that raises falls back to hub default, does not re-raise."""

    def boom(_dk: str) -> int:
        raise ValueError("exploded")

    coordinator.effective_timeout_resolver = boom
    result = coordinator._effective_timeout("any")
    assert result == 600  # hub default, not 999


# ---------------------------------------------------------------------------
# Replay classification: boundary conditions
# ---------------------------------------------------------------------------


def test_high_water_mark_boundary_equal_is_replay(hass, coordinator):
    """event_time == high_water is classified as an already-seen replay."""
    # Set high-water mark explicitly
    t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    coordinator._event_high_water = t0

    # Feed a frame whose time == the mark
    with freeze_time("2026-05-25T10:00:05+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
        )

    assert _dispatched_replay_flags(dispatch) == [True]


def test_high_water_mark_boundary_one_second_newer_falls_through(hass, coordinator):
    """event_time == high_water + 1s is not caught by the already-seen branch."""
    t0 = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    coordinator._event_high_water = t0

    # 1s newer than mark, and recent enough -> live
    t_now = t0 + timedelta(seconds=5)
    with freeze_time(t_now), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:01Z", "model": "Dev", "id": 1, "val": 1}'
        )

    assert _dispatched_replay_flags(dispatch) == [False]


def test_stale_threshold_exactly_at_boundary_is_live(hass, coordinator):
    """An event exactly REPLAY_STALE_THRESHOLD old is treated as LIVE (boundary is >)."""
    # event_time = now - exactly 30s
    t_now = dt_util.parse_datetime("2026-05-25T10:00:30+00:00")
    event_time_str = "2026-05-25T10:00:00Z"

    with freeze_time(t_now), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            f'{{"time": "{event_time_str}", "model": "Dev", "id": 1, "val": 1}}'
        )

    # (now - event_time) == 30s == threshold -> NOT > threshold -> LIVE
    assert _dispatched_replay_flags(dispatch) == [False]


def test_stale_threshold_one_second_over_is_replay(hass, coordinator):
    """An event 31s old (> 30s threshold) is a stale gap event (replay)."""
    t_now = dt_util.parse_datetime("2026-05-25T10:00:31+00:00")
    event_time_str = "2026-05-25T10:00:00Z"

    with freeze_time(t_now), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            f'{{"time": "{event_time_str}", "model": "Dev", "id": 1, "val": 1}}'
        )

    # 31s > 30s -> stale -> replay
    assert _dispatched_replay_flags(dispatch) == [True]


def test_stale_gap_event_advances_high_water_mark(hass, coordinator):
    """A stale gap event advances the high-water mark to the event's own time."""
    t_now = dt_util.parse_datetime("2026-05-25T10:05:00+00:00")
    gap_time = dt_util.parse_datetime("2026-05-25T10:04:00+00:00")  # 60s ago -> stale

    with freeze_time(t_now), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:04:00Z", "model": "Dev", "id": 1, "val": 1}'
        )

    assert coordinator._event_high_water == gap_time


def test_live_event_advances_high_water_mark(hass, coordinator):
    """A live event advances the high-water mark to the event's own time."""
    t_now = dt_util.parse_datetime("2026-05-25T10:00:05+00:00")
    event_time = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")

    with freeze_time(t_now), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
        )

    assert coordinator._event_high_water == event_time


def test_no_timestamp_frame_treated_as_live(hass, coordinator):
    """A frame with no 'time' field is treated as live (never drop a real one)."""
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame('{"model": "Dev", "id": 1, "val": 1}')

    assert _dispatched_replay_flags(dispatch) == [False]
    assert coordinator.available.get("Dev-1") is True


def test_unparsable_time_treated_as_live(hass, coordinator):
    """A frame with a garbage 'time' field is treated as live."""
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "not-a-timestamp", "model": "Dev", "id": 1, "val": 1}'
        )

    assert _dispatched_replay_flags(dispatch) == [False]


def test_cold_start_no_high_water_recent_event_is_live(hass, coordinator):
    """On cold start (no high-water mark), a recent event is live."""
    assert coordinator._event_high_water is None
    t_now = dt_util.parse_datetime("2026-05-25T10:00:05+00:00")

    with freeze_time(t_now), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
        )

    assert _dispatched_replay_flags(dispatch) == [False]


def test_cold_start_no_high_water_old_event_is_stale_gap(hass, coordinator):
    """On cold start (no high-water mark), an old event is a stale gap event."""
    assert coordinator._event_high_water is None
    t_now = dt_util.parse_datetime("2026-05-25T10:30:00+00:00")

    with freeze_time(t_now), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, "val": 1}'
        )

    assert _dispatched_replay_flags(dispatch) == [True]


# ---------------------------------------------------------------------------
# _parse_event_time: additional coverage
# ---------------------------------------------------------------------------


def test_parse_event_time_fractional_seconds():
    """Local-naive format with fractional seconds parses successfully."""
    parsed = Rtl433Coordinator._parse_event_time("2026-05-25 10:00:00.5")
    assert parsed is not None
    assert parsed.tzinfo is not None  # aware UTC datetime


def test_parse_event_time_iso_z_suffix():
    """ISO-8601 with Z suffix parses to UTC."""
    parsed = Rtl433Coordinator._parse_event_time("2026-05-25T10:00:00Z")
    expected = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    assert parsed == expected


def test_parse_event_time_iso_offset():
    """ISO-8601 with numeric offset reduces to UTC."""
    parsed = Rtl433Coordinator._parse_event_time("2026-05-25T12:00:00+02:00")
    expected = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    assert parsed == expected


def test_parse_event_time_none_input():
    """None input returns None without raising."""
    assert Rtl433Coordinator._parse_event_time(None) is None


def test_parse_event_time_empty_string():
    """Empty string returns None."""
    assert Rtl433Coordinator._parse_event_time("") is None


def test_parse_event_time_whitespace_only():
    """Whitespace-only string returns None."""
    assert Rtl433Coordinator._parse_event_time("   ") is None


def test_parse_event_time_garbage():
    """Garbage string returns None without raising."""
    assert Rtl433Coordinator._parse_event_time("not-a-date") is None


def test_parse_event_time_int_input():
    """Non-string (int) returns None."""
    assert Rtl433Coordinator._parse_event_time(12345) is None


def test_parse_event_time_local_naive_returns_aware():
    """Local-naive format returns an aware UTC datetime."""
    parsed = Rtl433Coordinator._parse_event_time("2026-05-25 10:00:00")
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_event_time_local_naive_matches_dt_util():
    """Local-naive format is interpreted in HA's configured time zone."""
    text = "2026-05-25 10:00:00"
    parsed = Rtl433Coordinator._parse_event_time(text)
    expected = dt_util.as_utc(dt_util.parse_datetime("2026-05-25T10:00:00"))
    assert parsed == expected


# ---------------------------------------------------------------------------
# _handle_text_frame: frame parsing
# ---------------------------------------------------------------------------


def test_handle_text_frame_strips_whitespace(hass, coordinator):
    """Leading/trailing whitespace is stripped before JSON parsing."""
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame('  {"model": "Dev", "id": 1, "val": 1}  ')
    assert "Dev-1" in coordinator.devices
    dispatch.assert_called()


def test_handle_text_frame_null_string_like_input(hass, coordinator):
    """Non-string data that is falsy is handled gracefully (empty path)."""
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame("")  # empty string -> keep-alive
    dispatch.assert_not_called()
    assert coordinator.devices == {}


def test_handle_text_frame_list_json_is_dropped(hass, coordinator):
    """A JSON array (not an object) is logged and dropped."""
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame('[{"model": "Dev", "id": 1}]')
    dispatch.assert_not_called()
    assert coordinator.devices == {}


def test_handle_text_frame_json_number_is_dropped(hass, coordinator):
    """A JSON number is dropped (not an object)."""
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame("42")
    dispatch.assert_not_called()


def test_handle_text_frame_json_string_is_dropped(hass, coordinator):
    """A JSON string literal is dropped (not an object)."""
    with patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame('"hello"')
    dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Managed SDR: get_desired, is_managed, clear_desired_state
# ---------------------------------------------------------------------------


def test_get_desired_returns_none_when_not_set(hass, coordinator):
    """get_desired returns None for an unset key."""
    assert coordinator.get_desired("nonexistent") is None


def test_get_desired_returns_exact_value(hass, coordinator):
    """get_desired returns the exact stored value."""
    coordinator._desired["gain"] = 32.8
    assert coordinator.get_desired("gain") == 32.8


def test_get_desired_zero_value(hass, coordinator):
    """get_desired returns 0 (falsy but valid) correctly."""
    coordinator._desired["ppm_error"] = 0
    assert coordinator.get_desired("ppm_error") == 0


def test_is_managed_false_when_not_in_managed(hass, coordinator):
    """is_managed returns False for a field not in _managed."""
    assert coordinator.is_managed("gain") is False


def test_is_managed_true_when_in_managed(hass, coordinator):
    """is_managed returns True for a field in _managed."""
    coordinator._managed.add("gain")
    assert coordinator.is_managed("gain") is True


def test_is_managed_false_after_clear(hass, coordinator):
    """is_managed returns False after clear_desired_state."""
    coordinator._managed.add("gain")
    coordinator._desired["gain"] = 32.8
    _run(hass, coordinator.clear_desired_state())
    assert coordinator.is_managed("gain") is False


def test_clear_desired_state_empties_desired_and_managed(hass, coordinator):
    """clear_desired_state resets _desired and _managed to empty."""
    coordinator._desired = {"gain": 32.8, "ppm_error": 2}
    coordinator._managed = {"gain", "ppm_error"}
    _run(hass, coordinator.clear_desired_state())
    assert coordinator._desired == {}
    assert coordinator._managed == set()


# ---------------------------------------------------------------------------
# Managed SDR: async_load_desired_state
# ---------------------------------------------------------------------------


def test_load_desired_state_manage_off_clears(hass, coordinator):
    """When manage_settings=False, _desired and _managed are cleared."""
    coordinator.manage_settings = False
    coordinator._desired = {"gain": 32.8}
    coordinator._managed = {"gain"}

    _run(hass, coordinator.async_load_desired_state())

    assert coordinator._desired == {}
    assert coordinator._managed == set()


def test_load_desired_state_manage_on_empty_store(hass, hub_entry_builder):
    """When manage_settings=True and no store, _desired and _managed are empty."""
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local", manage_settings=True)

    _run(hass, coord.async_load_desired_state())

    assert coord._desired == {}
    assert coord._managed == set()


# ---------------------------------------------------------------------------
# Managed SDR: _command_args
# ---------------------------------------------------------------------------


def test_command_args_gain_db_returns_gain_command(hass, coordinator):
    """KEY_GAIN_DB resolves to the 'gain' command with the dB arg."""
    coordinator._desired = {KEY_GAIN_DB: 32.8, KEY_GAIN_AUTO: False}
    result = coordinator._command_args(KEY_GAIN_DB)
    assert result is not None
    command, val, arg = result
    assert command == "gain"
    assert val is None
    assert arg == "32.8"


def test_command_args_gain_auto_true_returns_empty_arg(hass, coordinator):
    """KEY_GAIN_AUTO with auto=True returns empty string arg."""
    coordinator._desired = {KEY_GAIN_AUTO: True}
    result = coordinator._command_args(KEY_GAIN_AUTO)
    assert result is not None
    command, val, arg = result
    assert command == "gain"
    assert val is None
    assert arg == ""


def test_command_args_gain_auto_false_uses_db_value(hass, coordinator):
    """KEY_GAIN_AUTO with auto=False uses the dB value for the arg."""
    coordinator._desired = {KEY_GAIN_DB: 40.0, KEY_GAIN_AUTO: False}
    result = coordinator._command_args(KEY_GAIN_AUTO)
    assert result is not None
    command, val, arg = result
    assert command == "gain"
    # gain_command_arg(40.0, False) -> "40"
    assert arg == "40"


def test_command_args_center_frequency(hass, coordinator):
    """KEY_CENTER_FREQUENCY resolves to center_frequency command with val."""
    coordinator._desired = {KEY_CENTER_FREQUENCY: 433920000}
    result = coordinator._command_args(KEY_CENTER_FREQUENCY)
    assert result is not None
    command, val, arg = result
    assert command == "center_frequency"
    assert val == 433920000
    assert arg is None


def test_command_args_sample_rate(hass, coordinator):
    """KEY_SAMPLE_RATE resolves to sample_rate command with val."""
    coordinator._desired = {KEY_SAMPLE_RATE: 250000}
    result = coordinator._command_args(KEY_SAMPLE_RATE)
    assert result is not None
    command, val, arg = result
    assert command == "sample_rate"
    assert val == 250000
    assert arg is None


def test_command_args_ppm_error(hass, coordinator):
    """KEY_PPM_ERROR resolves to ppm_error command with val."""
    coordinator._desired = {KEY_PPM_ERROR: 5}
    result = coordinator._command_args(KEY_PPM_ERROR)
    assert result is not None
    command, val, arg = result
    assert command == "ppm_error"
    assert val == 5
    assert arg is None


def test_command_args_conversion_mode(hass, coordinator):
    """KEY_CONVERSION_MODE resolves to convert command with val."""
    coordinator._desired = {KEY_CONVERSION_MODE: 1}
    result = coordinator._command_args(KEY_CONVERSION_MODE)
    assert result is not None
    command, val, arg = result
    assert command == "convert"
    assert val == 1
    assert arg is None


def test_command_args_hop_interval(hass, coordinator):
    """KEY_HOP_INTERVAL resolves to hop_interval command with val."""
    coordinator._desired = {KEY_HOP_INTERVAL: 600}
    result = coordinator._command_args(KEY_HOP_INTERVAL)
    assert result is not None
    command, val, arg = result
    assert command == "hop_interval"
    assert val == 600
    assert arg is None


def test_command_args_unknown_key_returns_none(hass, coordinator):
    """An unknown key returns None (no command to send)."""
    coordinator._desired = {"unknown_key": 42}
    assert coordinator._command_args("unknown_key") is None


def test_command_args_key_not_in_desired_returns_none(hass, coordinator):
    """A known key with no desired value returns None."""
    coordinator._desired = {}  # KEY_CENTER_FREQUENCY not set
    assert coordinator._command_args(KEY_CENTER_FREQUENCY) is None


# ---------------------------------------------------------------------------
# Managed SDR: set_sdr
# ---------------------------------------------------------------------------


def test_set_sdr_updates_desired_and_managed(hass, coordinator, aioclient_mock):
    """set_sdr stores the value in _desired and marks the field managed."""
    coordinator.connected = False
    # Mock store to avoid actual persistence
    with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
        _run(hass, coordinator.set_sdr(KEY_PPM_ERROR, 5))

    assert coordinator._desired[KEY_PPM_ERROR] == 5
    assert KEY_PPM_ERROR in coordinator._managed


def test_set_sdr_connected_triggers_enforce_and_refresh(
    hass, coordinator, aioclient_mock
):
    """set_sdr while connected sends the command and refreshes meta."""
    coordinator.connected = True
    _mock_cmd(aioclient_mock, ppm=5)
    # Also need to mock the ppm_error setter cmd
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "ppm_error", "val": "5"},
        status=200,
    )

    with (
        patch.object(coordinator._store, "async_save", new_callable=AsyncMock),
        patch(DISPATCH),
    ):
        _run(hass, coordinator.set_sdr(KEY_PPM_ERROR, 5))

    assert coordinator._desired[KEY_PPM_ERROR] == 5


def test_set_sdr_not_connected_no_enforce(hass, coordinator):
    """set_sdr while disconnected persists but does not call _enforce_field."""
    coordinator.connected = False
    enforce_called = []

    async def fake_enforce(field):
        enforce_called.append(field)

    coordinator._enforce_field = fake_enforce

    with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
        _run(hass, coordinator.set_sdr(KEY_PPM_ERROR, 3))

    assert enforce_called == []
    assert coordinator._desired[KEY_PPM_ERROR] == 3


# ---------------------------------------------------------------------------
# Managed SDR: _enforce_all
# ---------------------------------------------------------------------------


def test_enforce_all_sends_gain_once_for_gain_pair(hass, coordinator, aioclient_mock):
    """The gain pair (gain + gain_auto) emits exactly one gain command."""
    coordinator._desired = {KEY_GAIN_DB: 32.8, KEY_GAIN_AUTO: False}
    coordinator._managed = {KEY_GAIN_DB, KEY_GAIN_AUTO}

    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "gain", "arg": "32.8"},
        status=200,
    )

    _run(hass, coordinator._enforce_all())

    # Only one /cmd call for gain
    calls = aioclient_mock.mock_calls
    gain_calls = [c for c in calls if "gain" in str(c)]
    assert len(gain_calls) == 1


def test_enforce_all_skips_gain_keys_in_loop(hass, coordinator, aioclient_mock):
    """gain and gain_auto are not sent in the per-key loop, only at the end."""
    coordinator._desired = {
        KEY_GAIN_DB: 32.8,
        KEY_GAIN_AUTO: False,
        KEY_PPM_ERROR: 2,
    }
    coordinator._managed = {KEY_GAIN_DB, KEY_GAIN_AUTO, KEY_PPM_ERROR}

    # Register both possible calls
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "gain", "arg": "32.8"},
        status=200,
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "ppm_error", "val": "2"},
        status=200,
    )

    _run(hass, coordinator._enforce_all())

    # We expect 2 calls: ppm_error + gain (once at the end)
    assert len(aioclient_mock.mock_calls) == 2


def test_enforce_all_no_gain_managed_skips_gain_command(
    hass, coordinator, aioclient_mock
):
    """When gain keys are not managed, no gain command is sent at the end."""
    coordinator._desired = {KEY_PPM_ERROR: 2}
    coordinator._managed = {KEY_PPM_ERROR}

    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "ppm_error", "val": "2"},
        status=200,
    )

    _run(hass, coordinator._enforce_all())

    # Only the ppm_error call, no gain
    calls = aioclient_mock.mock_calls
    gain_calls = [c for c in calls if "gain" in str(c)]
    assert len(gain_calls) == 0


# ---------------------------------------------------------------------------
# _adopt_from_server: adoption logic
# ---------------------------------------------------------------------------


def test_adopt_from_server_empty_meta_is_noop(hass, coordinator):
    """When meta is empty, adoption does nothing (proxy may hide /cmd)."""
    coordinator.meta = {}
    coordinator._desired = {}
    coordinator._managed = set()

    with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
        _run(hass, coordinator._adopt_from_server())

    assert coordinator._desired == {}
    assert coordinator._managed == set()


def test_adopt_from_server_single_frequency_adopts_center_freq(hass, coordinator):
    """Single frequency: center_frequency IS adopted (not hopping)."""
    coordinator.meta = {
        "center_frequency": 433920000,
        "frequencies": [433920000],
        "samp_rate": 250000,
    }

    with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
        _run(hass, coordinator._adopt_from_server())

    assert coordinator._desired.get(KEY_CENTER_FREQUENCY) == 433920000
    assert KEY_CENTER_FREQUENCY in coordinator._managed


def test_adopt_from_server_multi_frequency_skips_center_freq(hass, coordinator):
    """Multiple frequencies (hopping): center_frequency is NOT adopted."""
    coordinator.meta = {
        "center_frequency": 433920000,
        "frequencies": [433920000, 868000000],
        "samp_rate": 250000,
    }

    with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
        _run(hass, coordinator._adopt_from_server())

    assert KEY_CENTER_FREQUENCY not in coordinator._desired
    assert KEY_CENTER_FREQUENCY not in coordinator._managed


def test_adopt_from_server_gain_auto_adopted(hass, coordinator):
    """Empty gain string -> gain_auto=True is adopted."""
    coordinator.meta = {"gain": ""}

    with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
        _run(hass, coordinator._adopt_from_server())

    assert coordinator._desired.get(KEY_GAIN_AUTO) is True
    assert KEY_GAIN_AUTO in coordinator._managed
    # gain_db should NOT be adopted when auto
    assert KEY_GAIN_DB not in coordinator._desired


def test_adopt_from_server_gain_db_adopted(hass, coordinator):
    """A gain string (non-empty) -> gain_auto=False and gain_db float are adopted."""
    coordinator.meta = {"gain": "32.8"}

    with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
        _run(hass, coordinator._adopt_from_server())

    assert coordinator._desired.get(KEY_GAIN_AUTO) is False
    assert coordinator._desired.get(KEY_GAIN_DB) == 32.8
    assert KEY_GAIN_AUTO in coordinator._managed
    assert KEY_GAIN_DB in coordinator._managed


def test_adopt_from_server_no_gain_key_skips_gain_pair(hass, coordinator):
    """When 'gain' is not in meta, neither gain key is adopted."""
    coordinator.meta = {"center_frequency": 433920000, "frequencies": [433920000]}

    with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
        _run(hass, coordinator._adopt_from_server())

    assert KEY_GAIN_AUTO not in coordinator._desired
    assert KEY_GAIN_DB not in coordinator._desired


def test_adopt_from_server_samp_rate_adopted(hass, coordinator):
    """samp_rate is adopted as KEY_SAMPLE_RATE."""
    coordinator.meta = {"samp_rate": 250000, "frequencies": [433920000]}

    with patch.object(coordinator._store, "async_save", new_callable=AsyncMock):
        _run(hass, coordinator._adopt_from_server())

    assert coordinator._desired.get(KEY_SAMPLE_RATE) == 250000
    assert KEY_SAMPLE_RATE in coordinator._managed


# ---------------------------------------------------------------------------
# _async_refresh_tick
# ---------------------------------------------------------------------------


def test_refresh_tick_connected_calls_both_refreshes(hass, coordinator, aioclient_mock):
    """The tick refreshes meta AND stats when connected."""
    _mock_cmd(aioclient_mock, gain="32.8", ppm=2)
    coordinator.connected = True

    with patch(DISPATCH):
        _run(hass, coordinator._async_refresh_tick(dt_util.utcnow()))

    assert coordinator.meta.get("center_frequency") == 433920000
    assert coordinator.stats.get("frames", {}).get("events") == 9


def test_refresh_tick_disconnected_noop(hass, coordinator, aioclient_mock):
    """The tick issues no HTTP calls when disconnected."""
    _mock_cmd(aioclient_mock)
    coordinator.connected = False

    with patch(DISPATCH):
        _run(hass, coordinator._async_refresh_tick(dt_util.utcnow()))

    assert coordinator.meta == {}
    assert coordinator.stats == {}
    assert aioclient_mock.mock_calls == []


# ---------------------------------------------------------------------------
# skip_keys: 'time' always excluded from fields
# ---------------------------------------------------------------------------


def test_time_key_never_in_fields(hass, coordinator):
    """The 'time' field is never present in the normalized event's fields."""
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Dev", "id": 1, '
            '"temperature_C": 21.4}'
        )

    key = "Dev-1"
    assert "time" not in coordinator.devices[key].fields
    assert "time" not in coordinator.seen_fields


def test_time_always_in_skip_keys(hass, hub_entry_builder):
    """The coordinator always adds 'time' to skip_keys, regardless of injection."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(
        hass,
        entry,
        host="rtl433.local",
        skip_keys=set(),  # inject empty set
    )
    assert "time" in coord.skip_keys


# ---------------------------------------------------------------------------
# Skip keys: custom vs default
# ---------------------------------------------------------------------------


def test_skip_keys_defaults_to_default_skip_keys(hass, hub_entry_builder):
    """When skip_keys=None is passed, DEFAULT_SKIP_KEYS is used."""
    from custom_components.rtl_433.normalizer import DEFAULT_SKIP_KEYS

    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local", skip_keys=None)
    # All DEFAULT_SKIP_KEYS should be in coord.skip_keys (plus 'time')
    for key in DEFAULT_SKIP_KEYS:
        assert key in coord.skip_keys


def test_skip_keys_custom_set_merged_with_time(hass, hub_entry_builder):
    """Custom skip_keys are stored correctly and 'time' is always added."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(
        hass, entry, host="rtl433.local", skip_keys={"model", "custom_skip"}
    )
    assert "custom_skip" in coord.skip_keys
    assert "time" in coord.skip_keys


# ---------------------------------------------------------------------------
# Connected flag transitions
# ---------------------------------------------------------------------------


def test_coordinator_initially_disconnected(hass, coordinator):
    """A fresh coordinator starts disconnected."""
    assert coordinator.connected is False


def test_connected_flag_exact_false_after_stop(hass, coordinator):
    """After async_stop, connected is exactly False (not just falsy)."""
    coordinator.connected = True
    _run(hass, coordinator.async_stop())
    assert coordinator.connected is False


# ---------------------------------------------------------------------------
# Multiple event types through the same dispatcher channel
# ---------------------------------------------------------------------------


def test_successive_events_same_device_dispatch_each_time(hass, coordinator):
    """Every event for a device (live or not) dispatches on the device signal."""
    key = "Acurite-606TX-42"
    sig = signal_device_update(coordinator.entry.entry_id, key)

    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Acurite-606TX", '
            '"id": 42, "temperature_C": 21.4}'
        )
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:05Z", "model": "Acurite-606TX", '
            '"id": 42, "temperature_C": 22.0}'
        )

    dev_calls = [c for c in dispatch.call_args_list if c.args[1] == sig]
    assert len(dev_calls) == 2


def test_two_devices_dispatch_on_separate_signals(hass, coordinator):
    """Two different devices dispatch on their own separate signals."""
    key1 = "Acurite-606TX-1"
    key2 = "Acurite-606TX-2"
    sig1 = signal_device_update(coordinator.entry.entry_id, key1)
    sig2 = signal_device_update(coordinator.entry.entry_id, key2)

    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Acurite-606TX", '
            '"id": 1, "temperature_C": 21.4}'
        )
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Acurite-606TX", '
            '"id": 2, "temperature_C": 22.0}'
        )

    signals_sent = [c.args[1] for c in dispatch.call_args_list]
    assert sig1 in signals_sent
    assert sig2 in signals_sent


# ---------------------------------------------------------------------------
# _send_cmd: returns True/False
# ---------------------------------------------------------------------------


def test_send_cmd_returns_true_on_success(hass, coordinator, aioclient_mock):
    """_send_cmd returns True when the HTTP call succeeds."""
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "ppm_error", "val": "5"},
        status=200,
    )
    result = _run(hass, coordinator._send_cmd("ppm_error", val=5))
    assert result is True


def test_send_cmd_returns_false_on_failure(hass, coordinator, aioclient_mock):
    """_send_cmd returns False when the HTTP call fails."""
    aioclient_mock.get("http://rtl433.local:8433/cmd", status=500)
    result = _run(hass, coordinator._send_cmd("ppm_error", val=5))
    assert result is False


def test_send_cmd_with_arg_sends_arg_param(hass, coordinator, aioclient_mock):
    """_send_cmd with arg='' sends the empty string (gain auto sentinel)."""
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "gain", "arg": ""}, status=200
    )
    result = _run(hass, coordinator._send_cmd("gain", arg=""))
    assert result is True


def test_send_cmd_with_val_stringifies_int(hass, coordinator, aioclient_mock):
    """_send_cmd sends val as a string-coerced integer on the query string."""
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "center_frequency", "val": "433920000"},
        status=200,
    )
    result = _run(hass, coordinator._send_cmd("center_frequency", val=433920000))
    assert result is True


# ---------------------------------------------------------------------------
# _fetch_cmd: failure handling
# ---------------------------------------------------------------------------


def test_fetch_cmd_returns_none_on_http_error(hass, coordinator, aioclient_mock):
    """_fetch_cmd returns None when the server returns an HTTP error."""
    aioclient_mock.get("http://rtl433.local:8433/cmd", status=503)
    result = _run(hass, coordinator._fetch_cmd("get_meta"))
    assert result is None


def test_fetch_cmd_returns_parsed_json(hass, coordinator, aioclient_mock):
    """_fetch_cmd returns the parsed JSON body on success."""
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_meta"},
        json={"result": {"center_frequency": 433920000}},
    )
    result = _run(hass, coordinator._fetch_cmd("get_meta"))
    assert result == {"result": {"center_frequency": 433920000}}


# ---------------------------------------------------------------------------
# device_removers: callback list
# ---------------------------------------------------------------------------


def test_device_removers_initially_empty(hass, coordinator):
    """device_removers starts as an empty list."""
    assert coordinator.device_removers == []


def test_device_removers_can_be_appended(hass, coordinator):
    """Removal callbacks can be appended to device_removers."""
    called = []
    coordinator.device_removers.append(lambda dk: called.append(dk))
    coordinator.device_removers[0]("Dev-1")
    assert called == ["Dev-1"]


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_state_all_dicts_empty(hass, coordinator):
    """A freshly constructed coordinator has empty runtime state."""
    assert coordinator.devices == {}
    assert coordinator.last_seen == {}
    assert coordinator.available == {}
    assert coordinator.seen_fields == set()
    assert coordinator.device_fields == {}
    assert coordinator.meta == {}
    assert coordinator.stats == {}
    assert coordinator._desired == {}
    assert coordinator._managed == set()
    assert coordinator._event_high_water is None


def test_initial_connected_false(hass, coordinator):
    """A freshly constructed coordinator is not connected."""
    assert coordinator.connected is False


def test_initial_manage_settings_true(hass, hub_entry_builder):
    """The default manage_settings=True is set correctly."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
    assert coord.manage_settings is True


def test_initial_discovery_enabled(hass, hub_entry_builder):
    """The default discovery_enabled=True is set correctly."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    coord = Rtl433Coordinator(hass, entry, host="rtl433.local")
    assert coord.discovery_enabled is True


# ---------------------------------------------------------------------------
# CannotConnect exception
# ---------------------------------------------------------------------------


def test_cannot_connect_is_homeassistant_error():
    """CannotConnect is a HomeAssistantError subclass."""
    from homeassistant.exceptions import HomeAssistantError

    exc = CannotConnect("test message")
    assert isinstance(exc, HomeAssistantError)


def test_cannot_connect_carries_message():
    """CannotConnect preserves the error message."""
    exc = CannotConnect("Cannot connect to ws://host:8433/ws: timeout")
    assert "Cannot connect" in str(exc)


# ---------------------------------------------------------------------------
# _refresh_meta: meta key selection (only allowed keys pass through)
# ---------------------------------------------------------------------------


def test_refresh_meta_only_known_keys_extracted(hass, coordinator, aioclient_mock):
    """Unknown keys in the meta response are NOT carried into coordinator.meta."""
    meta = {
        "result": {
            "center_frequency": 433920000,
            "samp_rate": 250000,
            "conversion_mode": 0,
            "frequencies": [433920000],
            "hop_times": [600],
            # Extra unknown keys that should be filtered out
            "unknown_key_xyz": "should_not_appear",
            "duration": 0,
            "stats_interval": 0,
        }
    }
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_meta"}, json=meta
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_gain"},
        json={"result": None},
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_ppm_error"},
        json={"result": None},
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    assert "unknown_key_xyz" not in coordinator.meta
    assert "duration" not in coordinator.meta
    assert "stats_interval" not in coordinator.meta
    assert coordinator.meta["center_frequency"] == 433920000
    assert coordinator.meta["samp_rate"] == 250000


def test_refresh_meta_exact_extracted_keys(hass, coordinator, aioclient_mock):
    """All five allowed meta keys are extracted when present."""
    meta = {
        "result": {
            "center_frequency": 433920000,
            "samp_rate": 250000,
            "conversion_mode": 0,
            "frequencies": [433920000, 868000000],
            "hop_times": [600, 30],
        }
    }
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd", params={"cmd": "get_meta"}, json=meta
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_gain"},
        json={"result": "32.8"},
    )
    aioclient_mock.get(
        "http://rtl433.local:8433/cmd",
        params={"cmd": "get_ppm_error"},
        json={"result": 2},
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    assert coordinator.meta["center_frequency"] == 433920000
    assert coordinator.meta["samp_rate"] == 250000
    assert coordinator.meta["conversion_mode"] == 0
    assert coordinator.meta["frequencies"] == [433920000, 868000000]
    assert coordinator.meta["hop_times"] == [600, 30]
    assert coordinator.meta["hop_interval"] == 600
    assert coordinator.meta["gain"] == "32.8"
    assert coordinator.meta["ppm_error"] == 2


# ---------------------------------------------------------------------------
# Availability recovery log path: was_available is False
# ---------------------------------------------------------------------------


def test_available_recovery_logged_when_was_unavailable(hass, coordinator, caplog):
    """A live event after unavailability emits the 'back online' debug log."""
    import logging

    key = "Acurite-606TX-42"
    start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")

    with freeze_time(start), patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Acurite-606TX", '
            '"id": 42, "temperature_C": 21.4}'
        )

    # Force unavailable
    with freeze_time(start + timedelta(seconds=700)), patch(DISPATCH):
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False

    # Fresh live event brings it back
    fresh = start + timedelta(seconds=800)
    with (
        freeze_time(fresh),
        patch(DISPATCH),
        caplog.at_level(logging.DEBUG, logger="custom_components.rtl_433"),
    ):
        coordinator._handle_text_frame(
            f'{{"time": "{fresh.strftime("%Y-%m-%dT%H:%M:%SZ")}", '
            '"model": "Acurite-606TX", "id": 42, "temperature_C": 22.0}'
        )

    assert coordinator.available[key] is True
    assert any("back online" in r.message for r in caplog.records)
