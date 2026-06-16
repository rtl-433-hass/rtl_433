"""Tests for the rtl_433 push coordinator's frame handling and watchdog.

We never open a real socket: frames are fed straight into the coordinator's
``_handle_text_frame`` (the same method ``_read_frames`` calls for TEXT frames),
and the availability watchdog is invoked directly with time advanced via
freezegun. The dispatcher send is patched so we can assert fan-out without
entities.
"""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from unittest.mock import Mock, patch

from freezegun import freeze_time
import pytest

from custom_components.rtl_433.const import signal_device_update, signal_hub_update
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.coordinator.base import (
    REPLAY_STALE_THRESHOLD,
    _build_cmd_url,
)
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
    seen: list[tuple[str, str, bool]] = []
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda key, model, is_replay: seen.append(
        (key, model, is_replay)
    )

    frame = '{"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4}'
    with patch(DISPATCH):
        coordinator._handle_text_frame(frame)
        coordinator._handle_text_frame(frame)  # second sighting: no new callback

    # A live frame (no usable ``time``) is not a replay.
    assert seen == [("Acurite-606TX-42", "Acurite-606TX", False)]


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

    # Past the timeout: watchdog flips to unavailable and re-dispatches the cached
    # frame as an availability re-paint (``is_repaint=True``, ``is_replay=False``)
    # so measurement entities re-read availability while ``Rtl433Event`` skips it.
    with (
        freeze_time(start + timedelta(seconds=601)),
        patch(DISPATCH) as dispatch,
    ):
        hass.loop.run_until_complete(coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False
    dispatch.assert_called_once()
    repaint = dispatch.call_args.args[2]
    assert repaint.is_repaint is True
    assert repaint.is_replay is False

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


def test_refresh_stats_malformed_json_logs_error_once(
    hass, coordinator, aioclient_mock, caplog
):
    """A reachable /cmd returning invalid JSON logs an error once, not per tick.

    This is the rtl_433 server-side truncation case (an oversized ``get_stats``
    overflows the server's output buffer and emits corrupt JSON): the endpoint
    answers 200, the body fails to parse, and the stats sensors stay "unknown".
    The error must surface (unlike a hidden ``/cmd``, which stays at debug) but
    must not flood the log across the 60s refresh ticks.
    """
    url = "http://rtl433.local:8433/cmd"
    aioclient_mock.get(url, params={"cmd": "get_stats"}, text='{"frames":')

    with caplog.at_level(logging.ERROR), patch(DISPATCH):
        _run(hass, coordinator._refresh_stats())
        _run(hass, coordinator._refresh_stats())

    malformed = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "malformed JSON" in r.getMessage()
    ]
    assert len(malformed) == 1
    assert "get_stats" in malformed[0].getMessage()
    # The body never parses, so stats is left empty rather than half-populated.
    assert coordinator.stats == {}


def test_getter_clears_malformed_flag_on_recovery(hass, coordinator, aioclient_mock):
    """A subsequent valid response clears the once-logged flag so it can re-warn."""
    _mock_cmd(aioclient_mock)
    coordinator._malformed_cmds.add("get_stats")

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_stats())

    assert "get_stats" not in coordinator._malformed_cmds
    assert coordinator.stats["frames"]["events"] == 9


# --------------------------------------------------------------------------- #
# SDR device identity (get_dev_info / get_dev_query)                           #
# --------------------------------------------------------------------------- #
_DEV_INFO_RESULT = {
    "vendor": "Realtek",
    "product": "RTL2838UHIDIR",
    "serial": "00000001",
}


def _mock_dev_info(aioclient_mock, *, info=_DEV_INFO_RESULT, query=":00000001"):
    """Register ``/cmd`` stubs for the device-identity getters.

    Both are wrapped in the ``result`` envelope, mirroring the real ``/cmd``
    responder. ``info`` may be a dict (the embedded object ``/cmd`` returns) or a
    str (a JSON string, as the WS framing / a proxy might deliver it).
    """
    url = "http://rtl433.local:8433/cmd"
    aioclient_mock.get(url, params={"cmd": "get_dev_info"}, json={"result": info})
    aioclient_mock.get(url, params={"cmd": "get_dev_query"}, json={"result": query})


