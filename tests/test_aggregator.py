"""The location aggregator: the union of several receivers' view of one sensor.

Two rtl_433 servers within range of the same 433 MHz sensor decode the *same*
transmission twice, a few hundred milliseconds and one host clock apart. These
tests pin what the integration does with that:

* **grouping** -- both receivers register one location-scoped device identifier
  under one owning entry, so the registry resolves them to ONE device;
* **the entity union** -- one entity per mapped field, not one per receiver;
* **the dedup** -- a near-duplicate inside the debounce window applies exactly
  once, a clearly-newer frame advances the value, and a replayed frame arriving
  after a live one never regresses it; and
* **the exclusion set** -- ``rssi`` / ``snr`` / ``last_seen`` describe the link
  between one receiver and the sensor, so they are partitioned out on the way in
  and neither unioned nor deduped;
* **merged availability** -- a receiver *vouches* for a device when it is
  connected AND received it inside the timeout, and the device is available when at
  least one receiver vouches. The full cross-product is asserted because the row
  an independent OR of the two gates gets wrong (connected-but-deaf plus
  offline-but-fresh) is the whole reason the rule is shaped this way;
* **per-receiver signal entities** -- one ``rssi`` / ``snr`` / ``last_seen``
  entity per (sensor x receiver) on the ONE merged device, each naming its
  receiver so no ``entity_id`` gains a ``_2`` suffix, each carrying no
  ``config_subentry_id``, and the comparison between them served from aggregator
  state so nothing has to be enabled to see it; and
* **receiver removal** -- deleting a receiver takes its own device and its signal
  entities, keeps every merged device and its history, and leaves a device only
  that receiver ever received unavailable rather than deleted;
* **the merged candidate list** -- a sensor two receivers receive is ONE row on the
  add-device page, showing the last frame to *arrive* (no debounce, unlike the
  adopted-value rule above) and naming who received it, with each receiver's
  replay / backlog gate applied before the merge and the candidate cap applied
  after it; and
* **location-scoped adoption** -- adopting or ignoring a device once applies to
  every receiver in the location, and ``ignored`` wins if a consolidation ever
  puts a key on both lists.

Frames are fed straight into each receiver's client (``_process_event``), the
same seam the rest of the suite uses, with an explicit ``time`` because the
dedup's whole rule is expressed in terms of the frame's own stamp.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from freezegun import freeze_time
from pyrtl_433.normalizer import NormalizedEvent
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.rtl_433.adoption import (
    async_adopt_devices,
    async_ignore_devices,
    async_unignore_devices,
)
from custom_components.rtl_433.aggregator import (
    _MERGE_DEBOUNCE,
    LAST_SEEN_FIELD,
    LINK_FIELD_KEYS,
    Rtl433LocationAggregator,
    clear_pending,
    is_link_field,
    location_aggregator,
    merged_candidate,
    merged_candidates,
    partition_fields,
)
from custom_components.rtl_433.const import (
    CONF_DEVICES,
    CONF_IGNORED_DEVICES,
    CONF_MODEL,
    DATA_AGGREGATOR,
    DEVICE_FIELDS,
    DOMAIN,
    signal_location_device_update,
    signal_pending_update,
)
from custom_components.rtl_433.coordinator import MAX_PENDING_CANDIDATES
from homeassistant.config_entries import RELOAD_AFTER_UPDATE_DELAY
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from tests.conftest import (
    build_receiver_entry,
    build_receiver_subentry,
    receiver_id,
    receiver_subentry,
)

_MODEL = "Acurite-606TX"
_DEVICE_KEY = "Acurite-606TX-42"
# A fixed instant every test freezes at, so a frame stamped "now" classifies as a
# live transmission rather than a stale-gap replay.
_NOW = "2026-05-25T10:00:00+00:00"


def _at(offset_seconds: float) -> str:
    """Return an rtl_433 ``time`` stamp ``offset_seconds`` from :data:`_NOW`."""
    stamp = datetime.fromisoformat(_NOW) + timedelta(seconds=offset_seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _frame(temperature: float, when: float, **extra: float) -> dict:
    """Build one decoded Acurite frame."""
    return {
        "time": _at(when),
        "model": _MODEL,
        "id": 42,
        "temperature_C": temperature,
        **extra,
    }


async def _setup_two_receivers(hass, **kwargs):
    """Set up one location with two receivers, pre-seeded with the sensor."""
    location = build_receiver_entry(
        availability_timeout=600,
        devices={
            _DEVICE_KEY: {CONF_MODEL: _MODEL, DEVICE_FIELDS: ["temperature_C"]},
        },
        receivers=[
            build_receiver_subentry(host="attic.local"),
            build_receiver_subentry(host="garage.local"),
        ],
        **kwargs,
    )
    location.add_to_hass(hass)
    assert await hass.config_entries.async_setup(location.entry_id)
    await hass.async_block_till_done()
    return location


def _coordinators(hass, location):
    """Return the location's two coordinators, in subentry order."""
    return [hass.data[DOMAIN][receiver_id(location, index)] for index in (0, 1)]


def _feed(coordinator, frame: dict) -> None:
    """Drive one frame through a receiver's client, as the socket would."""
    coordinator._client._process_event(frame)


def _temperature(hass, location):
    """Return the merged device's temperature state, as a string."""
    eid = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}:T"
    )
    assert eid is not None
    return hass.states.get(eid).state


# --------------------------------------------------------------------------- #
# The field partition (the union's exclusion set).                            #
# --------------------------------------------------------------------------- #
class TestFieldPartition:
    """``rssi`` / ``snr`` / ``last_seen`` are link fields; nothing else is."""

    def test_exclusion_set_is_exactly_the_three_link_fields(self):
        assert frozenset({"rssi", "snr", LAST_SEEN_FIELD}) == LINK_FIELD_KEYS
        assert LAST_SEEN_FIELD == "__last_seen__"

    @pytest.mark.parametrize("field_key", ["rssi", "snr", LAST_SEEN_FIELD])
    def test_link_fields_are_recognised(self, field_key):
        assert is_link_field(field_key) is True

    @pytest.mark.parametrize(
        "field_key", ["temperature_C", "humidity", "battery_ok", "noise"]
    )
    def test_sensor_fields_are_not_link_fields(self, field_key):
        assert is_link_field(field_key) is False

    def test_partition_splits_one_frame_into_both_halves(self):
        unioned, link = partition_fields(
            {"temperature_C": 21.4, "humidity": 55, "rssi": -62.0, "snr": 11.5}
        )
        assert unioned == {"temperature_C": 21.4, "humidity": 55}
        assert link == {"rssi": -62.0, "snr": 11.5}

    def test_partition_of_a_frame_with_no_link_fields_keeps_everything(self):
        unioned, link = partition_fields({"temperature_C": 21.4})
        assert unioned == {"temperature_C": 21.4}
        assert link == {}

    def test_debounce_window_is_in_the_specified_band(self):
        """2-5 s: long enough for a repeat burst plus skew, short enough to
        never swallow a genuine new reading."""
        assert timedelta(seconds=2) <= _MERGE_DEBOUNCE <= timedelta(seconds=5)


# --------------------------------------------------------------------------- #
# Sub-step A — device grouping.                                               #
# --------------------------------------------------------------------------- #
async def test_two_receivers_hearing_one_sensor_yield_one_device(hass):
    """Both receivers' entities land on ONE device-registry device.

    They register the same location-scoped identifier under the same owning
    entry, which is what makes the registry resolve them to one row -- the
    receivers are subentries of one entry, not two entries HA merges across
    (that behaviour is gone since registry storage v3).
    """
    location = await _setup_two_receivers(hass)
    with freeze_time(_NOW):
        for coordinator in _coordinators(hass, location):
            _feed(coordinator, _frame(21.4, 0))
        await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    devices = [
        device
        for device in dr.async_entries_for_config_entry(dev_reg, location.entry_id)
        if (DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}") in device.identifiers
    ]
    assert len(devices) == 1
    # Owned by the location, not by either receiver's subentry.
    assert devices[0].config_entries_subentries[location.entry_id] == {None}


async def test_two_receivers_yield_one_entity_per_mapped_field(hass):
    """One physical sensor, one entity per *unioned* field, however many hear it.

    The link fields are the deliberate exception: they are excluded from the
    union, so each receiver contributes its own (see the per-receiver signal
    tests below).
    """
    location = await _setup_two_receivers(hass)
    with freeze_time(_NOW):
        for coordinator in _coordinators(hass, location):
            _feed(coordinator, _frame(21.4, 0))
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    unique_ids = sorted(
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, location.entry_id)
        if f":{_DEVICE_KEY}:" in entity.unique_id
    )
    attic, garage = (receiver_id(location, index) for index in (0, 1))
    assert unique_ids == sorted(
        [
            f"{location.entry_id}:{_DEVICE_KEY}:T",
            f"{location.entry_id}:{_DEVICE_KEY}:{attic}:last_seen",
            f"{location.entry_id}:{_DEVICE_KEY}:{garage}:last_seen",
        ]
    )


