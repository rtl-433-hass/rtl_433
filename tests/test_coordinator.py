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
from custom_components.rtl_433.coordinator.base import _build_cmd_url
from homeassistant.util import dt as dt_util

DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"

# The shape the rtl_433 HTTP ``/cmd`` dispatcher actually returns: its responder
# (``rpc_response_jsoncmd``) wraps *every* getter reply in a ``{"result": ...}``
# envelope -- the JSON-payload getters (``get_meta``/``get_stats``) just as much
# as the scalar getters (``get_gain``/``get_ppm_error``). Only the WebSocket
# framing sends ``get_meta``/``get_stats`` as a bare object; this integration
# uses ``/cmd``, so the coordinator must unwrap ``result`` for all of them.
_META_RESULT = {
    "center_frequency": 433920000,
    "samp_rate": 250000,
    "conversion_mode": 0,
    "frequencies": [433920000, 868000000],
    "hop_times": [600, 30],
    "duration": 0,
    "stats_interval": 0,
}
_STATS_RESULT = {
    "enabled": 5,
    "since": "2026-05-26T10:00:00",
    "frames": {"count": 3, "fsk": 1, "events": 9},
    "stats": [],
}
_META_BODY = {"result": _META_RESULT}
_STATS_BODY = {"result": _STATS_RESULT}


def _mock_cmd(aioclient_mock, *, gain="32.8", ppm=2):
    """Register per-command ``/cmd`` GET stubs keyed by the ``cmd`` query param.

    The mocker matches a registered response when every query component in the
    matcher is present in the request, so a distinct ``params={"cmd": ...}`` per
    getter routes each request to its own body even though all share one URL.
    """
    url = "http://rtl433.local:8433/cmd"
    aioclient_mock.get(url, params={"cmd": "get_meta"}, json=_META_BODY)
    aioclient_mock.get(url, params={"cmd": "get_gain"}, json={"result": gain})
    aioclient_mock.get(url, params={"cmd": "get_ppm_error"}, json={"result": ppm})
    aioclient_mock.get(url, params={"cmd": "get_stats"}, json=_STATS_BODY)


def _run(hass, coro):
    """Drive an async coordinator method to completion on the hass loop."""
    return hass.loop.run_until_complete(coro)


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
    # Freeze ``now`` at the frame's ``time`` so the event is recent (live), not a
    # stale gap event: the replay classifier ages each timed frame against now.
    with freeze_time("2026-05-25 10:00:00"), patch(DISPATCH) as dispatch:
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


def test_mixed_frame_sequence_classifies_correctly(hass, coordinator):
    """Plan Success Criterion #1 / Self-Validation #3 as one sequence.

    Feeding a meta object, a stats frame, an RPC result frame, a shutdown frame,
    a model-less identity event, and one real model event leaves exactly the two
    real devices (neither keyed ``"unknown"``) and a ``seen_fields`` containing
    only those two events' measurement fields — no SDR/meta field-name pollution.
    """
    coordinator.connected = True
    with patch(DISPATCH):
        coordinator._handle_text_frame(
            '{"center_frequency": 433920000, "samp_rate": 250000, '
            '"frequencies": [433920000], "hop_times": [600]}'
        )  # meta -> ignored
        coordinator._handle_text_frame(
            '{"enabled": 5, "frames": {"count": 3, "fsk": 1, "events": 9}}'
        )  # stats -> ignored
        coordinator._handle_text_frame('{"result": "ok"}')  # RPC -> ignored
        coordinator._handle_text_frame('{"shutdown": "goodbye"}')  # -> connectivity
        coordinator._handle_text_frame(
            '{"channel": 1, "temperature_C": 5}'
        )  # model-less identity event
        coordinator._handle_text_frame(
            '{"model": "Acurite-606TX", "id": 42, '
            '"temperature_C": 21.4, "humidity": 55}'
        )  # real model event

    # Exactly the two real devices; neither is the phantom "unknown" key.
    assert set(coordinator.devices) == {"unknown-ch1", "Acurite-606TX-42"}
    assert "unknown" not in coordinator.devices
    # seen_fields carries only the two events' measurement fields — no meta names
    # such as frequencies / samp_rate / center_frequency leaked in.
    assert coordinator.seen_fields == {"temperature_C", "humidity"}


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