def test_refresh_dev_info_populates_identity(hass, coordinator, aioclient_mock):
    """get_dev_info/get_dev_query populate the identity and fire the callback."""
    _mock_dev_info(aioclient_mock)
    info_callback = Mock()
    coordinator.hub_info_callback = info_callback

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_dev_info())

    assert coordinator.dev_info == {
        "vendor": "Realtek",
        "product": "RTL2838UHIDIR",
        "serial": "00000001",
    }
    assert coordinator.dev_query == ":00000001"
    info_callback.assert_called_once_with()


def test_refresh_dev_info_parses_json_string(hass, coordinator, aioclient_mock):
    """A ``get_dev_info`` result delivered as a JSON string is parsed to a dict."""
    _mock_dev_info(
        aioclient_mock,
        info='{"vendor": "Realtek", "product": "RTL2832U", "serial": "abc"}',
    )

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_dev_info())

    assert coordinator.dev_info["product"] == "RTL2832U"
    assert coordinator.dev_info["serial"] == "abc"


def test_refresh_dev_info_unparsable_string_left_unchanged(
    hass, coordinator, aioclient_mock
):
    """A non-JSON ``get_dev_info`` string leaves ``dev_info`` untouched."""
    _mock_dev_info(aioclient_mock, info="not json", query=":42")
    info_callback = Mock()
    coordinator.hub_info_callback = info_callback

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_dev_info())

    # The unparsable info is ignored, but the query still updates -> callback.
    assert coordinator.dev_info == {}
    assert coordinator.dev_query == ":42"
    info_callback.assert_called_once_with()


def test_refresh_dev_info_empty_preserves_and_skips_callback(
    hass, coordinator, aioclient_mock
):
    """An empty identity (no SDR open) leaves prior values and fires no callback."""
    coordinator.dev_info = {"vendor": "Realtek", "product": "RTL2838UHIDIR"}
    coordinator.dev_query = ":00000001"
    info_callback = Mock()
    coordinator.hub_info_callback = info_callback
    url = "http://rtl433.local:8433/cmd"
    aioclient_mock.get(url, params={"cmd": "get_dev_info"}, json={"result": ""})
    aioclient_mock.get(url, params={"cmd": "get_dev_query"}, json={"result": ""})

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_dev_info())

    assert coordinator.dev_info == {"vendor": "Realtek", "product": "RTL2838UHIDIR"}
    assert coordinator.dev_query == ":00000001"
    info_callback.assert_not_called()


def test_refresh_dev_info_callback_only_on_change(hass, coordinator, aioclient_mock):
    """An unchanged identity on reconnect does not re-fire the callback."""
    _mock_dev_info(aioclient_mock)
    info_callback = Mock()
    coordinator.hub_info_callback = info_callback

    with patch(DISPATCH):
        _run(hass, coordinator._refresh_dev_info())
        _run(hass, coordinator._refresh_dev_info())

    info_callback.assert_called_once_with()


def test_refresh_dev_info_callback_error_is_swallowed(
    hass, coordinator, aioclient_mock
):
    """A raising callback can never break the connect loop / streaming."""
    _mock_dev_info(aioclient_mock)
    coordinator.hub_info_callback = Mock(side_effect=RuntimeError("registry boom"))

    with patch(DISPATCH):
        # Must not raise despite the callback blowing up.
        _run(hass, coordinator._refresh_dev_info())

    assert coordinator.dev_info["product"] == "RTL2838UHIDIR"


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


# --------------------------------------------------------------------------- #
# Replay suppression: timestamp parsing + frame classification.                #
# --------------------------------------------------------------------------- #
# A doorbell event device (``secret_knock`` maps to an ``event`` platform entity)
# used by the classification tests; the coordinator reads raw ``time`` for the
# replay decision, so each frame controls its own ``time`` value.
_DOORBELL_KEY = "Honeywell-Doorbell-7"


