"""How field entities treat the rtl_433 server's reconnect replay.

On every (re)connect the server re-sends its recent backlog, and ``pyrtl_433``
marks those frames ``is_replay``. That one flag covers three different things:
frames already delivered, frames sent while Home Assistant was not listening,
and frames from before this connection. Only the middle kind is news.

Measurement entities used to apply and write every replayed frame, which put a
burst of historical readings into the Recorder all stamped "now". Dropping every
replay fixes that but loses state that changed during an outage (a door opened
while Home Assistant restarted). The contract under test:

* ``is_repaint`` -- availability only: no value applied, no ``clear_delay`` arm.
* ``is_replay`` -- adopted only when its server timestamp is at least as new as
  the value the entity holds (live or restored), and written once after the
  burst settles (``REPLAY_WRITE_DELAY``), never when the value is unchanged.
* live -- applied and written at once, superseding a pending replay write.
* ``__init__`` seeds only from a live cached frame; a replayed cached frame is
  weighed against the restored value when the entity is added.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pyrtl_433.library import FieldDescriptor
from pyrtl_433.normalizer import NormalizedEvent
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.rtl_433.binary_sensor import Rtl433BinarySensor
from custom_components.rtl_433.entity import REPLAY_WRITE_DELAY
from custom_components.rtl_433.event import Rtl433Event
from custom_components.rtl_433.sensor import (
    Rtl433LastSeenSensor,
    Rtl433Sensor,
    Rtl433SensorExtraStoredData,
)
from homeassistant.components.sensor import SensorExtraStoredData
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.restore_state import RestoredExtraData
from homeassistant.util import dt as dt_util

# Nine-step historical temperature backlog (reconnect replay shape): values that
# previously landed in the Recorder within tens of milliseconds.
_FRIGOBAR_FREEZER_BURST = (
    -13.0,
    -12.7222222222222,
    -12.4444444444444,
    -12.2222222222222,
    -11.9444444444444,
    -11.7777777777778,
    -11.5,
    -11.3333333333333,
    -11.0555555555556,
)

_DEVICE_KEY = "Acurite-515-121-chAF"
_MODEL = "Acurite-515"
_FIELD = "temperature_C"
_PIR_KEY = "GenericPIR-Z1-5"
_PIR_MODEL = "GenericPIR-Z1"
_DOOR_KEY = "Generic-Door-7"
_DOOR_MODEL = "Generic-Door"

# The server timestamp of the value an entity holds in most tests below.
_T0 = datetime(2026, 11, 3, 9, 57, tzinfo=UTC)


def _coord(*, devices: dict | None = None, last_seen: dict | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.devices = devices if devices is not None else {}
    coordinator.last_seen = last_seen if last_seen is not None else {}
    coordinator.receiver_available = True
    coordinator.hass = MagicMock()
    coordinator.is_event_driven_device = MagicMock(return_value=False)
    coordinator.effective_clear_delay_resolver = None
    return coordinator


def _temp_desc(**overrides: Any) -> FieldDescriptor:
    return FieldDescriptor(
        **{
            "field_key": _FIELD,
            "platform": "sensor",
            "name": "Temperature",
            "object_suffix": "C",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_class": "measurement",
            "force_update": False,
            **overrides,
        }
    )


def _motion_desc(**overrides: Any) -> FieldDescriptor:
    return FieldDescriptor(
        **{
            "field_key": "motion",
            "platform": "binary_sensor",
            "name": "Motion",
            "object_suffix": "motion",
            "device_class": "occupancy",
            "clear_delay": 30,
            "payload": {"on": "1", "off": "0"},
            **overrides,
        }
    )


def _door_desc() -> FieldDescriptor:
    return FieldDescriptor(
        field_key="opened",
        platform="binary_sensor",
        name="Door",
        object_suffix="opened",
        device_class="door",
        payload={"on": 1, "off": 0},
    )


def _event(
    *,
    fields: dict[str, Any] | None = None,
    is_replay: bool = False,
    is_repaint: bool = False,
    event_time: datetime | None = _T0,
    device_key: str = _DEVICE_KEY,
    model: str = _MODEL,
) -> NormalizedEvent:
    return NormalizedEvent(
        device_key=device_key,
        model=model,
        fields=fields if fields is not None else {_FIELD: -11.0},
        is_replay=is_replay,
        is_repaint=is_repaint,
        event_time=event_time,
    )


def _sensor(
    hass=None,
    coordinator: MagicMock | None = None,
    *,
    descriptor: FieldDescriptor | None = None,
) -> Rtl433Sensor:
    coord = coordinator or _coord()
    sensor = Rtl433Sensor(
        coord, "receiver-entry", _DEVICE_KEY, _MODEL, descriptor or _temp_desc()
    )
    sensor.entity_id = f"sensor.{_DEVICE_KEY}_temperature"
    sensor.hass = hass if hass is not None else MagicMock()
    sensor.async_write_ha_state = MagicMock()
    return sensor


def _holding(hass, value: Any, value_time: datetime | None = _T0, **kw) -> Rtl433Sensor:
    """A sensor already showing ``value``, read from a frame sent at ``value_time``."""
    sensor = _sensor(hass, **kw)
    sensor._attr_native_value = value
    sensor._value_time = value_time
    return sensor


def _binary(
    hass=None,
    coordinator: MagicMock | None = None,
    *,
    descriptor: FieldDescriptor | None = None,
    device_key: str = _PIR_KEY,
    model: str = _PIR_MODEL,
) -> Rtl433BinarySensor:
    coord = coordinator or _coord()
    entity = Rtl433BinarySensor(
        coord, "receiver-entry", device_key, model, descriptor or _motion_desc()
    )
    entity.entity_id = f"binary_sensor.{device_key.lower()}"
    entity.hass = hass if hass is not None else MagicMock()
    entity.async_write_ha_state = MagicMock()
    entity._schedule_clear = MagicMock()
    return entity


def _last_seen(coordinator: MagicMock | None = None) -> Rtl433LastSeenSensor:
    coord = coordinator or _coord()
    entity = Rtl433LastSeenSensor(coord, "receiver-entry", _DEVICE_KEY, _MODEL)
    entity.entity_id = f"sensor.{_DEVICE_KEY}_last_seen"
    entity.hass = MagicMock()
    entity.async_write_ha_state = MagicMock()
    return entity


async def _settle(hass) -> None:
    """Let a deferred replay write run."""
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=REPLAY_WRITE_DELAY + 1)
    )
    await hass.async_block_till_done()


# --------------------------------------------------------------------------- #
# Replays older than the held value: the already-recorded backlog              #
# --------------------------------------------------------------------------- #


async def test_backlog_older_than_held_value_is_ignored(hass):
    """The original Recorder sawtooth: a backlog the entity has already seen."""
    restored = -11.0555555555556
    sensor = _holding(hass, restored, value_time=_T0)

    for i, value in enumerate(_FRIGOBAR_FREEZER_BURST):
        sensor._handle_dispatch(
            _event(
                fields={_FIELD: value},
                is_replay=True,
                event_time=_T0 - timedelta(minutes=len(_FRIGOBAR_FREEZER_BURST) - i),
            )
        )
    await _settle(hass)

    assert sensor._attr_native_value == restored
    assert sensor._value_time == _T0
    sensor.async_write_ha_state.assert_not_called()


async def test_replay_of_held_frame_does_not_write(hass):
    """The already-delivered tail re-adopts the same value without a state row.

    ``force_update`` would otherwise record a duplicate on every reconnect.
    """
    sensor = _holding(hass, -11.0, descriptor=_temp_desc(force_update=True))

    sensor._handle_dispatch(_event(fields={_FIELD: -11.0}, is_replay=True))
    await _settle(hass)

    sensor.async_write_ha_state.assert_not_called()


# --------------------------------------------------------------------------- #
# Replays newer than the held value: transmissions made during an outage       #
# --------------------------------------------------------------------------- #


async def test_gap_burst_is_written_once_as_its_newest_value(hass):
    """Frames sent while disconnected are news, but recorded as one state."""
    sensor = _holding(hass, -13.5, value_time=_T0)

    for i, value in enumerate(_FRIGOBAR_FREEZER_BURST, start=1):
        sensor._handle_dispatch(
            _event(
                fields={_FIELD: value},
                is_replay=True,
                event_time=_T0 + timedelta(minutes=i),
            )
        )
    # Nothing is written while the burst may still be arriving.
    sensor.async_write_ha_state.assert_not_called()

    await _settle(hass)

    assert sensor._attr_native_value == _FRIGOBAR_FREEZER_BURST[-1]
    assert sensor._value_time == _T0 + timedelta(minutes=len(_FRIGOBAR_FREEZER_BURST))
    sensor.async_write_ha_state.assert_called_once()


async def test_empty_sensor_seeds_from_newest_replay(hass):
    """Nothing to protect: the backlog is the best reading available."""
    sensor = _sensor(hass)
    assert sensor._attr_native_value is None

    for i, value in enumerate(_FRIGOBAR_FREEZER_BURST):
        sensor._handle_dispatch(
            _event(
                fields={_FIELD: value},
                is_replay=True,
                event_time=_T0 + timedelta(minutes=i),
            )
        )
    await _settle(hass)

    assert sensor._attr_native_value == _FRIGOBAR_FREEZER_BURST[-1]
    sensor.async_write_ha_state.assert_called_once()


async def test_unknown_value_time_accepts_replay(hass):
    """A value restored from data that predates the timestamp can be replaced."""
    sensor = _holding(hass, -13.0, value_time=None)

    sensor._handle_dispatch(_event(fields={_FIELD: -11.5}, is_replay=True))
    await _settle(hass)

    assert sensor._attr_native_value == -11.5
    assert sensor._value_time == _T0
    sensor.async_write_ha_state.assert_called_once()


async def test_equal_time_replay_reapplies_the_transform(hass):
    """The frame behind a restored value re-applies under a changed transform.

    After a calibration reload the restored number came from the old scale; the
    replayed copy of the same frame corrects it without waiting for a live one.
    """
    calibrated = _temp_desc(value_transform={"scale": 0.01})
    sensor = _holding(hass, 123456, value_time=_T0, descriptor=calibrated)

    sensor._handle_dispatch(_event(fields={_FIELD: 123456}, is_replay=True))
    await _settle(hass)

    assert sensor._attr_native_value == 1234.56
    sensor.async_write_ha_state.assert_called_once()


async def test_skewed_server_clock_keeps_sensors_updating(hass):
    """Every frame reads as stale when the server clock lags, yet still lands.

    The comparison is server time against server time, so a constant offset
    from Home Assistant's clock does not freeze the entity.
    """
    lagging = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)  # an hour behind
    sensor = _holding(hass, 1.0, value_time=lagging)

    for i, value in enumerate((2.0, 3.0, 4.0), start=1):
        sensor._handle_dispatch(
            _event(
                fields={_FIELD: value},
                is_replay=True,
                event_time=lagging + timedelta(minutes=i),
            )
        )
        await _settle(hass)
        assert sensor._attr_native_value == value

    assert sensor.async_write_ha_state.call_count == 3


async def test_replay_without_timestamp_is_not_adopted(hass):
    """A frame that cannot be placed in time never replaces a value."""
    sensor = _holding(hass, -11.0)

    sensor._handle_dispatch(
        _event(fields={_FIELD: 5.0}, is_replay=True, event_time=None)
    )
    await _settle(hass)

    assert sensor._attr_native_value == -11.0
    sensor.async_write_ha_state.assert_not_called()


async def test_live_frame_supersedes_pending_replay_write(hass):
    """A live write lands at once and cancels the deferred replay write."""
    sensor = _holding(hass, -13.0)

    sensor._handle_dispatch(
        _event(
            fields={_FIELD: -12.0},
            is_replay=True,
            event_time=_T0 + timedelta(minutes=1),
        )
    )
    sensor._handle_dispatch(
        _event(fields={_FIELD: -10.5}, event_time=_T0 + timedelta(minutes=2))
    )
    assert sensor._attr_native_value == -10.5
    sensor.async_write_ha_state.assert_called_once()

    await _settle(hass)
    sensor.async_write_ha_state.assert_called_once()


async def test_replay_after_cancelled_write_is_still_written(hass):
    """Cancelling a deferred write must leave the next replay free to arm one.

    A live frame drops the pending replay write; a later reconnect's backlog
    has to schedule a fresh write rather than find a stale handle and assume
    one is already pending.
    """
    sensor = _holding(hass, -13.0)

    sensor._handle_dispatch(
        _event(
            fields={_FIELD: -12.0},
            is_replay=True,
            event_time=_T0 + timedelta(minutes=1),
        )
    )
    sensor._handle_dispatch(
        _event(fields={_FIELD: -10.5}, event_time=_T0 + timedelta(minutes=2))
    )
    sensor._handle_dispatch(
        _event(
            fields={_FIELD: -9.0},
            is_replay=True,
            event_time=_T0 + timedelta(minutes=3),
        )
    )
    await _settle(hass)

    assert sensor._attr_native_value == -9.0
    assert sensor.async_write_ha_state.call_count == 2


async def test_removal_cancels_pending_replay_write(hass):
    sensor = _holding(hass, -13.0)
    sensor._handle_dispatch(
        _event(
            fields={_FIELD: -12.0},
            is_replay=True,
            event_time=_T0 + timedelta(minutes=1),
        )
    )

    await sensor.async_will_remove_from_hass()
    await _settle(hass)

    sensor.async_write_ha_state.assert_not_called()


async def test_door_opened_during_outage_is_not_lost(hass):
    """A never-expire contact must show the change it made while unheard."""
    door = _binary(
        hass, descriptor=_door_desc(), device_key=_DOOR_KEY, model=_DOOR_MODEL
    )
    door._attr_is_on = False
    door._value_time = _T0

    door._handle_dispatch(
        _event(
            fields={"opened": 1},
            is_replay=True,
            event_time=_T0 + timedelta(minutes=5),
            device_key=_DOOR_KEY,
            model=_DOOR_MODEL,
        )
    )
    await _settle(hass)

    assert door._attr_is_on is True
    door.async_write_ha_state.assert_called_once()


# --------------------------------------------------------------------------- #
# Restore data carries the value's server timestamp                            #
# --------------------------------------------------------------------------- #


def test_sensor_restore_data_round_trips_value_time():
    sensor = _sensor()
    sensor._attr_native_value = -11.0
    sensor._value_time = _T0

    stored = sensor.extra_restore_state_data.as_dict()

    assert stored["event_time"] == _T0.isoformat()
    # Home Assistant's own reader (and an older build of this integration)
    # still recovers the value and unit, ignoring the extra key.
    plain = SensorExtraStoredData.from_dict(stored)
    assert plain.native_value == -11.0
    assert plain.native_unit_of_measurement == "°C"


def test_sensor_restore_data_without_value_time():
    sensor = _sensor()
    sensor._attr_native_value = -11.0

    assert sensor.extra_restore_state_data == Rtl433SensorExtraStoredData(
        -11.0, "°C", None
    )


async def test_sensor_restore_reads_value_time(hass):
    sensor = _sensor()
    sensor.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData(
            {
                "native_value": -11.0,
                "native_unit_of_measurement": "°C",
                "event_time": _T0.isoformat(),
            }
        )
    )

    await sensor._async_restore_state()

    assert sensor._attr_native_value == -11.0
    assert sensor._value_time == _T0


async def test_binary_restore_round_trips_value_time():
    door = _binary(descriptor=_door_desc(), device_key=_DOOR_KEY, model=_DOOR_MODEL)
    door._attr_is_on = True
    door._value_time = _T0
    stored = door.extra_restore_state_data.as_dict()
    assert stored == {"is_on": True, "event_time": _T0.isoformat()}

    fresh = _binary(descriptor=_door_desc(), device_key=_DOOR_KEY, model=_DOOR_MODEL)
    fresh.async_get_last_extra_data = AsyncMock(return_value=RestoredExtraData(stored))
    await fresh._async_restore_state()
    assert fresh._attr_is_on is True
    assert fresh._value_time == _T0


# --------------------------------------------------------------------------- #
# Seeding at construction                                                       #
# --------------------------------------------------------------------------- #


def test_init_seeds_from_live_cached_frame_only():
    replay = _event(fields={_FIELD: -13.0}, is_replay=True)
    coord = _coord(devices={_DEVICE_KEY: replay})
    assert _sensor(coordinator=coord)._attr_native_value is None

    coord.devices[_DEVICE_KEY] = _event(fields={_FIELD: -11.0})
    seeded = _sensor(coordinator=coord)
    assert seeded._attr_native_value == -11.0
    assert seeded._value_time == _T0


def test_binary_init_seed_skips_replay_last_event():
    replay = _event(
        fields={"motion": "1"}, is_replay=True, device_key=_PIR_KEY, model=_PIR_MODEL
    )
    entity = _binary(coordinator=_coord(devices={_PIR_KEY: replay}))
    assert entity._attr_is_on is None


# --------------------------------------------------------------------------- #
# Watchdog is_repaint                                                          #
# --------------------------------------------------------------------------- #


def test_repaint_writes_availability_without_applying_cached_value():
    sensor = _sensor()
    sensor._attr_native_value = -11.0555555555556
    sensor._handle_dispatch(
        _event(fields={_FIELD: -13.0}, is_repaint=True)  # stale cache
    )

    assert sensor._attr_native_value == -11.0555555555556
    sensor.async_write_ha_state.assert_called_once()


def test_repaint_does_not_arm_binary_clear_delay():
    entity = _binary()
    entity._attr_is_on = False
    entity._handle_dispatch(
        _event(
            fields={"motion": "1"},
            is_repaint=True,
            device_key=_PIR_KEY,
            model=_PIR_MODEL,
        )
    )
    assert entity._attr_is_on is False
    entity._schedule_clear.assert_not_called()
    entity.async_write_ha_state.assert_called_once()


# --------------------------------------------------------------------------- #
# Detect-only motion refuses replays                                           #
# --------------------------------------------------------------------------- #


async def test_replay_motion_does_not_apply_or_arm_clear_delay(hass):
    """A replayed detection is a past moment, not occupancy now."""
    entity = _binary(hass)
    entity._attr_is_on = False

    for minute in range(5):
        entity._handle_dispatch(
            _event(
                fields={"motion": "1"},
                is_replay=True,
                event_time=_T0 + timedelta(minutes=minute),
                device_key=_PIR_KEY,
                model=_PIR_MODEL,
            )
        )
    await _settle(hass)

    assert entity._attr_is_on is False
    entity._schedule_clear.assert_not_called()
    entity.async_write_ha_state.assert_not_called()

    # A live detection still works (payload "1" -> on) and arms the clear.
    entity._handle_dispatch(
        _event(fields={"motion": "1"}, device_key=_PIR_KEY, model=_PIR_MODEL)
    )
    assert entity._attr_is_on is True
    entity._schedule_clear.assert_called_once()
    entity.async_write_ha_state.assert_called_once()


# --------------------------------------------------------------------------- #
# force_update live frames still write every time                             #
# --------------------------------------------------------------------------- #


def test_force_update_live_writes_each_time():
    sensor = _sensor(descriptor=_temp_desc(force_update=True))
    sensor._attr_native_value = 3.0

    for _ in range(3):
        sensor._handle_dispatch(_event(fields={_FIELD: 3.0}))
    assert sensor.async_write_ha_state.call_count == 3


# --------------------------------------------------------------------------- #
# Last seen                                                                    #
# --------------------------------------------------------------------------- #


def test_last_seen_ignores_replay_repaint_updates_on_live():
    seen = datetime(2026, 11, 3, 10, 5, tzinfo=UTC)
    coord = _coord(last_seen={_DEVICE_KEY: seen})
    entity = _last_seen(coord)
    entity._attr_native_value = seen

    entity._handle_dispatch(_event(is_replay=True, fields={}))
    entity.async_write_ha_state.assert_not_called()
    assert entity._attr_native_value == seen

    entity._handle_dispatch(_event(is_repaint=True, fields={}))
    entity.async_write_ha_state.assert_called_once()
    assert entity._attr_native_value == seen

    newer = datetime(2026, 11, 3, 10, 6, tzinfo=UTC)
    coord.last_seen[_DEVICE_KEY] = newer
    entity._handle_dispatch(_event(fields={}))
    assert entity._attr_native_value == newer


def test_last_seen_repaint_wins_over_replay_flag():
    """Checked in the base class's order: a repaint always re-reads availability."""
    entity = _last_seen()
    entity._handle_dispatch(_event(is_repaint=True, is_replay=True, fields={}))
    entity.async_write_ha_state.assert_called_once()