# --------------------------------------------------------------------------- #
# HTTP /cmd getters                                                           #
# --------------------------------------------------------------------------- #
def test_cmd_url_ignores_ws_path():
    """The /cmd URL is the server root, never derived from the WS path."""
    host, port = "rtl433.local", 8433
    # A proxy-style WS path must not leak into the /cmd URL.
    assert _build_cmd_url(host, port, secure=False) == "http://rtl433.local:8433/cmd"
    assert _build_cmd_url(host, port, secure=True) == "https://rtl433.local:8433/cmd"


def test_refresh_meta_populates_state(hass, coordinator, aioclient_mock):
    """get_meta + get_gain + get_ppm_error assemble coordinator.meta and emit."""
    _mock_cmd(aioclient_mock, gain="32.8", ppm=2)

    with patch(DISPATCH) as dispatch:
        _run(hass, coordinator._refresh_meta())

    meta = coordinator.meta
    assert meta["center_frequency"] == 433920000
    assert meta["samp_rate"] == 250000
    assert meta["conversion_mode"] == 0
    assert meta["frequencies"] == [433920000, 868000000]
    assert meta["hop_times"] == [600, 30]
    assert meta["hop_interval"] == 600  # derived = hop_times[0]
    assert meta["gain"] == "32.8"
    assert meta["ppm_error"] == 2

    # A populated refresh emits the hub-update signal.
    hub_signal = signal_hub_update(coordinator.entry.entry_id)
    sent = [call.args[1] for call in dispatch.call_args_list]
    assert hub_signal in sent


def test_refresh_meta_empty_gain_preserved(hass, coordinator, aioclient_mock):
    """An empty gain string is preserved verbatim (rendered as 'auto' later)."""
    _mock_cmd(aioclient_mock, gain="")

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_meta())

    assert coordinator.meta["gain"] == ""


def test_refresh_stats_populates_state(hass, coordinator, aioclient_mock):
    """get_stats populates coordinator.stats and emits the hub-update signal."""
    _mock_cmd(aioclient_mock)

    with patch(DISPATCH) as dispatch:
        _run(hass, coordinator._refresh_stats())

    assert coordinator.stats["frames"]["events"] == 9
    assert coordinator.stats["frames"]["count"] == 3
    assert coordinator.stats["frames"]["fsk"] == 1

    hub_signal = signal_hub_update(coordinator.entry.entry_id)
    sent = [call.args[1] for call in dispatch.call_args_list]
    assert hub_signal in sent


def test_getter_failure_leaves_values_intact(hass, coordinator, aioclient_mock):
    """A failing /cmd leaves prior meta/stats intact and never flips connected."""
    coordinator.meta = {"gain": "32.8"}
    coordinator.stats = {"frames": {"events": 1}}
    coordinator.connected = True

    # Every getter 500s; raise_for_status() turns this into a swallowed failure.
    aioclient_mock.get("http://rtl433.local:8433/cmd", status=500)

    with patch(DISPATCH) as dispatch:
        _run(hass, coordinator._refresh_meta())
        _run(hass, coordinator._refresh_stats())

    assert coordinator.meta == {"gain": "32.8"}
    assert coordinator.stats == {"frames": {"events": 1}}
    assert coordinator.connected is True
    # No successful getter -> no hub-update emitted.
    dispatch.assert_not_called()


def test_refresh_tick_refreshes_meta_and_stats_while_connected(
    hass, coordinator, aioclient_mock
):
    """The periodic tick re-polls meta + stats so the actual sensors converge.

    Without this, meta was only read on connect / right after a write, so a
    change made on the server (or a post-write read-back that raced the retune)
    left the hub's actual SDR sensors stale until the next reconnect.
    """
    _mock_cmd(aioclient_mock, gain="40", ppm=7)
    coordinator.connected = True

    with patch(DISPATCH):
        _run(hass, coordinator._async_refresh_tick(dt_util.utcnow()))

    # Meta was re-polled (not just stats).
    assert coordinator.meta["center_frequency"] == 433920000
    assert coordinator.meta["gain"] == "40"
    assert coordinator.meta["ppm_error"] == 7
    assert coordinator.stats["frames"]["events"] == 9


def test_refresh_tick_noop_while_disconnected(hass, coordinator, aioclient_mock):
    """The tick issues no HTTP and changes nothing when not connected."""
    _mock_cmd(aioclient_mock)
    coordinator.connected = False

    with patch(DISPATCH):
        _run(hass, coordinator._async_refresh_tick(dt_util.utcnow()))

    assert coordinator.meta == {}
    assert coordinator.stats == {}
    assert aioclient_mock.mock_calls == []