def _doorbell_frame(event_time: str, *, value: int = 1) -> str:
    """Build a doorbell event frame string with the given ``time`` value."""
    payload = {"model": "Honeywell-Doorbell", "id": 7, "secret_knock": value}
    if event_time is not None:
        payload["time"] = event_time
    return json.dumps(payload)


def _dispatched_replay_flags(dispatch) -> list[bool]:
    """Pull the ``is_replay`` flag off each per-device dispatched event."""
    return [
        call.args[2].is_replay
        for call in dispatch.call_args_list
        if call.args[1].startswith("rtl_433_device_update")
    ]


def test_parse_event_time_handles_format_variance():
    """Parsing tolerates local-naive, ISO/``Z``, and offset; junk yields None.

    The unit-level guarantee behind the classifier: a usable timestamp parses to a
    comparable UTC instant regardless of format, and a missing/blank/garbage value
    yields ``None`` (so the frame is later treated as live) and never raises.
    """
    parse = Rtl433Coordinator._parse_event_time

    # Local naive "YYYY-MM-DD HH:MM:SS" -> interpreted in HA's configured time zone
    # and reduced to UTC. Compare against the same tz-aware reduction so the test
    # is robust to whatever DEFAULT_TIME_ZONE the harness set (HA test default is
    # US/Pacific, not UTC).
    naive = parse("2026-05-25 10:00:00")
    assert naive is not None
    assert naive.tzinfo is not None  # reduced to an aware (UTC) instant
    expected_naive = dt_util.as_utc(dt_util.parse_datetime("2026-05-25T10:00:00"))
    assert naive == expected_naive
    # Optional fractional seconds parse too.
    assert parse("2026-05-25 10:00:00.5") is not None

    # ISO-8601 with a ``Z`` suffix is unambiguous UTC regardless of HA's tz.
    assert parse("2026-05-25T10:00:00Z") == dt_util.parse_datetime(
        "2026-05-25T10:00:00+00:00"
    )
    # ISO-8601 with a numeric offset reduces to the same UTC basis.
    assert parse("2026-05-25T12:00:00+02:00") == dt_util.parse_datetime(
        "2026-05-25T10:00:00+00:00"
    )

    # Missing / blank / garbage -> None, never raising.
    assert parse(None) is None
    assert parse("") is None
    assert parse("   ") is None
    assert parse("not a timestamp") is None
    assert parse(12345) is None  # non-string


def test_classifies_local_iso_and_unparsable_times(hass, coordinator):
    """A local, an ISO/``Z``, and an unparsable frame each classify as expected.

    Drives the classifier through ``_handle_text_frame`` with ``now`` frozen so
    age is deterministic: a recent local-naive time and a recent ISO/``Z`` time
    are LIVE; a blank/garbage ``time`` is treated as LIVE ("never drop a real
    one") and the frame loop never raises.
    """
    # A local-naive frame is interpreted in HA's configured tz; freeze ``now`` a
    # few seconds after the frame's true UTC instant so its age stays under the
    # threshold regardless of that tz (HA test default is US/Pacific, not UTC).
    local_frame_utc = dt_util.as_utc(dt_util.parse_datetime("2026-05-25T10:00:00"))
    with freeze_time(local_frame_utc + timedelta(seconds=10)), patch(DISPATCH) as d1:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25 10:00:00"))
    assert _dispatched_replay_flags(d1) == [False]  # recent local time -> live

    # An ISO/``Z`` (unambiguous UTC) frame; freeze at a matching recent instant.
    # Use a strictly newer instant than the prior mark so the mark does not gate.
    iso_now = dt_util.parse_datetime("2026-05-25T20:00:10+00:00")
    with freeze_time(iso_now), patch(DISPATCH) as d2:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T20:00:05Z"))
        # Blank ``time`` -> unparsable -> treated as live, never raises.
        coordinator._handle_text_frame(_doorbell_frame(""))
        # Garbage ``time`` -> unparsable -> live, never raises.
        coordinator._handle_text_frame(_doorbell_frame("garbage"))
    assert _dispatched_replay_flags(d2) == [False, False, False]