async def test_device_field_unique_id_is_receiver_agnostic(hass):
    """The unique_id names the location and the device -- never a receiver."""
    location = await _setup_two_receivers(hass)
    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}:T"
    )
    assert eid is not None
    for index in (0, 1):
        assert receiver_id(location, index) not in ent_reg.async_get(eid).unique_id


# --------------------------------------------------------------------------- #
# Sub-step B — the dedup.                                                     #
# --------------------------------------------------------------------------- #
async def test_a_clearly_newer_frame_advances_the_value(hass):
    """A second transmission, well past the window, is applied."""
    location = await _setup_two_receivers(hass)
    attic, _garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0))
        await hass.async_block_till_done()
        assert _temperature(hass, location) == "21.4"

    later = datetime.fromisoformat(_NOW) + timedelta(seconds=60)
    with freeze_time(later):
        _feed(attic, _frame(22.9, 60))
        await hass.async_block_till_done()
    assert _temperature(hass, location) == "22.9"


async def test_a_near_duplicate_from_the_other_receiver_applies_exactly_once(hass):
    """The same transmission received twice moves the value once: first wins.

    The second receiver's copy carries a slightly different decoded value here
    purely so the assertion can *see* which one was taken; a real pair would
    agree.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0))
        await hass.async_block_till_done()
        assert _temperature(hass, location) == "21.4"

        # Garage decodes the same transmission a beat later, and its host clock
        # reads a little ahead -- still well inside the debounce window.
        _feed(garage, _frame(99.9, 1))
        await hass.async_block_till_done()

    assert _temperature(hass, location) == "21.4"


async def test_a_replay_after_a_live_frame_does_not_regress_the_value(hass):
    """A backlog replay (T0 << T1) arriving after a live frame is rejected."""
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0))
        await hass.async_block_till_done()
        assert _temperature(hass, location) == "21.4"

        # Garage reconnects and the server replays a ten-minute-old frame.
        _feed(garage, _frame(15.0, -600))
        await hass.async_block_till_done()

    assert _temperature(hass, location) == "21.4"


async def test_a_replay_still_seeds_a_field_nothing_has_applied_yet(hass):
    """The stale-frame rejection is relative, not absolute.

    Nothing has been applied for this field, so the replay is the only reading
    there is and it must seed the entity -- the reconnect-replay seeding the rest
    of the integration relies on.
    """
    location = await _setup_two_receivers(hass)
    attic, _garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(15.0, -600))
        await hass.async_block_till_done()

    assert _temperature(hass, location) == "15.0"


async def test_dedup_is_per_field_not_per_frame(hass):
    """A frame carrying one deduped and one fresh field applies the fresh one."""
    location = await _setup_two_receivers(hass)
    aggregator = hass.data[DOMAIN][DATA_AGGREGATOR][location.entry_id]
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0))
        await hass.async_block_till_done()
        # The garage's copy repeats the temperature and adds a humidity reading
        # the attic never decoded.
        _feed(garage, _frame(99.9, 1, humidity=55))
        await hass.async_block_till_done()

    assert _temperature(hass, location) == "21.4"
    assert (_DEVICE_KEY, "humidity") in aggregator._applied


async def test_the_anchor_moves_to_each_applied_frame(hass):
    """The window is measured against the *last applied* frame, not the first.

    A third transmission arriving just after the second is still a duplicate of
    the second -- which only holds if applying a frame re-anchors the window on
    it.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    base = datetime.fromisoformat(_NOW)
    with freeze_time(base):
        _feed(attic, _frame(21.4, 0))
        await hass.async_block_till_done()
    with freeze_time(base + timedelta(seconds=60)):
        _feed(attic, _frame(22.9, 60))
        await hass.async_block_till_done()
        assert _temperature(hass, location) == "22.9"
    with freeze_time(base + timedelta(seconds=61)):
        _feed(garage, _frame(99.9, 61))
        await hass.async_block_till_done()

    assert _temperature(hass, location) == "22.9"


async def test_a_frame_exactly_on_the_window_boundary_is_a_duplicate(hass):
    """The debounce window is inclusive: exactly ``_MERGE_DEBOUNCE`` apart is one
    transmission, not two."""
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    edge = _MERGE_DEBOUNCE.total_seconds()
    base = datetime.fromisoformat(_NOW)
    with freeze_time(base):
        _feed(attic, _frame(21.4, 0))
        await hass.async_block_till_done()
    with freeze_time(base + timedelta(seconds=edge)):
        _feed(garage, _frame(99.9, edge))
        await hass.async_block_till_done()

    assert _temperature(hass, location) == "21.4"


async def test_an_unstamped_frame_after_a_stamped_one_is_applied(hass):
    """With no ``event_time`` there is nothing to measure, so the frame is taken.

    The aggregator does not guess: a redundant apply writes a value the entity
    already holds, where a wrong rejection would drop a real reading for good.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0))
        await hass.async_block_till_done()
        _feed(garage, {"model": _MODEL, "id": 42, "temperature_C": 22.2})
        await hass.async_block_till_done()

    assert _temperature(hass, location) == "22.2"


async def test_known_device_keys_unions_the_stored_map_with_the_live_view(hass):
    """Startup subscribes for the stored devices *and* a coordinator's live set.

    The stored map is the adopted set, so it is normally the whole answer; the
    union covers a device adopted between a coordinator starting and the
    aggregator starting, which would otherwise never be subscribed to at all.
    """
    location = await _setup_two_receivers(hass)
    aggregator: Rtl433LocationAggregator = hass.data[DOMAIN][DATA_AGGREGATOR][
        location.entry_id
    ]
    attic, _garage = _coordinators(hass, location)
    attic.devices["Acurite-606TX-99"] = object()

    assert set(aggregator._known_device_keys(attic)) == {
        _DEVICE_KEY,
        "Acurite-606TX-99",
    }


# --------------------------------------------------------------------------- #
# The exclusion set is neither unioned nor deduped.                           #
# --------------------------------------------------------------------------- #
async def test_link_fields_are_stripped_from_the_location_signal(hass):
    """``rssi`` / ``snr`` never reach the receiver-agnostic stream."""
    location = await _setup_two_receivers(hass)
    attic, _garage = _coordinators(hass, location)

    seen: list[dict] = []
    unsub = async_dispatcher_connect(
        hass,
        signal_location_device_update(location.entry_id, _DEVICE_KEY),
        lambda event: seen.append(dict(event.fields)),
    )
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0, snr=11.5))
        await hass.async_block_till_done()
    unsub()

    assert seen == [{"temperature_C": 21.4}]


async def test_link_fields_are_never_deduped(hass):
    """No dedup anchor is ever recorded for a link field.

    Merging them would publish whichever receiver won the debounce race, which
    is meaningless for a signal measurement, so they are partitioned out before
    the dedup ever sees them.
    """
    location = await _setup_two_receivers(hass)
    aggregator = hass.data[DOMAIN][DATA_AGGREGATOR][location.entry_id]
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0, snr=11.5))
        _feed(garage, _frame(21.4, 1, rssi=-89.0, snr=3.0))
        await hass.async_block_till_done()

    assert {field for _key, field in aggregator._applied} == {"temperature_C"}


async def test_a_link_field_entity_still_follows_its_own_receiver(hass):
    """An ``rssi`` entity reads its receiver's frames, not the merged stream.

    It subscribes to the per-receiver signal precisely because the location
    stream has the link fields stripped out of it.
    """
    location = await _setup_two_receivers(hass)
    attic, _garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0))
        await hass.async_block_till_done()

    # ``rssi`` ships disabled-by-default (a diagnostic), so enable it as a user
    # would and let the debounced reload rebuild the platform.
    ent_reg = er.async_get(hass)
    rssi_eid = ent_reg.async_get_entity_id(
        "sensor",
        DOMAIN,
        f"{location.entry_id}:{_DEVICE_KEY}:{receiver_id(location)}:rssi",
    )
    assert rssi_eid is not None
    ent_reg.async_update_entity(rssi_eid, disabled_by=None)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RELOAD_AFTER_UPDATE_DELAY + 1)
    )
    await hass.async_block_till_done()

    attic, _garage = _coordinators(hass, location)
    _feed(attic, {"model": _MODEL, "id": 42, "rssi": -77.0})
    await hass.async_block_till_done()

    assert hass.states.get(rssi_eid).state == "-77.0"


# --------------------------------------------------------------------------- #
# Lifecycle.                                                                  #
# --------------------------------------------------------------------------- #
async def test_removing_a_device_drops_its_dedup_anchors(hass):
    """A removed device starts from a clean slate if the user adds it back."""
    location = await _setup_two_receivers(hass)
    aggregator = hass.data[DOMAIN][DATA_AGGREGATOR][location.entry_id]
    attic, _garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0))
        await hass.async_block_till_done()
    assert aggregator._applied

    aggregator.forget_device(_DEVICE_KEY)
    assert aggregator._applied == {}


async def test_unloading_the_location_tears_the_aggregator_down(hass):
    """Unload drops the subscriptions, the anchors and the coordinator hooks."""
    location = await _setup_two_receivers(hass)
    aggregator = hass.data[DOMAIN][DATA_AGGREGATOR][location.entry_id]
    coordinators = _coordinators(hass, location)
    assert all(
        aggregator.forget_device in coordinator.device_removers
        for coordinator in coordinators
    )

    assert await hass.config_entries.async_unload(location.entry_id)
    await hass.async_block_till_done()

    assert hass.data[DOMAIN][DATA_AGGREGATOR] == {}
    assert aggregator._applied == {}
    assert aggregator._subscribed == set()
    assert all(
        aggregator.forget_device not in coordinator.device_removers
        for coordinator in coordinators
    )


async def test_a_device_adopted_mid_session_is_subscribed_to(hass):
    """``signal_new_device`` opens the aggregator's subscription at runtime."""
    location = build_receiver_entry(
        availability_timeout=600,
        receivers=[
            build_receiver_subentry(host="attic.local"),
            build_receiver_subentry(host="garage.local"),
        ],
    )
    location.add_to_hass(hass)
    assert await hass.config_entries.async_setup(location.entry_id)
    await hass.async_block_till_done()

    aggregator: Rtl433LocationAggregator = hass.data[DOMAIN][DATA_AGGREGATOR][
        location.entry_id
    ]
    assert aggregator._subscribed == set()

    attic, _garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0))
        await hass.async_block_till_done()
        assert attic.adopt_device(_DEVICE_KEY) is not None
        await hass.async_block_till_done()

    assert (receiver_id(location, 0), _DEVICE_KEY) in aggregator._subscribed


