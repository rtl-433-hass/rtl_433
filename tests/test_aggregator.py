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
  and neither unioned nor deduped.

Frames are fed straight into each receiver's client (``_process_event``), the
same seam the rest of the suite uses, with an explicit ``time`` because the
dedup's whole rule is expressed in terms of the frame's own stamp.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from freezegun import freeze_time
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.rtl_433.aggregator import (
    _MERGE_DEBOUNCE,
    LAST_SEEN_FIELD,
    LINK_FIELD_KEYS,
    Rtl433LocationAggregator,
    is_link_field,
    partition_fields,
)
from custom_components.rtl_433.const import (
    CONF_MODEL,
    DATA_AGGREGATOR,
    DEVICE_FIELDS,
    DOMAIN,
    signal_location_device_update,
)
from homeassistant.config_entries import RELOAD_AFTER_UPDATE_DELAY
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from tests.conftest import build_receiver_entry, build_receiver_subentry, receiver_id

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
    """One physical sensor, one entity per field, however many receivers hear it."""
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
    assert unique_ids == [
        f"{location.entry_id}:{_DEVICE_KEY}:T",
        f"{location.entry_id}:{_DEVICE_KEY}:last_seen",
    ]


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
    """The same transmission heard twice moves the value once: first wins.

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
        "sensor", DOMAIN, f"{location.entry_id}:{_DEVICE_KEY}:rssi"
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