def test_gap_event_after_reconnect_is_suppressed(hass, coordinator):
    """A reconnect replay frame newer than the mark but stale is a gap event.

    Feed a live event (advances the high-water mark), then — simulating a
    reconnect — feed a frame whose ``time`` is newer than the mark (HA never saw
    it) but older than ``REPLAY_STALE_THRESHOLD``. It must classify as a replay
    (``is_replay=True``) so the event entity does not fire, while still advancing
    the mark so it is not reconsidered.
    """
    # Use unambiguous UTC (``Z``) timestamps so age is tz-independent.
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:00Z"))
    assert _dispatched_replay_flags(dispatch) == [False]

    # Reconnect: a gap event stamped at 10:01:00 arrives at 10:05:00 -> newer than
    # the 10:00:00 mark but 240s old, comfortably beyond the staleness threshold
    # -> stale gap event -> suppressed.
    assert timedelta(seconds=240) > REPLAY_STALE_THRESHOLD
    with freeze_time("2026-05-25T10:05:00+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:01:00Z"))
    assert _dispatched_replay_flags(dispatch) == [True]
    # The mark advanced to the gap event's time so it is not reconsidered.
    assert coordinator._event_high_water == dt_util.parse_datetime(
        "2026-05-25T10:01:00+00:00"
    )


def test_no_double_fire_on_blip_replay(hass, coordinator):
    """Replaying the SAME frame (``time <= mark``) is an already-seen replay.

    A brief blip re-sends the recently-fired buffer tail on reconnect; the
    high-water mark recognises those frames as already-seen so they do not
    re-fire (``is_replay=True``).
    """
    frame = _doorbell_frame("2026-05-25T10:00:00Z")
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(frame)  # live -> fires
    assert _dispatched_replay_flags(dispatch) == [False]

    # Same frame re-sent on reconnect (time == mark) -> already-seen replay.
    with freeze_time("2026-05-25T10:00:02+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(frame)
    assert _dispatched_replay_flags(dispatch) == [True]


def test_recent_backlog_event_on_restart_is_suppressed(hass, coordinator):
    """A recent doorbell press replayed on connect (cold start) does not re-fire.

    The HA-restart re-delivery case: after a restart the high-water mark is unset
    and the server replays its buffer, so a doorbell pressed only seconds before
    the restart is *recent* (well inside ``REPLAY_STALE_THRESHOLD``) yet predates
    the new connection. The connection-time backlog gate must classify it as a
    replay (``is_replay=True``) so the event entity does not re-fire it — without
    the gate the age test alone would have treated it as a live transmission.
    """
    # Connected at 10:00:10; a press at 10:00:00 is recent (age 11s < 30s) but a
    # clear 10s before the connection (outside the 5s skew grace), so it is
    # replayed backlog and must be suppressed.
    coordinator._connection_time = dt_util.parse_datetime("2026-05-25T10:00:10+00:00")
    assert timedelta(seconds=11) < REPLAY_STALE_THRESHOLD
    with freeze_time("2026-05-25T10:00:11+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:00Z"))
    assert _dispatched_replay_flags(dispatch) == [True]

    # A genuine live press AFTER the connection still fires (never suppressed).
    with freeze_time("2026-05-25T10:00:15+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:15Z"))
    assert _dispatched_replay_flags(dispatch) == [False]


def test_fresh_event_at_reconnect_and_steady_repeat_are_live(hass, coordinator):
    """A fresh post-reconnect event and a steady-state repeat both classify live.

    With the mark already set, a frame whose ``time`` is newer than the mark and
    within ``REPLAY_STALE_THRESHOLD`` of ``now`` is a genuine live transmission
    ("never drop a real one") — there is no suppression window. A later genuine
    repeat of the SAME value is likewise live.
    """
    # Prior live event sets the mark at 10:00:00 (UTC ``Z`` so age is tz-free).
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:00Z"))

    # Fresh event right after a reconnect: time == now, well within the threshold.
    assert timedelta(seconds=10) < REPLAY_STALE_THRESHOLD
    with freeze_time("2026-05-25T10:00:10+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:10Z"))
    assert _dispatched_replay_flags(dispatch) == [False]

    # Steady-state repeat of the same value, seconds later -> still live.
    with freeze_time("2026-05-25T10:00:20+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:20Z", value=1))
    assert _dispatched_replay_flags(dispatch) == [False]