# --------------------------------------------------------------------------- #
# Merged availability: vouching, evaluated per receiver and then OR-ed.       #
# --------------------------------------------------------------------------- #
def _set_connected(coordinator, connected: bool) -> None:
    """Flip one receiver's transport gate and dispatch the repaint."""
    coordinator._client.connected = connected
    coordinator._async_sync_receiver_availability()


def _merged_entity(hass, location):
    """Return the merged device's temperature entity object."""
    eid = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}:T"
    )
    assert eid is not None
    entity = hass.data["entity_components"]["sensor"].get_entity(eid)
    assert entity is not None
    return entity


@pytest.mark.parametrize(
    ("attic", "garage", "expected"),
    [
        # (connected, received-recently) per receiver -> merged availability.
        pytest.param((True, True), (True, True), True, id="both-vouch"),
        pytest.param((True, True), (False, True), True, id="attic-vouches-alone"),
        pytest.param((False, True), (True, True), True, id="garage-vouches-alone"),
        pytest.param(
            (True, True), (True, False), True, id="garage-deaf-attic-receives"
        ),
        # The bug the naive merge introduces: "some receiver connected" AND "some
        # last_seen fresh" both hold, yet no single receiver can hear the device.
        pytest.param(
            (True, False), (False, True), False, id="connected-deaf-vs-offline-fresh"
        ),
        pytest.param((True, False), (True, False), False, id="both-deaf"),
        pytest.param((False, True), (False, True), False, id="both-offline"),
        pytest.param((False, False), (False, False), False, id="nothing-at-all"),
    ],
)
async def test_merged_availability_is_the_or_of_per_receiver_vouches(
    hass, attic, garage, expected
):
    """A device is available exactly while some receiver both receives and is received.

    The cross-product is spelled out rather than sampled because the whole point
    of evaluating the pair per receiver is the one row an independent OR of the
    two gates gets wrong: a connected receiver that is deaf to the sensor plus an
    offline receiver that received it a minute ago satisfies "a receiver is
    connected" and "a last_seen is fresh" while nothing can actually hear the
    device.
    """
    location = await _setup_two_receivers(hass)
    entity = _merged_entity(hass, location)
    now = dt_util.utcnow()

    for coordinator, (connected, fresh) in zip(
        _coordinators(hass, location), (attic, garage), strict=True
    ):
        _set_connected(coordinator, connected)
        # 600s is the location's configured timeout, so "stale" is well past it.
        coordinator.last_seen[_DEVICE_KEY] = now - timedelta(
            seconds=1 if fresh else 6000
        )
    await hass.async_block_till_done()

    assert entity.available is expected


async def test_a_receiver_vouches_at_exactly_the_timeout(hass):
    """The silence gate is inclusive: age == timeout is still fresh.

    Pinned because the boundary is the one place the merged rule can silently
    disagree with the watchdog, which uses the same comparison.
    """
    location = await _setup_two_receivers(hass)
    entity = _merged_entity(hass, location)
    attic, garage = _coordinators(hass, location)
    start = dt_util.utcnow()
    for coordinator in (attic, garage):
        coordinator.last_seen[_DEVICE_KEY] = start

    # 600s is the location's configured availability timeout.
    with freeze_time(start + timedelta(seconds=600)):
        assert entity.available is True
    with freeze_time(start + timedelta(seconds=601)):
        assert entity.available is False


def test_there_is_no_aggregator_before_any_location_is_set_up(hass):
    """``hass.data`` has no domain bucket at all yet -- that is not an error.

    An entity reading ``available`` outside a running location has to get a
    ``None`` it can fall back from, never an ``AttributeError``.
    """
    hass.data.pop(DOMAIN, None)
    assert location_aggregator(hass, "no-such-entry") is None


def test_there_is_no_aggregator_before_the_location_stores_one(hass):
    """The domain bucket exists but the aggregator map does not yet."""
    hass.data[DOMAIN] = {}
    assert location_aggregator(hass, "no-such-entry") is None
    hass.data[DOMAIN][DATA_AGGREGATOR] = {}
    assert location_aggregator(hass, "no-such-entry") is None


async def test_the_running_aggregator_is_found_by_its_location_id(hass):
    """And the lookup really returns the location's own aggregator."""
    location = await _setup_two_receivers(hass)
    assert (
        location_aggregator(hass, location.entry_id)
        is hass.data[DOMAIN][DATA_AGGREGATOR][location.entry_id]
    )


async def test_a_receiver_that_never_received_the_device_cannot_vouch(hass):
    """No ``last_seen`` at all is not "fresh", however healthy the socket is."""
    location = await _setup_two_receivers(hass)
    entity = _merged_entity(hass, location)
    for coordinator in _coordinators(hass, location):
        coordinator.last_seen.pop(_DEVICE_KEY, None)

    assert entity.available is False


async def test_never_expire_still_needs_a_connected_receiver(hass):
    """The never-expire exemption is from silence, not from the transport.

    An event-driven device never expires on silence, but a receiver with its
    socket down receives nothing at all, so it cannot vouch -- and with no other
    receiver the merged device is unavailable. The exemption itself is unchanged:
    reconnect the receiver and the same stale timestamp vouches again.
    """
    location = build_receiver_entry(
        availability_timeout=0,
        devices={_DEVICE_KEY: {CONF_MODEL: _MODEL, DEVICE_FIELDS: ["temperature_C"]}},
        receivers=[
            build_receiver_subentry(host="attic.local"),
            build_receiver_subentry(host="garage.local"),
        ],
    )
    location.add_to_hass(hass)
    assert await hass.config_entries.async_setup(location.entry_id)
    await hass.async_block_till_done()
    entity = _merged_entity(hass, location)
    stale = dt_util.utcnow() - timedelta(days=7)
    attic, garage = _coordinators(hass, location)
    for coordinator in (attic, garage):
        coordinator.last_seen[_DEVICE_KEY] = stale
    assert entity.available is True

    for coordinator in (attic, garage):
        _set_connected(coordinator, False)
    await hass.async_block_till_done()
    assert entity.available is False

    _set_connected(garage, True)
    await hass.async_block_till_done()
    assert entity.available is True


