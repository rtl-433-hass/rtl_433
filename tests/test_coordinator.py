"""Behavioral tests for the rtl_433 push coordinator's HA-side adaptation.

The transport half — WebSocket frame parsing, event normalization, reconnect
replay classification, and the HTTP ``/cmd`` getters/setters — now lives in
:class:`pyrtl_433.Rtl433Client` and is tested upstream in the library. This file
covers only what the *coordinator* still owns: applying a client-delivered,
already-classified :class:`~pyrtl_433.normalizer.NormalizedEvent` to per-device
runtime state, the registration of an adopted device, the availability watchdog,
the per-device effective-timeout resolution, and the device-update dispatch.
Every test here works on an *adopted* device (see the ``coordinator`` fixture);
the pending/ignored routing for unadopted devices is its own contract.

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

from custom_components.rtl_433.const import signal_device_update, signal_pending_update
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.coordinator._events import _MAX_PENDING_CANDIDATES
from homeassistant.util import dt as dt_util

DISPATCH = "custom_components.rtl_433.coordinator.base.async_dispatcher_send"
_TRACE_LOGGER = "custom_components.rtl_433"
# The device key every ``_event()`` carries, and the one the fixture adopts.
_KEY = "Acurite-606TX-42"


def _dispatched(dispatch) -> list[str]:
    """Return the signal names one patched ``async_dispatcher_send`` recorded.

    Several different dispatcher signals go through the one function these tests
    patch, and a device-update -- the fan-out that reaches a device's entities --
    is only one of them. Asserting on names rather than call counts lets a test
    say "no entity fan-out happened" exactly, instead of approximating it with
    "nothing happened at all".
    """
    return [call.args[1] for call in dispatch.call_args_list]


def _run(hass, coro):
    """Drive an async coordinator method to completion on the hass loop."""
    return hass.loop.run_until_complete(coro)


@pytest.fixture
async def coordinator(hass, hub_entry_builder):
    """Build a coordinator wired to a hub entry, with a 600s timeout.

    Async so construction runs inside the event loop: the coordinator now builds
    its :class:`pyrtl_433.Rtl433Client` in ``__init__`` (injecting HA's shared
    aiohttp session), which requires a running loop.

    ``_KEY`` is pre-adopted because these tests cover what the coordinator does
    with an *adopted* device's frames — runtime state, registration, the
    watchdog, dispatch. A device the user has not adopted never reaches any of
    that; it lands in the pending list instead, which is a separate contract.
    """
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)
    return Rtl433Coordinator(
        hass,
        entry,
        host="rtl433.local",
        availability_timeout=600,
        skip_keys={"model", "id", "channel", "subtype", "time", "mic"},
        adopted_keys={_KEY},
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
# Adopted-device registration.                                                 #
# --------------------------------------------------------------------------- #
def test_new_device_callback_fires_once_per_process(hass, coordinator):
    """The new-device hook fires only on the first sighting of a device."""
    seen: list[tuple[str, str, bool]] = []
    coordinator.new_device_callback = lambda key, model, is_replay: seen.append(
        (key, model, is_replay)
    )

    with patch(DISPATCH):
        coordinator._on_client_event(_event())
        coordinator._on_client_event(_event())  # second sighting: no new callback

    assert seen == [("Acurite-606TX-42", "Acurite-606TX", False)]


def test_no_crash_when_new_device_callback_unset(hass, coordinator):
    """An adopted device with no callback wired is still tracked, without raising."""
    coordinator.new_device_callback = None
    with patch(DISPATCH):
        coordinator._on_client_event(_event())
    assert "Acurite-606TX-42" in coordinator.devices


def test_backlog_event_seeds_state_but_does_not_register(hass, coordinator):
    """A pre-connection backlog frame seeds state without registering the device."""
    seen: list[str] = []
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
    coordinator.new_device_callback = lambda k, m, r: seen.append(k)
    conn = dt_util.parse_datetime("2026-05-25T10:00:00+00:00")
    coordinator._connection_time = conn

    at_grace = _event(event_time=conn - DISCOVERY_BACKLOG_GRACE)
    with patch(DISPATCH):
        coordinator._on_client_event(at_grace)
    assert seen == ["Acurite-606TX-42"]  # not < boundary -> not backlog -> registers


def test_callback_exception_does_not_break_ingest(hass, coordinator):
    """A throwing new_device_callback is caught; the device is still tracked."""
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
    """forget_device un-adopts the device and clears every runtime dict."""
    key = "Acurite-606TX-42"
    with patch(DISPATCH):
        coordinator._on_client_event(_event())
    assert key in coordinator.devices

    coordinator.forget_device(key)
    assert key not in coordinator._discovered
    _assert_nothing_tracks(coordinator, key)

    # Un-adopted, so the next transmission makes it a pending candidate again
    # rather than silently re-creating the device the user deleted. It fires one
    # pending-update -- the device is a fresh candidate and the discovery panel
    # has to hear about it -- and no device-update, which is the point: the
    # entities the user deleted must not be fanned out to again.
    with patch(DISPATCH) as dispatch:
        coordinator._on_client_event(_event())
    assert key not in coordinator.devices
    assert coordinator.pending[key].count == 1
    assert _dispatched(dispatch) == [signal_pending_update(coordinator.entry.entry_id)]

    # forget on an unknown key is a safe no-op.
    coordinator.forget_device("nonexistent-key")


# --------------------------------------------------------------------------- #
# Coordinator-side DEBUG traces (registration + unmapped fields).              #
# --------------------------------------------------------------------------- #
def test_registration_logs_new_device_line_once(hass, coordinator, caplog):
    """A first sighting logs the registration DEBUG line once, with via_replay."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    coordinator.new_device_callback = lambda key, model, is_replay: None

    with patch(DISPATCH):
        coordinator._on_client_event(_event())
        coordinator._on_client_event(_event())  # second sighting: no re-log

    lines = [
        m for m in caplog.messages if m.startswith("rtl_433 registered adopted device")
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


# --------------------------------------------------------------------------- #
# The client is given HA's configured zone for naive-timestamp classification. #
# --------------------------------------------------------------------------- #
async def test_client_receives_ha_configured_event_tz(hass, hub_entry_builder):
    """The coordinator passes HA's configured zone as the client's event_tz.

    Regression guard: an offset-less rtl_433 ``time`` stamp must be classified in
    HA's configured zone (matching the pre-extraction ``dt_util.as_utc`` behavior),
    not the host process zone. Dropping ``event_tz`` would silently misclassify
    live events as stale replays whenever the host zone differs from HA's.
    """
    await hass.config.async_set_time_zone("America/New_York")
    configured = dt_util.get_default_time_zone()
    entry = hub_entry_builder(availability_timeout=600)
    entry.add_to_hass(hass)

    coordinator = Rtl433Coordinator(
        hass, entry, host="rtl433.local", availability_timeout=600
    )

    assert coordinator._client._event_tz == configured
    assert coordinator._client._event_tz.key == "America/New_York"


# --------------------------------------------------------------------------- #
# Cap on the pending-candidate list.                                           #
# --------------------------------------------------------------------------- #
def test_pending_candidate_cap_is_the_documented_value():
    """The ceiling is a deliberate number, not an incidental one.

    Pinned explicitly so a change to it is a change to this test: it is sized far
    above what a busy receiver hears, and the whole point is that it is generous
    enough never to touch a real install.
    """
    assert _MAX_PENDING_CANDIDATES == 512


def _assert_nothing_tracks(coordinator, key):
    """Assert no runtime map on the coordinator still holds ``key``.

    Reflective rather than a hand-written list of maps: there are six of them and
    the list is what drifts — two were found still holding evicted keys. A
    seventh added later is covered by this without anyone remembering to.

    ``calibration_snapshot`` and ``user_mappings_snapshot`` are excluded: they
    mirror the user's stored options rather than what the radio has been heard
    saying, and deleting a device is not meant to discard its configuration.
    """
    config_mirrors = {"calibration_snapshot", "user_mappings_snapshot"}
    leaked = sorted(
        name
        for name, value in vars(coordinator).items()
        if name not in config_mirrors
        and isinstance(value, (dict, set))
        and key in value
    )
    assert not leaked, f"{key} still tracked in {leaked}"


def _flood(coordinator, count):
    """Feed ``count`` distinct one-off device keys, as spurious decodes do."""
    for index in range(count):
        coordinator._on_client_event(_event(key=f"Noise-{index}", model="Noise"))


def test_spurious_decodes_do_not_grow_the_list_without_bound(hass, coordinator):
    """A shared band's junk decodes are dropped coldest-first at the cap.

    433 MHz produces decodes with arbitrary ids. Without a ceiling every one of
    them would sit on the pending list for the life of the config entry -- and
    every one would be rendered into the payload pushed to every open panel.
    """
    with patch(DISPATCH):
        _flood(coordinator, _MAX_PENDING_CANDIDATES + 10)

    # Exactly at the cap: dropping further would discard candidates nothing
    # asked us to discard.
    assert len(coordinator.pending) == _MAX_PENDING_CANDIDATES
    assert "Noise-0" not in coordinator.pending
    assert f"Noise-{_MAX_PENDING_CANDIDATES + 9}" in coordinator.pending


def test_nothing_is_dropped_at_exactly_the_cap(hass, coordinator):
    """The ceiling is a maximum to stay at, not one to fall below."""
    with patch(DISPATCH):
        _flood(coordinator, _MAX_PENDING_CANDIDATES)

    assert len(coordinator.pending) == _MAX_PENDING_CANDIDATES
    assert "Noise-0" in coordinator.pending


def test_a_candidate_heard_again_is_no_longer_the_coldest(hass, coordinator):
    """Recency is what "cold" means, so a repeat transmission buys a reprieve.

    Without this the list would drop by first-sighting order, which would discard
    a device that is still transmitting -- and still worth offering -- in favour
    of one that stopped long ago.
    """
    with patch(DISPATCH):
        _flood(coordinator, _MAX_PENDING_CANDIDATES)
        # The oldest key transmits again, so the *second* oldest is now coldest.
        coordinator._on_client_event(_event(key="Noise-0", model="Noise"))
        coordinator._on_client_event(_event(key="Noise-fresh", model="Noise"))

    assert "Noise-0" in coordinator.pending
    assert "Noise-1" not in coordinator.pending


def test_the_candidate_just_heard_is_never_the_one_dropped(hass, coordinator):
    """The frame that pushes the list over the cap is not what pays for it.

    It has just been moved to the warm end, so the drop comes off the cold end --
    which is the whole point, since the newest arrival is the one the user is
    most likely waiting to see.
    """
    with patch(DISPATCH):
        _flood(coordinator, _MAX_PENDING_CANDIDATES)
        coordinator._on_client_event(_event(key="Noise-fresh", model="Noise"))

    assert "Noise-fresh" in coordinator.pending
    assert "Noise-0" not in coordinator.pending
    assert len(coordinator.pending) == _MAX_PENDING_CANDIDATES


def test_a_dropped_candidate_leaves_nothing_behind(hass, coordinator):
    """A candidate holds no runtime state, so dropping it strands nothing.

    Asserted reflectively rather than against a list of maps: a candidate is
    only ever supposed to exist in ``pending``, and this is what says so.
    """
    with patch(DISPATCH):
        _flood(coordinator, _MAX_PENDING_CANDIDATES + 1)

    _assert_nothing_tracks(coordinator, "Noise-0")


def test_dropping_a_candidate_logs_the_key_and_the_cap(hass, coordinator, caplog):
    """The DEBUG line names what went and why, or it explains nothing."""
    caplog.set_level(logging.DEBUG, logger=_TRACE_LOGGER)
    with patch(DISPATCH):
        _flood(coordinator, _MAX_PENDING_CANDIDATES + 1)

    lines = [m for m in caplog.messages if m.startswith("rtl_433 dropping the coldest")]
    assert len(lines) == 1
    assert "Noise-0" in lines[0]
    assert str(_MAX_PENDING_CANDIDATES) in lines[0]


def test_adopted_devices_are_out_of_the_caps_reach(hass, coordinator):
    """A device the user added cannot be dropped by the cap, at any volume.

    Not because the cap skips it, but because it is not on the pending list at
    all: an adopted key is routed into runtime state and never becomes a
    candidate. That is what makes the cap safe to apply without any notion of a
    protected key -- the state a user's entities read is simply not in the map
    being capped.
    """
    adopted_key = "Acurite-606TX-42"
    coordinator.adopted.add(adopted_key)

    with patch(DISPATCH):
        coordinator._on_client_event(_event(key=adopted_key))
        _flood(coordinator, _MAX_PENDING_CANDIDATES + 10)

    assert adopted_key in coordinator.devices
    assert adopted_key in coordinator.last_seen
    assert adopted_key not in coordinator.pending
    assert len(coordinator.pending) == _MAX_PENDING_CANDIDATES


def test_forget_device_clears_the_log_once_memos(hass, coordinator):
    """A device that comes back is new to us, so both memos log again.

    ``forget_device`` used to leave these behind, so a device that was deleted
    and came back never re-logged its unmapped fields or its resolved timeout.
    """
    coordinator.known_field_keys = frozenset({"temperature_C"})
    key = "Acurite-606TX-42"
    with patch(DISPATCH):
        coordinator._on_client_event(_event(key=key, fields={"made_up_field": 1}))
    coordinator._log_timeout_change(key, 600, "hub")
    assert coordinator._logged_unmapped.get(key)
    assert key in coordinator._logged_timeouts

    coordinator.forget_device(key)
    _assert_nothing_tracks(coordinator, key)