def test_offline_device_not_resurrected_by_replay(hass, coordinator):
    """Replayed / stale frames do not flip an offline device back to available.

    Bring a device online, let the watchdog mark it unavailable past the timeout,
    then feed replayed/stale frames: ``available`` stays ``False`` and
    ``last_seen`` is unchanged. Only a fresh live frame restores availability.
    """
    key = _DOORBELL_KEY

    start = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    with freeze_time(start), patch(DISPATCH):
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:00Z"))
    assert coordinator.available[key] is True
    online_seen = coordinator.last_seen[key]

    # Past the 600s timeout: the watchdog flips it unavailable.
    with freeze_time(start + timedelta(seconds=601)), patch(DISPATCH):
        _run(hass, coordinator._async_watchdog(dt_util.utcnow()))
    assert coordinator.available[key] is False

    # A reconnect replays the buffer tail (already-seen) AND a gap event (stale).
    # Neither must resurrect the device or restamp last_seen.
    with freeze_time(start + timedelta(seconds=602)), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:00Z"))  # seen
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:05:00Z"))  # gap
    assert _dispatched_replay_flags(dispatch) == [True, True]
    assert coordinator.available[key] is False
    assert coordinator.last_seen[key] == online_seen

    # A genuinely-fresh live frame (time ~ now) restores availability.
    fresh = start + timedelta(seconds=700)
    with freeze_time(fresh), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            _doorbell_frame(fresh.strftime("%Y-%m-%dT%H:%M:%SZ"))  # == now, recent
        )
    assert _dispatched_replay_flags(dispatch) == [False]
    assert coordinator.available[key] is True
    assert coordinator.last_seen[key] != online_seen


def test_sensor_seeds_from_replay_without_stamping_last_seen(hass, coordinator):
    """A stale frame for a fresh sensor device seeds fields but not liveness.

    Feeding ONLY a stale replayed frame for a never-before-seen sensor device
    still records the device snapshot + field values (so its sensors can seed on
    reconnect/restart), but must NOT stamp ``last_seen`` or set ``available`` —
    the device stays governed by liveness until a live frame arrives.
    """
    key = "Acurite-606TX-42"
    # ``now`` is far ahead of the frame time (UTC ``Z``), so this is a stale gap
    # / replay event with no prior live frame for the device.
    with freeze_time("2026-05-25T10:30:00+00:00"), patch(DISPATCH) as dispatch:
        coordinator._handle_text_frame(
            '{"time": "2026-05-25T10:00:00Z", "model": "Acurite-606TX", '
            '"id": 42, "temperature_C": 21.4, "humidity": 55}'
        )

    # Classified as a replay (suppressed for events), yet the snapshot seeded.
    assert _dispatched_replay_flags(dispatch) == [True]
    assert key in coordinator.devices
    assert coordinator.devices[key].fields == {"temperature_C": 21.4, "humidity": 55}
    assert coordinator.device_fields[key] == {"temperature_C", "humidity"}
    assert coordinator.seen_fields >= {"temperature_C", "humidity"}
    # Liveness was NOT refreshed: no last_seen stamp, no availability set True.
    assert key not in coordinator.last_seen
    assert coordinator.available.get(key) is not True


# --------------------------------------------------------------------------- #
# Ingestion / classification DEBUG trace (Plan 22: event-trace logging).       #
# --------------------------------------------------------------------------- #
_TRACE_LOGGER = "custom_components.rtl_433"


def _ingestion_lines(caplog) -> list[str]:
    """Return the per-frame ``rtl_433 RX ... -> <verdict>`` ingestion lines."""
    return [m for m in caplog.messages if m.startswith("rtl_433 RX ")]