async def test_a_receiver_edge_repaints_the_merged_entity(hass):
    """Any receiver's connection edge re-writes the merged entity's state.

    A unioned entity is built by one receiver but its availability is the OR over
    all of them, so it has to be subscribed to every receiver's availability
    signal -- not only to the one that happened to construct it.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    eid = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}:T"
    )
    with freeze_time(_NOW):
        for coordinator in (attic, garage):
            _feed(coordinator, _frame(21.4, 0))
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "21.4"

        # Taking down only the *second* receiver -- the one that did not build
        # the entity -- must still reach it, and leave it available (attic
        # vouches).
        _set_connected(garage, False)
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "21.4"

        # Now the builder goes too: nothing vouches and the entity repaints.
        _set_connected(attic, False)
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "unavailable"


async def test_the_non_building_receivers_edge_is_the_one_that_flips_it(hass):
    """The edge that matters can come from a receiver that built nothing.

    The merged entity is constructed by the location's *first* receiver, so a
    subscription to "my own receiver" alone still looks right in every case where
    that receiver is the one vouching. This is the case where it does not: the
    builder is deaf, the second receiver is the only voucher, and its edge is the
    only thing that can take the entity unavailable.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    eid = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}:T"
    )
    with freeze_time(_NOW):
        _feed(garage, _frame(21.4, 0))
        await hass.async_block_till_done()
        # Only garage has received it; attic holds nothing but its startup baseline,
        # which is cleared here so garage is unambiguously the sole voucher.
        attic.last_seen.pop(_DEVICE_KEY, None)
        assert hass.states.get(eid).state == "21.4"

        _set_connected(garage, False)
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "unavailable"


async def test_a_link_entity_reads_only_its_own_receiver(hass):
    """``RSSI Attic`` goes unavailable with Attic, whatever Garage still receives.

    The merged OR would be wrong here: another receiver hearing the sensor says
    nothing about whether *this* receiver's signal reading is current.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0))
        _feed(garage, _frame(21.4, 1, rssi=-89.0))
        await hass.async_block_till_done()

    rssi_eid = _enable_link_entity(hass, location, "rssi", 0)
    await _reload_after_enable(hass)
    attic, garage = _coordinators(hass, location)
    entity = hass.data["entity_components"]["sensor"].get_entity(rssi_eid)

    now = dt_util.utcnow()
    for coordinator in (attic, garage):
        coordinator.last_seen[_DEVICE_KEY] = now
    assert entity.available is True

    _set_connected(attic, False)
    await hass.async_block_till_done()
    assert entity.available is False


# --------------------------------------------------------------------------- #
# Per-receiver signal entities on the merged device.                          #
# --------------------------------------------------------------------------- #
def _link_entries(hass, location, object_suffix: str):
    """Return the device's registry entries for one link field, per receiver."""
    ent_reg = er.async_get(hass)
    return [
        ent_reg.async_get(
            ent_reg.async_get_entity_id(
                "sensor",
                DOMAIN,
                f"{location.entry_id}:{_DEVICE_KEY}:"
                f"{receiver_id(location, index)}:{object_suffix}",
            )
            or ""
        )
        for index in (0, 1)
    ]


def _enable_link_entity(hass, location, object_suffix: str, index: int) -> str:
    """Enable one receiver's link entity (they all ship disabled) and return its id."""
    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id(
        "sensor",
        DOMAIN,
        f"{location.entry_id}:{_DEVICE_KEY}:"
        f"{receiver_id(location, index)}:{object_suffix}",
    )
    assert eid is not None
    ent_reg.async_update_entity(eid, disabled_by=None)
    return eid


async def _reload_after_enable(hass) -> None:
    """Let the debounced reload that an enable schedules actually run."""
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RELOAD_AFTER_UPDATE_DELAY + 1)
    )
    await hass.async_block_till_done()


async def test_one_signal_entity_per_sensor_and_receiver(hass):
    """``rssi`` / ``snr`` / ``last_seen`` are one entity per (sensor x receiver).

    All of them on the ONE merged device -- that is the point: a user reads
    "strong at Attic, weak at Garage" off a single device page.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0, snr=11.5))
        _feed(garage, _frame(21.4, 1, rssi=-89.0, snr=3.0))
        await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    merged = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}"), location.entry_id
    )
    assert merged is not None
    for object_suffix in ("rssi", "snr", "last_seen"):
        entries = _link_entries(hass, location, object_suffix)
        assert all(entry is not None for entry in entries), object_suffix
        assert {entry.device_id for entry in entries} == {merged.id}


async def test_a_signal_entity_names_its_receiver_and_never_gains_a_suffix(hass):
    """The receiver lives in the NAME, so HA never mints a ``_2`` entity_id.

    ``_attr_has_entity_name`` plus the descriptor's own name would give one
    merged device two entities both called "RSSI"; Home Assistant resolves that
    collision by appending ``_2`` to the second entity_id, which is the issue
    #132 failure mode. The unique_id carries the subentry id; the name carries
    the receiver the user actually recognises.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0, snr=11.5))
        _feed(garage, _frame(21.4, 1, rssi=-89.0, snr=3.0))
        await hass.async_block_till_done()

    titles = [receiver_subentry(location, index).title for index in (0, 1)]
    for object_suffix, base in (
        ("rssi", "RSSI"),
        ("snr", "SNR"),
        ("last_seen", "Last seen"),
    ):
        entries = _link_entries(hass, location, object_suffix)
        assert [entry.original_name for entry in entries] == [
            f"{base} {title}" for title in titles
        ]

    ent_reg = er.async_get(hass)
    device_entity_ids = [
        entry.entity_id
        for entry in er.async_entries_for_config_entry(ent_reg, location.entry_id)
        if f":{_DEVICE_KEY}:" in entry.unique_id
    ]
    assert device_entity_ids
    assert not [eid for eid in device_entity_ids if eid.endswith(("_2", "_3"))]


async def test_signal_entities_carry_no_subentry_and_warn_about_no_move(hass, caplog):
    """They are about a receiver but hang off the merged device: no subentry id.

    Adding them under their receiver's subentry would give one device entities
    from two subentries, which silently moves the device today and raises in HA
    Core 2027.8. Home Assistant reports that move as it happens, so the absence
    of the report after both receivers have loaded is the assertion.
    """
    caplog.set_level(logging.DEBUG)
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0))
        _feed(garage, _frame(21.4, 1, rssi=-89.0))
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    device_entries = [
        entry
        for entry in er.async_entries_for_config_entry(ent_reg, location.entry_id)
        if f":{_DEVICE_KEY}:" in entry.unique_id
    ]
    assert device_entries
    assert {entry.config_subentry_id for entry in device_entries} == {None}
    assert (
        "assigns an existing device to a different config subentry" not in caplog.text
    )


