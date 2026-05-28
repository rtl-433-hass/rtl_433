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
from unittest.mock import patch

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


def test_classifies_local_iso_and_unparseable_times(hass, coordinator):
    """A local, an ISO/``Z``, and an unparseable frame each classify as expected.

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
        # Blank ``time`` -> unparseable -> treated as live, never raises.
        coordinator._handle_text_frame(_doorbell_frame(""))
        # Garbage ``time`` -> unparseable -> live, never raises.
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