def test_live_frame_emits_single_ingestion_trace_line(hass, coordinator, caplog):
    """A live frame logs exactly one ``RX ... -> LIVE`` line with the device key."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:00Z"))

    lines = _ingestion_lines(caplog)
    assert len(lines) == 1
    assert _DOORBELL_KEY in lines[0]
    assert "-> LIVE" in lines[0]


def test_replay_and_backlog_trace_lines_and_no_event_fired(hass, coordinator, caplog):
    """REPLAY (at/below high-water) and BACKLOG (pre-connection) each trace and
    classify as a replay (so the event entity never fires)."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)

    # A first live frame sets the high-water mark.
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:00Z"))

    # REPLAY: the same frame re-sent on reconnect (time == mark) -> already-seen.
    caplog.clear()
    with freeze_time("2026-05-25T10:00:02+00:00"), patch(DISPATCH) as replay_dispatch:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:00Z"))
    replay_lines = _ingestion_lines(caplog)
    assert len(replay_lines) == 1
    assert _DOORBELL_KEY in replay_lines[0]
    assert "-> REPLAY" in replay_lines[0]
    # Classified as a replay -> the event entity must not fire.
    assert _dispatched_replay_flags(replay_dispatch) == [True]

    # BACKLOG: a recent press timestamped before the current connection.
    coordinator._connection_time = dt_util.parse_datetime("2026-05-25T11:00:10+00:00")
    caplog.clear()
    with freeze_time("2026-05-25T11:00:11+00:00"), patch(DISPATCH) as backlog_dispatch:
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T11:00:00Z"))
    backlog_lines = _ingestion_lines(caplog)
    assert len(backlog_lines) == 1
    assert _DOORBELL_KEY in backlog_lines[0]
    assert "-> BACKLOG" in backlog_lines[0]
    # Classified as a replay -> the event entity must not fire.
    assert _dispatched_replay_flags(backlog_dispatch) == [True]


def _discovery_lines(caplog) -> list[str]:
    """Return the ``rtl_433 discovered new device ...`` DEBUG lines."""
    return [
        m for m in caplog.messages if m.startswith("rtl_433 discovered new device ")
    ]


def _unmapped_lines(caplog) -> list[str]:
    """Return the ``rtl_433 ... reported unmapped field(s) ...`` DEBUG lines."""
    return [m for m in caplog.messages if "reported unmapped field(s)" in m]


def test_discovery_logs_new_device_line(hass, coordinator, caplog):
    """A live, post-connection first sighting logs the discovery DEBUG line once.

    The line carries the device key, model, and the ``via_replay`` verdict (False
    for a genuine live first sighting); a second sighting does not re-log it.
    """
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda key, model, is_replay: None

    frame = '{"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4}'
    with patch(DISPATCH):
        coordinator._handle_text_frame(frame)
        coordinator._handle_text_frame(frame)  # second sighting: no re-log

    lines = _discovery_lines(caplog)
    assert len(lines) == 1
    assert "Acurite-606TX-42" in lines[0]
    assert "model Acurite-606TX" in lines[0]
    assert "via_replay=False" in lines[0]