async def test_signal_entities_ship_disabled_by_default(hass):
    """A location does not pay sensors x receivers x 2 entities to see coverage."""
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0, snr=11.5))
        _feed(garage, _frame(21.4, 1, rssi=-89.0, snr=3.0))
        await hass.async_block_till_done()

    for object_suffix in ("rssi", "snr"):
        for entry in _link_entries(hass, location, object_suffix):
            assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_one_receivers_frame_never_moves_the_others_signal_reading(hass):
    """Garage hearing the sensor badly must not rewrite Attic's RSSI.

    The union would publish whichever receiver won the debounce race, which for a
    signal measurement is a number that belongs to neither of them.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    # The entities only exist once the field has been seen, so hear it first,
    # then enable both (they ship disabled) and let the reload rebuild them.
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0))
        _feed(garage, _frame(21.4, 1, rssi=-89.0))
        await hass.async_block_till_done()
    attic_eid = _enable_link_entity(hass, location, "rssi", 0)
    garage_eid = _enable_link_entity(hass, location, "rssi", 1)
    await _reload_after_enable(hass)
    attic, garage = _coordinators(hass, location)

    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 10, rssi=-62.0))
        await hass.async_block_till_done()
        assert hass.states.get(attic_eid).state == "-62.0"

        # The same transmission, received by the other receiver a second later and
        # much more weakly. The temperature is deduped away; the RSSI is not
        # shared.
        _feed(garage, _frame(21.4, 11, rssi=-89.0))
        await hass.async_block_till_done()

        assert hass.states.get(attic_eid).state == "-62.0"
        assert hass.states.get(garage_eid).state == "-89.0"


async def test_the_coverage_map_serves_the_comparison_without_any_entity(hass):
    """The A-vs-B comparison comes off aggregator state, entities all disabled.

    That is what lets the panel show "received by Attic (-62) / Garage (-89)" while
    ``rssi`` / ``snr`` stay disabled-by-default.
    """
    location = await _setup_two_receivers(hass)
    aggregator = hass.data[DOMAIN][DATA_AGGREGATOR][location.entry_id]
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0, snr=11.5))
        _feed(garage, _frame(21.4, 1, rssi=-89.0, snr=3.0))
        await hass.async_block_till_done()

        coverage = {
            entry.receiver_id: entry for entry in aggregator.coverage(_DEVICE_KEY)
        }
        assert set(coverage) == {receiver_id(location, 0), receiver_id(location, 1)}
        assert coverage[receiver_id(location, 0)].rssi == -62.0
        assert coverage[receiver_id(location, 0)].snr == 11.5
        assert coverage[receiver_id(location, 1)].rssi == -89.0
        assert coverage[receiver_id(location, 1)].snr == 3.0
        assert all(entry.last_seen is not None for entry in coverage.values())
        assert all(entry.connected and entry.vouches for entry in coverage.values())

    # Nothing had to be enabled for any of that.
    for object_suffix in ("rssi", "snr"):
        for entry in _link_entries(hass, location, object_suffix):
            assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_coverage_distinguishes_offline_from_deaf(hass):
    """ "Garage is down" and "Garage cannot receive this sensor" are different answers."""
    location = await _setup_two_receivers(hass)
    aggregator = hass.data[DOMAIN][DATA_AGGREGATOR][location.entry_id]
    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0))
        await hass.async_block_till_done()

    # Garage is up but has never received this sensor: connected, does not vouch.
    by_receiver = {e.receiver_id: e for e in aggregator.coverage(_DEVICE_KEY)}
    deaf = by_receiver[receiver_id(location, 1)]
    assert (deaf.connected, deaf.vouches, deaf.last_seen, deaf.rssi) == (
        True,
        False,
        None,
        None,
    )

    # Attic then goes offline: it still has a coverage record, but cannot vouch.
    _set_connected(attic, False)
    await hass.async_block_till_done()
    offline = {e.receiver_id: e for e in aggregator.coverage(_DEVICE_KEY)}[
        receiver_id(location, 0)
    ]
    assert offline.connected is False
    assert offline.vouches is False
    assert offline.rssi == -62.0


async def test_a_watchdog_repaint_does_not_move_the_coverage_timestamp(hass):
    """A re-paint carries the cached frame, so it is not the receiver hearing it."""
    location = await _setup_two_receivers(hass)
    aggregator = hass.data[DOMAIN][DATA_AGGREGATOR][location.entry_id]
    attic, _garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0))
        await hass.async_block_till_done()
    heard_at = aggregator.coverage(_DEVICE_KEY)[0].last_seen

    later = datetime.fromisoformat(_NOW) + timedelta(seconds=6000)
    with freeze_time(later):
        await attic._async_watchdog(later)
        await hass.async_block_till_done()

    assert aggregator.coverage(_DEVICE_KEY)[0].last_seen == heard_at


async def test_forgetting_a_device_drops_its_coverage(hass):
    """A device the user removed leaves no coverage behind either."""
    location = await _setup_two_receivers(hass)
    aggregator = hass.data[DOMAIN][DATA_AGGREGATOR][location.entry_id]
    attic, _garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0))
        await hass.async_block_till_done()
    assert aggregator._coverage

    aggregator.forget_device(_DEVICE_KEY)
    assert aggregator._coverage == {}


# --------------------------------------------------------------------------- #
# Removing a receiver from a location.                                        #
# --------------------------------------------------------------------------- #
_GARAGE_ONLY_KEY = "Acurite-606TX-77"


async def _setup_for_removal(hass):
    """Two receivers; one sensor both hear, one only the garage ever receives."""
    location = build_receiver_entry(
        availability_timeout=600,
        devices={
            _DEVICE_KEY: {CONF_MODEL: _MODEL, DEVICE_FIELDS: ["temperature_C"]},
            _GARAGE_ONLY_KEY: {CONF_MODEL: _MODEL, DEVICE_FIELDS: ["temperature_C"]},
        },
        receivers=[
            build_receiver_subentry(host="attic.local"),
            build_receiver_subentry(host="garage.local"),
        ],
    )
    location.add_to_hass(hass)
    assert await hass.config_entries.async_setup(location.entry_id)
    await hass.async_block_till_done()

    attic, garage = _coordinators(hass, location)
    with freeze_time(_NOW):
        _feed(attic, _frame(21.4, 0, rssi=-62.0, snr=11.5))
        _feed(garage, _frame(21.4, 1, rssi=-89.0, snr=3.0))
        garage._client._process_event(
            {
                "time": _at(0),
                "model": _MODEL,
                "id": 77,
                "temperature_C": 18.0,
                "rssi": -91.0,
            }
        )
        await hass.async_block_till_done()
    return location


async def _remove_garage(hass, location) -> str:
    """Delete the second receiver subentry and let the reload settle."""
    garage_id = receiver_id(location, 1)
    hass.config_entries.async_remove_subentry(location, garage_id)
    await hass.async_block_till_done()
    return garage_id


async def test_removing_a_receiver_takes_its_own_device_and_entities(hass):
    """The receiver device, its radio controls, noise and connectivity go with it."""
    location = await _setup_for_removal(hass)
    garage_scope = f"{location.entry_id}:receiver:{receiver_id(location, 1)}"
    dev_reg = dr.async_get(hass)
    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, garage_scope), location.entry_id
        )
        is not None
    )

    garage_id = await _remove_garage(hass, location)

    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, garage_scope), location.entry_id
        )
        is None
    )
    ent_reg = er.async_get(hass)
    assert not [
        entry
        for entry in er.async_entries_for_config_entry(ent_reg, location.entry_id)
        if entry.unique_id.startswith(f"{garage_scope}:")
    ]
    assert garage_id not in hass.data[DOMAIN]


async def test_removing_a_receiver_takes_its_signal_entities_off_merged_devices(hass):
    """Its per-receiver ``rssi`` / ``snr`` / ``last_seen`` go too -- the other's stay.

    Those entities carry no ``config_subentry_id`` (they hang off the merged
    device), so Home Assistant's subentry sweep cannot see them; without the
    integration removing them by identity they would survive as permanently
    unavailable orphans of a server that is gone.
    """
    location = await _setup_for_removal(hass)
    attic_id = receiver_id(location, 0)
    garage_id = await _remove_garage(hass, location)

    ent_reg = er.async_get(hass)
    survivors = {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(ent_reg, location.entry_id)
    }
    for device_key in (_DEVICE_KEY, _GARAGE_ONLY_KEY):
        for object_suffix in ("rssi", "snr", "last_seen"):
            assert (
                f"{location.entry_id}:{device_key}:{garage_id}:{object_suffix}"
                not in survivors
            )
    assert f"{location.entry_id}:{_DEVICE_KEY}:{attic_id}:last_seen" in survivors


async def test_removing_a_receiver_keeps_merged_devices_and_their_history(hass):
    """The sensor's device, entity_id and recorded value all survive untouched."""
    location = await _setup_for_removal(hass)
    ent_reg = er.async_get(hass)
    temperature_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}:T"
    )
    assert hass.states.get(temperature_eid).state == "21.4"

    await _remove_garage(hass, location)

    dev_reg = dr.async_get(hass)
    merged = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}"), location.entry_id
    )
    assert merged is not None
    # Same entity, same entity_id -- which is what carries the recorder history.
    assert (
        ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}:T"
        )
        == temperature_eid
    )
    assert hass.states.get(temperature_eid).state == "21.4"


async def test_removing_a_receiver_recomputes_availability_over_the_rest(hass):
    """The surviving receiver alone decides; the sensor stays available."""
    location = await _setup_for_removal(hass)
    await _remove_garage(hass, location)

    attic = hass.data[DOMAIN][receiver_id(location, 0)]
    attic.last_seen[_DEVICE_KEY] = dt_util.utcnow()
    assert _merged_entity(hass, location).available is True

    _set_connected(attic, False)
    await hass.async_block_till_done()
    assert _merged_entity(hass, location).available is False


async def test_a_device_only_the_removed_receiver_heard_goes_unavailable_not_deleted(
    hass,
):
    """Removing it stays an explicit user action, exactly like any other device."""
    location = await _setup_for_removal(hass)
    await _remove_garage(hass, location)

    dev_reg = dr.async_get(hass)
    orphan = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{location.entry_id}:{_GARAGE_ONLY_KEY}"), location.entry_id
    )
    assert orphan is not None, "the device must survive its only receiver"
    assert _GARAGE_ONLY_KEY in location.data[CONF_DEVICES]

    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{location.entry_id}:{_GARAGE_ONLY_KEY}:T"
    )
    assert eid is not None
    entity = hass.data["entity_components"]["sensor"].get_entity(eid)

    # The surviving receiver baselines a missing last_seen to "now" on add
    # ("restore then time out"), so the honest answer arrives once that elapses:
    # no remaining receiver has ever received this sensor, so none can vouch.
    with freeze_time(dt_util.utcnow() + timedelta(seconds=6000)):
        assert entity.available is False