def test_last_seen_does_not_seed_from_replay_only_device():
    """A replay-only devices entry would seed the startup baseline, not a real time."""
    baseline = datetime(2026, 11, 3, 10, 30, tzinfo=UTC)
    coord = _coord(
        devices={_DEVICE_KEY: _event(is_replay=True)},
        last_seen={_DEVICE_KEY: baseline},
    )
    assert _last_seen(coord)._attr_native_value is None

    coord.devices[_DEVICE_KEY] = _event()
    assert _last_seen(coord)._attr_native_value == baseline


# --------------------------------------------------------------------------- #
# Event entity still suppresses replay (no regression)                         #
# --------------------------------------------------------------------------- #


async def test_event_entity_still_suppresses_replay(hass, receiver_entry_builder):
    """Rtl433Event must not fire on is_replay (pre-existing contract)."""
    entry = receiver_entry_builder(
        devices={
            "Honeywell-Doorbell-7": {
                "model": "Honeywell-Doorbell",
                "fields": ["secret_knock"],
            }
        }
    )
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("rtl_433", entry.entry_id)}
    )
    from custom_components.rtl_433.coordinator import Rtl433Coordinator

    coordinator = Rtl433Coordinator(hass, entry, host="rtl433.local")
    descriptor = FieldDescriptor(
        field_key="secret_knock",
        platform="event",
        name="Secret knock",
        object_suffix="secret_knock",
        event_map={"1": "secret_knock"},
    )
    entity = Rtl433Event(
        coordinator,
        entry.entry_id,
        "Honeywell-Doorbell-7",
        "Honeywell-Doorbell",
        descriptor,
    )
    entity.hass = hass
    entity.entity_id = "event.honeywell_doorbell_7_secret_knock"
    entity.async_write_ha_state = MagicMock()
    entity._trigger_event = MagicMock()

    replay = NormalizedEvent(
        device_key="Honeywell-Doorbell-7",
        model="Honeywell-Doorbell",
        fields={"secret_knock": 1},
        is_replay=True,
        event_time=_T0,
    )
    entity._handle_dispatch(replay)
    entity._trigger_event.assert_not_called()