def test_discovery_via_replay_flag_is_true_for_replay(hass, coordinator, caplog):
    """A replay-discovered device logs the discovery line with ``via_replay=True``.

    A device first seen via a post-connection replay (time at/below high-water)
    still registers so its entities exist, and the line reflects the replay
    origin.
    """
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    coordinator.discovery_enabled = True
    coordinator.new_device_callback = lambda key, model, is_replay: None

    # Connect, then a first live frame sets the high-water mark for the device.
    coordinator._connection_time = dt_util.parse_datetime("2026-05-25T09:59:00+00:00")
    with freeze_time("2026-05-25T10:00:00+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(_doorbell_frame("2026-05-25T10:00:00Z"))
    caplog.clear()

    # A different device whose only sighting is a replay (time == an earlier,
    # at/below-high-water moment) is still discovered, flagged via_replay=True.
    replay_frame = json.dumps(
        {
            "model": "Acurite-606TX",
            "id": 99,
            "temperature_C": 20.0,
            "time": "2026-05-25T10:00:00Z",
        }
    )
    with freeze_time("2026-05-25T10:00:02+00:00"), patch(DISPATCH):
        coordinator._handle_text_frame(replay_frame)

    lines = _discovery_lines(caplog)
    assert len(lines) == 1
    assert "Acurite-606TX-99" in lines[0]
    assert "via_replay=True" in lines[0]


def test_unmapped_field_logged_once_per_device_field(hass, coordinator, caplog):
    """A field with no library descriptor logs once per (device, field).

    Gated on a non-empty ``known_field_keys`` (otherwise the whole library is
    treated as unloaded and nothing is flagged). A second identical frame must
    NOT re-log the same unmapped field.
    """
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    coordinator.known_field_keys = frozenset({"temperature_C"})

    frame = '{"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4, "mystery_field": 7}'
    with patch(DISPATCH):
        coordinator._handle_text_frame(frame)
        coordinator._handle_text_frame(frame)  # identical -> no second log

    lines = _unmapped_lines(caplog)
    assert len(lines) == 1
    assert "Acurite-606TX-42" in lines[0]
    assert "mystery_field" in lines[0]
    assert "(no entity)" in lines[0]
    # The known field is never reported as unmapped.
    assert "temperature_C" not in lines[0]


def test_unmapped_field_not_logged_when_library_empty(hass, coordinator, caplog):
    """With an empty ``known_field_keys`` the unmapped-field line is suppressed.

    An empty set means the library is unwired / failed to load, so flagging every
    field would be noise; the line is skipped entirely.
    """
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    coordinator.known_field_keys = frozenset()

    frame = '{"model": "Acurite-606TX", "id": 42, "mystery_field": 7}'
    with patch(DISPATCH):
        coordinator._handle_text_frame(frame)

    assert _unmapped_lines(caplog) == []


def _connect_loop_lines(caplog, needle: str) -> list[str]:
    """Return DEBUG trace lines from the connect loop containing ``needle``."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _TRACE_LOGGER and needle in r.getMessage()
    ]


class _FakeWS:
    """Minimal async-context-manager + async-iterator standing in for a WS.

    ``_read_frames`` does ``async with session.ws_connect(...) as ws`` then
    ``async for msg in ws``; this yields no frames and closes immediately, so the
    connect loop falls through to its reconnect path.
    """

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def __aiter__(self) -> _FakeWS:
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def test_connect_loop_logs_anchor_and_reconnect(hass, coordinator, caplog):
    """The connect loop logs the replay anchor on connect and the reconnect delay.

    Drives the real ``_connect_loop`` against a fake socket that yields no frames
    and closes, forcing one reconnect. ``_stop_event`` is set on the second
    connect so the loop terminates after logging both lines. The HTTP refresh
    helpers and the backoff constant are patched out to keep the test fast and
    isolated from the socket-less session.
    """
    import custom_components.rtl_433.coordinator.base as base_mod

    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    coordinator.manage_settings = False

    connects: list[str] = []

    def fake_ws_connect(url, **kwargs):
        connects.append(url)
        if len(connects) >= 2:
            # Stop after the second connect so the loop is bounded.
            coordinator._stop_event.set()
        return _FakeWS()

    session = Mock()
    session.ws_connect = fake_ws_connect

    with (
        patch(
            "custom_components.rtl_433.coordinator.base.async_get_clientsession",
            return_value=session,
        ),
        patch.object(base_mod, "_BACKOFF_MIN", 0.01),
        patch.object(Rtl433Coordinator, "_refresh_meta", new=_async_noop),
        patch.object(Rtl433Coordinator, "_refresh_stats", new=_async_noop),
        patch.object(Rtl433Coordinator, "_refresh_dev_info", new=_async_noop),
    ):
        _run(hass, coordinator._connect_loop())

    anchor = _connect_loop_lines(caplog, "connection anchor")
    reconnect = _connect_loop_lines(caplog, "reconnecting in")

    assert anchor, "expected a connection-anchor line on connect"
    assert "replay_high_water=" in anchor[0]
    assert "connected_at=" in anchor[0]
    assert reconnect, "expected a reconnect-delay line after the socket closed"
    assert coordinator.ws_url in reconnect[0]


async def _async_noop(self, *args, **kwargs) -> None:
    """No-op stand-in for the coordinator's async HTTP refresh helpers."""
    return None