# --------------------------------------------------------------------------- #
# The union add-device page: one merged candidate list for the location.      #
# --------------------------------------------------------------------------- #
# A device the location has *not* adopted, so its frames become candidates
# instead of taking the adopted path the tests above exercise.
_CANDIDATE_KEY = "Acurite-606TX-55"


def _dt(offset_seconds: float) -> datetime:
    """Return :data:`_NOW` shifted by ``offset_seconds``, as a datetime."""
    return datetime.fromisoformat(_NOW) + timedelta(seconds=offset_seconds)


def _heard(
    coordinator,
    *,
    key: str = _CANDIDATE_KEY,
    model: str = _MODEL,
    fields: dict | None = None,
    is_replay: bool = False,
    event_time: datetime | None = None,
) -> None:
    """Feed one already-classified frame into a receiver's Home Assistant side.

    The candidate tests drive ``_on_client_event`` rather than the client's
    ``_process_event`` (which the union tests above use) because what they are
    about is the *verdict*: whether a frame this receiver classified as a replay,
    as pre-connection backlog, or as live becomes a candidate at all. Handing the
    verdict in makes each case one line instead of a reconstruction of the wire
    conditions that would produce it -- the same seam ``tests/test_pending_devices``
    uses for the single-receiver half of this contract.
    """
    coordinator._on_client_event(
        NormalizedEvent(
            device_key=key,
            model=model,
            fields={"temperature_C": 21.4} if fields is None else fields,
            is_replay=is_replay,
            event_time=event_time,
        )
    )


async def test_one_sensor_received_by_both_receivers_is_one_candidate(hass):
    """Two receivers, one sensor, ONE row to approve.

    The regression the union exists to prevent: without the merge the same
    physical sensor queues once per receiver and has to be approved twice, which
    is exactly the "approve a sensor once" the location model promises.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    with freeze_time(_NOW):
        _heard(attic)
    with freeze_time(_dt(1)):
        _heard(garage)

    # Each receiver really did record its own sighting: the merge is a view over
    # both, not one receiver winning the race to record.
    assert set(attic.pending) == {_CANDIDATE_KEY}
    assert set(garage.pending) == {_CANDIDATE_KEY}

    candidates = merged_candidates(hass, location)
    assert [candidate.key for candidate in candidates] == [_CANDIDATE_KEY]
    # The location received it twice, which is the count a user judges a candidate
    # by -- a real sensor keeps checking in, a bad decode is received once.
    assert candidates[0].record.count == 2
    assert candidates[0].record.first_seen == _dt(0)
    assert candidates[0].record.last_seen == _dt(1)


async def test_the_merged_row_shows_the_last_received_frame(hass):
    """Last-received-wins, by arrival -- not by the frame's own timestamp.

    ``event_time`` is stamped by the decoding *host*, so two receivers disagree
    by their clock skew; here the attic's clock runs a minute fast. A preview
    that trusted it would show the older reading indefinitely. The rule is
    deliberately the simple one -- the freshest sample to arrive, with none of
    the debounce the *adopted* value path needs -- so the card tracks the sensor.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    with freeze_time(_NOW):
        _heard(attic, fields={"temperature_C": 21.4}, event_time=_dt(60))
    with freeze_time(_dt(1)):
        _heard(garage, fields={"temperature_C": 22.9}, event_time=_dt(0))

    candidate = merged_candidate(hass, location, _CANDIDATE_KEY)
    assert candidate is not None
    assert candidate.record.event.fields == {"temperature_C": 22.9}
    assert candidate.record.fields["temperature_C"] == 22.9


async def test_the_merged_row_accumulates_what_either_receiver_heard(hass):
    """A sensor that splits its readings across frames shows the whole device.

    An Acurite-5n1 sends wind in one message and rain in another, and radio being
    what it is, the two can reach different receivers. The row unions both halves
    -- so the user sees the whole device before deciding, and adoption creates all
    of its entities at once -- while a field both receivers report keeps the
    newest value.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    with freeze_time(_NOW):
        _heard(attic, fields={"wind_avg_km_h": 12.0, "temperature_C": 21.4})
    with freeze_time(_dt(1)):
        _heard(garage, fields={"rain_mm": 3.5, "temperature_C": 22.9})

    candidate = merged_candidate(hass, location, _CANDIDATE_KEY)
    assert candidate is not None
    assert candidate.record.fields == {
        "wind_avg_km_h": 12.0,
        "rain_mm": 3.5,
        "temperature_C": 22.9,
    }


async def test_the_merged_row_records_which_receivers_heard_it(hass):
    """The row carries its coverage, so the page can show it before adoption.

    The aggregator's ``coverage`` map cannot answer this: it is fed by the
    per-device dispatch, and a device nobody has adopted dispatches nothing. So
    the candidate's own records supply it -- and only the receivers that actually
    received the sensor appear, because before adoption that is the whole question.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    attic_id, garage_id = receiver_id(location, 0), receiver_id(location, 1)

    with freeze_time(_NOW):
        _heard(attic, fields={"temperature_C": 21.4, "rssi": -62.0, "snr": 11.5})
        _heard(garage, fields={"temperature_C": 21.4, "rssi": -89.0, "snr": 3.0})
        # A second sensor only the attic is in range of.
        _heard(attic, key="Bresser-3CH-7", model="Bresser-3CH")

    candidate = merged_candidate(hass, location, _CANDIDATE_KEY)
    assert candidate is not None
    assert candidate.receivers == (attic_id, garage_id)
    coverage = {entry.receiver_id: entry for entry in candidate.coverage}
    # The per-receiver signal detail the merged row itself cannot show.
    assert coverage[attic_id].rssi == -62.0
    assert coverage[attic_id].snr == 11.5
    assert coverage[garage_id].rssi == -89.0
    assert coverage[garage_id].snr == 3.0
    assert all(entry.connected for entry in candidate.coverage)
    assert all(entry.last_seen == _dt(0) for entry in candidate.coverage)

    attic_only = merged_candidate(hass, location, "Bresser-3CH-7")
    assert attic_only is not None
    assert attic_only.receivers == (attic_id,)


async def test_a_replayed_or_backlog_frame_never_joins_the_merge(hass):
    """The reconnect gate is per receiver, and it runs before the merge.

    Each receiver classifies its own frames against its own connection, so the
    gate cannot move to the merge without losing what it is measuring. Applying
    it first is what keeps one receiver's reconnect from refilling the *location's*
    candidate list with devices that were received and dismissed hours ago.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    garage._connection_time = _dt(0)

    with freeze_time(_NOW):
        _heard(attic, fields={"temperature_C": 21.4}, event_time=_dt(5))
        # The garage reconnects and the server re-broadcasts its backlog: frames
        # stamped well before this connection came up, plus the library's own
        # replay verdict on the stale ones.
        _heard(garage, fields={"temperature_C": 99.0}, event_time=_dt(-600))
        _heard(garage, fields={"temperature_C": 98.0}, is_replay=True)

    assert garage.pending == {}
    candidate = merged_candidate(hass, location, _CANDIDATE_KEY)
    assert candidate is not None
    # One row, from the receiver that genuinely received it, with none of the
    # backlog's values anywhere in it.
    assert candidate.receivers == (receiver_id(location, 0),)
    assert candidate.record.count == 1
    assert candidate.record.fields == {"temperature_C": 21.4}


async def test_the_candidate_cap_is_enforced_on_the_merged_list(hass):
    """N receivers cannot hold N times as many candidates as one.

    The cap exists because every candidate is rendered into the payload pushed to
    every open panel, and 433 MHz mints spurious keys indefinitely. That bound has
    to be on the merged list the user is actually offered: here neither receiver
    ever reaches its own ceiling, and without the merged pass the location would
    be holding 601 of them.

    The eviction is coldest-first across the merge, so the key both receivers
    received *first* goes, and it goes from **both** of their maps -- leaving it in
    one would put the row straight back on the next merge.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    each = 300

    _heard(attic, key="Shared-Cold-1", model="Noise")
    _heard(garage, key="Shared-Cold-1", model="Noise")
    for index in range(each):
        _heard(attic, key=f"Noise-a{index}", model="Noise")
    for index in range(each):
        _heard(garage, key=f"Noise-b{index}", model="Noise")

    assert len(merged_candidates(hass, location)) == MAX_PENDING_CANDIDATES
    # Neither receiver is anywhere near its own ceiling, so the merged pass is
    # the only thing that can have done this.
    assert len(attic.pending) < MAX_PENDING_CANDIDATES
    assert len(garage.pending) < MAX_PENDING_CANDIDATES
    assert "Shared-Cold-1" not in attic.pending
    assert "Shared-Cold-1" not in garage.pending
    # The warm end is untouched: the newest arrival is the one a user is most
    # likely waiting to see.
    assert f"Noise-b{each - 1}" in garage.pending


async def test_an_evicted_candidate_says_so_in_the_log(hass, caplog):
    """The merged eviction names the key it dropped and the cap that forced it.

    A candidate that disappears from the add-device page with nothing in the log
    is indistinguishable from one that was never received, which is the first thing
    a "my sensor never shows up" report has to rule out -- and the *merged*
    eviction is the one with no other explanation, because neither receiver is
    anywhere near its own ceiling here. The record is matched to
    ``aggregator.py`` for exactly that reason: each coordinator logs the same
    sentence for its own per-receiver cap.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.rtl_433")
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    each = 300

    _heard(attic, key="Shared-Cold-1", model="Noise")
    _heard(garage, key="Shared-Cold-1", model="Noise")
    for index in range(each):
        _heard(attic, key=f"Noise-a{index}", model="Noise")
    for index in range(each):
        _heard(garage, key=f"Noise-b{index}", model="Noise")

    assert "Shared-Cold-1" not in merged_candidates(hass, location)
    assert (
        "rtl_433 dropping the coldest candidate Shared-Cold-1 "
        f"(over the {MAX_PENDING_CANDIDATES} key cap)"
    ) in [
        record.getMessage()
        for record in caplog.records
        if record.filename == "aggregator.py"
    ]


async def test_the_candidate_list_announces_on_one_location_scoped_signal(hass):
    """One list, one signal -- whichever receiver received the device.

    The panel subscribes once per location, so both receivers announce on the
    *location's* signal rather than each on its own: a subscriber has exactly one
    thing to listen to, and never has to work out which receiver's list a signal
    referred to.

    A repeat sighting stays silent, as it always has: a busy receiver decodes
    constantly, and dispatching per frame would push a whole list down every open
    socket so one count could tick up.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    announced: list[str] = []
    unsub = async_dispatcher_connect(
        hass,
        signal_pending_update(location.entry_id),
        lambda: announced.append("location"),
    )

    with freeze_time(_NOW):
        _heard(attic)
        # The same receiver hearing it again changes no membership, so it says
        # nothing at all.
        _heard(attic)
        await hass.async_block_till_done()
    assert announced == ["location"]

    # The second receiver's first sighting changes what the merged row says
    # (it now names two receivers), and is announced on the same one signal.
    with freeze_time(_dt(1)):
        _heard(garage)
        await hass.async_block_till_done()
    assert announced == ["location", "location"]

    unsub()


async def test_a_row_takes_its_model_from_whichever_receiver_decoded_one(hass):
    """A frame that decodes without a model does not leave the row unnamed.

    ``model`` is what the user reads first, and a row that showed nothing because
    the last frame to arrive happened to omit it would be unjudgeable.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    with freeze_time(_NOW):
        _heard(attic, model=_MODEL)
    with freeze_time(_dt(1)):
        _heard(garage, model="")

    candidate = merged_candidate(hass, location, _CANDIDATE_KEY)
    assert candidate is not None
    assert candidate.record.model == _MODEL

    # When nobody decoded a model there is nothing to fall back to, and the row
    # goes out unnamed rather than carrying a placeholder the panel would render
    # as a model name.
    with freeze_time(_dt(2)):
        _heard(attic, key="Unknown-1", model="")
        _heard(garage, key="Unknown-1", model="")
    unnamed = merged_candidate(hass, location, "Unknown-1")
    assert unnamed is not None
    assert unnamed.record.model == ""


async def test_candidates_are_offered_most_recently_discovered_first(hass):
    """One order for the whole location, and it does not shuffle under the cursor.

    The panel draws a card per candidate and re-renders every few seconds, and
    the options form renders the same list, so the order is decided once -- by
    when the *location* first received each device, which (unlike last-seen) does
    not move every time a sensor transmits. The key breaks a tie so two devices
    first received in the same instant still have a stable order.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    with freeze_time(_NOW):
        _heard(attic, key="Bresser-3CH-1", model="Bresser-3CH")
    with freeze_time(_dt(1)):
        _heard(garage, key="Bresser-3CH-2", model="Bresser-3CH")
    with freeze_time(_dt(2)):
        # Received in the same instant by different receivers: the tie-break is the
        # key, not whichever map was iterated first.
        _heard(attic, key="Bresser-3CH-4", model="Bresser-3CH")
        _heard(garage, key="Bresser-3CH-3", model="Bresser-3CH")

    assert [candidate.key for candidate in merged_candidates(hass, location)] == [
        "Bresser-3CH-4",
        "Bresser-3CH-3",
        "Bresser-3CH-2",
        "Bresser-3CH-1",
    ]


async def test_one_sensor_heard_twice_is_one_candidate_against_the_cap(hass):
    """The bound counts rows, so overlap does not spend it twice.

    Two receivers that hear the same 300 sensors are offering 300 candidates, not
    600 -- the whole reason the cap is applied to the merge rather than to the
    sum of the maps.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    shared = 300

    for index in range(shared):
        _heard(attic, key=f"Noise-{index}", model="Noise")
        _heard(garage, key=f"Noise-{index}", model="Noise")

    # The per-receiver maps sum to more than the cap, and nothing is evicted.
    assert len(attic.pending) + len(garage.pending) > MAX_PENDING_CANDIDATES
    assert len(merged_candidates(hass, location)) == shared
    assert "Noise-0" in attic.pending


async def test_clearing_counts_and_clears_merged_rows(hass):
    """Clearing a list of rows clears rows, and reports what the user saw go.

    Counting per-receiver records would tell a user with two receivers that they
    had just cleared twice as many devices as the page was showing.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    with freeze_time(_NOW):
        _heard(attic)
        _heard(garage)
        _heard(attic, key="Bresser-3CH-7", model="Bresser-3CH")

    assert clear_pending(hass, location) == 2
    assert attic.pending == {}
    assert garage.pending == {}
    assert merged_candidates(hass, location) == []


# --------------------------------------------------------------------------- #
# Location-scoped adoption: one decision, every receiver.                      #
# --------------------------------------------------------------------------- #
def _merged_devices(hass, location, device_key) -> list:
    """Return every registry device carrying one merged device's identifier.

    A list rather than a lookup because the assertion worth making is *how many*
    there are: adopting a sensor two receivers both hear must produce one device,
    and a second one is the regression.
    """
    identifier = (DOMAIN, f"{location.entry_id}:{device_key}")
    return [
        device
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), location.entry_id
        )
        if identifier in device.identifiers
    ]


def _entity_ids_for(hass, location, device_key) -> list[str]:
    """Return the *merged* entity ids built for one device, in registry order.

    Only the receiver-agnostic ones: a merged field entity's ``unique_id`` is
    three segments (``{entry}:{device_key}:{suffix}``) while the per-receiver
    link entities carry a fourth, and counting those would make "one entity per
    mapped field" read as one per field per receiver.
    """
    prefix = f"{location.entry_id}:{device_key}:"
    return [
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.unique_id.startswith(prefix) and entry.unique_id.count(":") == 2
    ]


async def test_adopting_once_adds_the_device_for_the_whole_location(hass):
    """One click, one device -- not one per receiver that received the sensor."""
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    # The ordinary case: both receivers decode the same transmission.
    with freeze_time(_NOW):
        _heard(attic, fields={"temperature_C": 21.4})
    with freeze_time(_dt(1)):
        _heard(garage, fields={"temperature_C": 21.4})

    with freeze_time(_dt(2)):
        result = await async_adopt_devices(hass, location, [_CANDIDATE_KEY])
        await hass.async_block_till_done()
        # Seeded from what was actually received, so the device arrives carrying a
        # real reading rather than an unavailable placeholder waiting for the
        # next transmission.
        entity_ids = _entity_ids_for(hass, location, _CANDIDATE_KEY)
        assert len(entity_ids) == 1
        assert hass.states.get(entity_ids[0]).state == "21.4"

    assert result.applied == [_CANDIDATE_KEY]
    assert result.skipped == []
    # Gone from the candidate list of every receiver, so it cannot be offered
    # (or approved) a second time.
    assert attic.pending == {}
    assert garage.pending == {}
    assert merged_candidates(hass, location) == []
    assert _CANDIDATE_KEY in attic.adopted
    assert _CANDIDATE_KEY in garage.adopted

    # Exactly one device, with exactly one entity per mapped field -- and no
    # ``_2`` suffix, which is what a second receiver minting a duplicate
    # ``unique_id`` would leave behind.
    assert len(_merged_devices(hass, location, _CANDIDATE_KEY)) == 1
    assert not entity_ids[0].endswith("_2")
    assert _CANDIDATE_KEY in location.data[CONF_DEVICES]


async def test_adopting_seeds_from_the_merged_record(hass):
    """The device is built from what the *location* received, not one receiver.

    A field only the garage ever decoded still creates its entity, because the
    record adoption seeds from is the merged one the user was looking at.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    with freeze_time(_NOW):
        _heard(attic, fields={"temperature_C": 21.4})
    with freeze_time(_dt(1)):
        _heard(garage, fields={"humidity": 61})

    await async_adopt_devices(hass, location, [_CANDIDATE_KEY])
    await hass.async_block_till_done()

    stored = location.data[CONF_DEVICES][_CANDIDATE_KEY]
    assert set(stored[DEVICE_FIELDS]) == {"temperature_C", "humidity"}
    assert len(_entity_ids_for(hass, location, _CANDIDATE_KEY)) == 2


async def test_adopting_reaches_a_receiver_that_never_received_the_device(hass):
    """The deaf receiver adopts the key too, and stops re-queueing the sensor.

    Otherwise the first frame it ever decodes for an already-added device would
    be treated as a brand-new sighting and put the device back on the page the
    user added it from.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    with freeze_time(_NOW):
        _heard(attic, fields={"temperature_C": 21.4})
    await async_adopt_devices(hass, location, [_CANDIDATE_KEY])
    await hass.async_block_till_done()

    assert _CANDIDATE_KEY in garage.adopted
    # No liveness is invented for a receiver that has not received it: a faked
    # last_seen would let the garage vouch for a sensor it cannot receive.
    assert _CANDIDATE_KEY not in garage.last_seen

    with freeze_time(_dt(1)):
        _heard(garage, fields={"temperature_C": 22.9})
    assert garage.pending == {}
    assert garage.devices[_CANDIDATE_KEY].fields == {"temperature_C": 22.9}


async def test_adopting_a_key_no_receiver_offers_is_skipped(hass):
    """A stale click reports the miss rather than inventing a device."""
    location = await _setup_two_receivers(hass)

    result = await async_adopt_devices(hass, location, ["Ghost-Device-1"])
    await hass.async_block_till_done()

    assert result.applied == []
    assert result.skipped == ["Ghost-Device-1"]
    assert _merged_devices(hass, location, "Ghost-Device-1") == []
    assert "Ghost-Device-1" not in location.data.get(CONF_DEVICES, {})


async def test_ignoring_once_hides_the_device_from_every_receiver(hass):
    """ "I do not want my neighbour's sensor" is not a per-server statement."""
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    with freeze_time(_NOW):
        _heard(attic)
        _heard(garage)

    result = await async_ignore_devices(hass, location, [_CANDIDATE_KEY])
    await hass.async_block_till_done()

    assert result.applied == [_CANDIDATE_KEY]
    assert location.data[CONF_IGNORED_DEVICES] == [_CANDIDATE_KEY]
    assert merged_candidates(hass, location) == []
    for coordinator in (attic, garage):
        assert _CANDIDATE_KEY in coordinator.ignored
        assert coordinator.pending == {}

    # The next transmission is dropped whichever receiver decodes it -- one
    # receiver still offering the row would put it straight back on the page.
    with freeze_time(_dt(1)):
        _heard(garage)
        _heard(attic)
    assert merged_candidates(hass, location) == []


async def test_unignoring_once_offers_the_device_again_everywhere(hass):
    """Un-ignoring is location-wide too, and takes effect on the next frame."""
    location = await _setup_two_receivers(hass, ignored_devices=[_CANDIDATE_KEY])
    attic, garage = _coordinators(hass, location)
    assert _CANDIDATE_KEY in attic.ignored
    assert _CANDIDATE_KEY in garage.ignored

    result = await async_unignore_devices(hass, location, [_CANDIDATE_KEY])
    await hass.async_block_till_done()

    assert result.applied == [_CANDIDATE_KEY]
    assert location.data[CONF_IGNORED_DEVICES] == []
    for coordinator in (attic, garage):
        assert _CANDIDATE_KEY not in coordinator.ignored

    with freeze_time(_NOW):
        _heard(garage)
    assert [candidate.key for candidate in merged_candidates(hass, location)] == [
        _CANDIDATE_KEY
    ]


async def test_an_adopted_device_cannot_be_ignored_anywhere_in_the_location(hass):
    """Ignoring an adopted device is a contradiction, so it is reported, not stored."""
    location = await _setup_two_receivers(hass)

    result = await async_ignore_devices(hass, location, [_DEVICE_KEY])
    await hass.async_block_till_done()

    assert result.applied == []
    assert result.skipped == [_DEVICE_KEY]
    assert location.data.get(CONF_IGNORED_DEVICES, []) == []
    for coordinator in _coordinators(hass, location):
        assert _DEVICE_KEY not in coordinator.ignored


async def test_ignore_reads_both_halves_of_the_locations_adopted_answer(hass):
    """ "Is this adopted?" is the stored map *and* every receiver's live mirror.

    The two disagree in both directions for a beat: a device adopted a moment ago
    is in the mirrors before the entry write lands, and a device adopted in an
    earlier session is in the stored map before any receiver has received it this
    process. Reading only one of them would let an ignore land on an adopted
    device -- a persisted contradiction the event path would then ignore, since
    it checks ``adopted`` first.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)

    # Stored, but not in any live mirror.
    for coordinator in (attic, garage):
        coordinator.adopted.discard(_DEVICE_KEY)
    stored_only = await async_ignore_devices(hass, location, [_DEVICE_KEY])
    assert stored_only.skipped == [_DEVICE_KEY]

    # In a live mirror, but not yet stored.
    attic.adopted.add("Bresser-3CH-7")
    live_only = await async_ignore_devices(hass, location, ["Bresser-3CH-7"])
    assert live_only.skipped == ["Bresser-3CH-7"]

    assert location.data.get(CONF_IGNORED_DEVICES, []) == []


async def test_both_lists_reach_every_receiver_of_the_location(hass):
    """Adopted and ignored are the location's, so both receivers start from them.

    This is the union half of a deliberate consolidation: folding a second
    receiver in gives it the same approvals and the same ignore list as the
    first, rather than a private copy it would have to be taught again.
    """
    location = await _setup_two_receivers(hass, ignored_devices=[_CANDIDATE_KEY])

    for coordinator in _coordinators(hass, location):
        assert coordinator.adopted == {_DEVICE_KEY}
        assert coordinator.ignored == {_CANDIDATE_KEY}


async def test_ignored_wins_when_a_consolidation_puts_a_key_on_both_lists(hass):
    """A device the user explicitly hid stays hidden, even if it was also added.

    The two lists are normally disjoint -- the approval surfaces refuse to ignore
    an adopted device -- so this is the consolidation case: merging two installs
    into one location unions both lists and a device one of them added while the
    other hid it lands on both. The conservative answer wins, and un-ignoring is
    how the user changes their mind.
    """
    location = await _setup_two_receivers(hass, ignored_devices=[_DEVICE_KEY])
    attic, garage = _coordinators(hass, location)

    for coordinator in (attic, garage):
        assert _DEVICE_KEY in coordinator.ignored
        assert _DEVICE_KEY not in coordinator.adopted

    # Its frames are dropped outright: not adopted (no runtime state, no
    # dispatch) and not offered as a candidate either.
    with freeze_time(_NOW):
        _heard(attic, key=_DEVICE_KEY)
    assert attic.devices == {}
    assert merged_candidates(hass, location) == []


async def test_ignoring_a_key_that_is_also_adopted_takes_effect_without_a_reload(hass):
    """The same rule applies live, when the stored lists change under a reload.

    A consolidation writes the union into ``entry.data``; the update listener is
    what makes the losing half stop reaching Home Assistant now rather than at
    the next restart.
    """
    location = await _setup_two_receivers(hass)
    attic, garage = _coordinators(hass, location)
    assert _DEVICE_KEY in attic.adopted

    hass.config_entries.async_update_entry(
        location,
        data={**location.data, CONF_IGNORED_DEVICES: [_DEVICE_KEY]},
    )
    await hass.async_block_till_done()

    for coordinator in (attic, garage):
        assert _DEVICE_KEY in coordinator.ignored
        assert _DEVICE_KEY not in coordinator.adopted
