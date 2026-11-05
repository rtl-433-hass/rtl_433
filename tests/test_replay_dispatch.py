"""Reconnect-replay must not publish historical readings as new HA states.

Regression for the Recorder burst observed when a hub reconnects: the server
replays a backlog of recent frames, ``pyrtl_433`` marks them ``is_replay``, and
measurement entities used to ``_apply_value`` + ``async_write_ha_state`` on every
step — stamping old temperatures with wall-clock "now".

The contract under test (aligned with ``Rtl433Event``):

* ``is_repaint`` — availability only (no value apply, no clear_delay arm)
* ``is_replay`` — ignore for measurement state (restore / next live own the value)
* live — apply + write as today
* ``__init__`` seed skips ``last_event.is_replay``
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from pyrtl_433.library import FieldDescriptor
from pyrtl_433.normalizer import NormalizedEvent

from custom_components.rtl_433.binary_sensor import Rtl433BinarySensor
from custom_components.rtl_433.event import Rtl433Event
from custom_components.rtl_433.sensor import Rtl433LastSeenSensor, Rtl433Sensor
from homeassistant.helpers import device_registry as dr

# Nine-step historical temperature backlog (reconnect replay shape): values
# that previously landed in the Recorder within tens of milliseconds.
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


def _coord(*, devices: dict | None = None, last_seen: dict | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.devices = devices if devices is not None else {}
    coordinator.last_seen = last_seen if last_seen is not None else {}
    coordinator.hub_available = True
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


def _event(
    *,
    fields: dict[str, Any] | None = None,
    is_replay: bool = False,
    is_repaint: bool = False,
    event_time: datetime | None = None,
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
    coordinator: MagicMock | None = None,
    *,
    descriptor: FieldDescriptor | None = None,
    device_key: str = _DEVICE_KEY,
    model: str = _MODEL,
) -> Rtl433Sensor:
    coord = coordinator or _coord()
    sensor = Rtl433Sensor(
        coord, "hub-entry", device_key, model, descriptor or _temp_desc()
    )
    sensor.entity_id = f"sensor.{device_key}_temperature"
    sensor.hass = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    return sensor


def _binary(
    coordinator: MagicMock | None = None,
    *,
    descriptor: FieldDescriptor | None = None,
) -> Rtl433BinarySensor:
    coord = coordinator or _coord()
    entity = Rtl433BinarySensor(
        coord,
        "hub-entry",
        "GenericPIR-Z1-5",
        "GenericPIR-Z1",
        descriptor or _motion_desc(),
    )
    entity.entity_id = "binary_sensor.genericpir_z1_5_motion"
    entity.hass = MagicMock()
    entity.async_write_ha_state = MagicMock()
    entity._schedule_clear = MagicMock()
    return entity


def _last_seen(coordinator: MagicMock | None = None) -> Rtl433LastSeenSensor:
    coord = coordinator or _coord()
    entity = Rtl433LastSeenSensor(coord, "hub-entry", _DEVICE_KEY, _MODEL)
    entity.entity_id = f"sensor.{_DEVICE_KEY}_last_seen"
    entity.hass = MagicMock()
    entity.async_write_ha_state = MagicMock()
    return entity


# --------------------------------------------------------------------------- #
# T1 — restored sensor + 9-frame reconnect burst
# --------------------------------------------------------------------------- #


def test_replay_burst_with_restore_does_not_rewrite_historical_steps():
    """Restored last value must survive a multi-frame is_replay backlog untouched."""
    sensor = _sensor()
    restored = -11.0555555555556
    sensor._attr_native_value = restored
    sensor.async_write_ha_state.reset_mock()

    t0 = datetime(2026, 11, 3, 9, 57, tzinfo=UTC)
    for i, value in enumerate(_FRIGOBAR_FREEZER_BURST):
        sensor._handle_dispatch(
            _event(
                fields={_FIELD: value},
                is_replay=True,
                event_time=t0 + timedelta(minutes=i),
            )
        )

    assert sensor._attr_native_value == restored
    sensor.async_write_ha_state.assert_not_called()

    live = -10.8333333333333
    sensor._handle_dispatch(_event(fields={_FIELD: live}, is_replay=False))
    assert sensor._attr_native_value == live
    sensor.async_write_ha_state.assert_called()


# --------------------------------------------------------------------------- #
# T2 — empty entity waits for live; init seed skips replay
# --------------------------------------------------------------------------- #


def test_empty_sensor_ignores_replay_until_live():
    sensor = _sensor()
    assert sensor._attr_native_value is None

    for value in _FRIGOBAR_FREEZER_BURST:
        sensor._handle_dispatch(_event(fields={_FIELD: value}, is_replay=True))

    assert sensor._attr_native_value is None
    sensor.async_write_ha_state.assert_not_called()

    sensor._handle_dispatch(_event(fields={_FIELD: -10.5}, is_replay=False))
    assert sensor._attr_native_value == -10.5
    sensor.async_write_ha_state.assert_called()


def test_init_seed_skips_replay_last_event():
    replay = _event(fields={_FIELD: -13.0}, is_replay=True)
    coord = _coord(devices={_DEVICE_KEY: replay})
    sensor = _sensor(coord)
    assert sensor._attr_native_value is None

    live = _event(fields={_FIELD: -11.0}, is_replay=False)
    coord.devices[_DEVICE_KEY] = live
    sensor2 = _sensor(coord)
    assert sensor2._attr_native_value == -11.0


def test_binary_init_seed_skips_replay_last_event():
    replay = NormalizedEvent(
        device_key="GenericPIR-Z1-5",
        model="GenericPIR-Z1",
        fields={"motion": "1"},
        is_replay=True,
    )
    coord = _coord(devices={"GenericPIR-Z1-5": replay})
    entity = _binary(coord)
    assert entity._attr_is_on is None


# --------------------------------------------------------------------------- #
# T3 — watchdog is_repaint
# --------------------------------------------------------------------------- #


def test_repaint_writes_availability_without_applying_cached_value():
    sensor = _sensor()
    sensor._attr_native_value = -11.0555555555556
    sensor.async_write_ha_state.reset_mock()

    sensor._handle_dispatch(
        _event(
            fields={_FIELD: -13.0},  # stale cache older than restored
            is_replay=False,
            is_repaint=True,
        )
    )

    assert sensor._attr_native_value == -11.0555555555556
    sensor.async_write_ha_state.assert_called_once()


def test_repaint_does_not_arm_binary_clear_delay():
    entity = _binary()
    entity._attr_is_on = False
    entity._handle_dispatch(
        NormalizedEvent(
            device_key="GenericPIR-Z1-5",
            model="GenericPIR-Z1",
            fields={"motion": "1"},
            is_replay=False,
            is_repaint=True,
        )
    )
    assert entity._attr_is_on is False
    entity._schedule_clear.assert_not_called()
    entity.async_write_ha_state.assert_called_once()


# --------------------------------------------------------------------------- #
# T4 — binary clear_delay + replay
# --------------------------------------------------------------------------- #


def test_replay_motion_does_not_apply_or_arm_clear_delay():
    entity = _binary()
    entity._attr_is_on = False

    for _ in range(5):
        entity._handle_dispatch(
            NormalizedEvent(
                device_key="GenericPIR-Z1-5",
                model="GenericPIR-Z1",
                fields={"motion": "1"},
                is_replay=True,
            )
        )

    assert entity._attr_is_on is False
    entity._schedule_clear.assert_not_called()
    entity.async_write_ha_state.assert_not_called()

    # Live detection still works (apply_transform maps payload "1" -> on).
    entity._handle_dispatch(
        NormalizedEvent(
            device_key="GenericPIR-Z1-5",
            model="GenericPIR-Z1",
            fields={"motion": "1"},
            is_replay=False,
        )
    )
    assert entity._attr_is_on is True
    entity.async_write_ha_state.assert_called()


# --------------------------------------------------------------------------- #
# T5 — force_update live still writes; replay still silent
# --------------------------------------------------------------------------- #


def test_force_update_live_writes_each_time_replay_silent():
    sensor = _sensor(descriptor=_temp_desc(force_update=True))
    sensor._attr_native_value = 3.0
    sensor.async_write_ha_state.reset_mock()

    for _ in range(3):
        sensor._handle_dispatch(_event(fields={_FIELD: 3.0}, is_replay=False))
    assert sensor.async_write_ha_state.call_count == 3

    sensor._handle_dispatch(_event(fields={_FIELD: 3.0}, is_replay=True))
    assert sensor.async_write_ha_state.call_count == 3
    assert sensor._attr_native_value == 3.0


# --------------------------------------------------------------------------- #
# T6 — reconnect pattern (restore + replay + live)
# --------------------------------------------------------------------------- #


def test_reconnect_pattern_restore_then_silent_replay_then_live():
    sensor = _sensor()
    sensor._attr_native_value = -21.3333333333333
    sensor.async_write_ha_state.reset_mock()

    for value in (-20.11, -20.33, -20.88, -21.0, -21.22, -21.33):
        sensor._handle_dispatch(_event(fields={_FIELD: value}, is_replay=True))

    assert sensor._attr_native_value == -21.3333333333333
    sensor.async_write_ha_state.assert_not_called()

    sensor._handle_dispatch(_event(fields={_FIELD: -21.3888888888889}, is_replay=False))
    assert sensor._attr_native_value == -21.3888888888889
    sensor.async_write_ha_state.assert_called()


# --------------------------------------------------------------------------- #
# Last seen
# --------------------------------------------------------------------------- #


def test_last_seen_ignores_replay_repaint_updates_on_live():
    seen = datetime(2026, 11, 3, 10, 5, tzinfo=UTC)
    coord = _coord(last_seen={_DEVICE_KEY: seen})
    entity = _last_seen(coord)
    entity._attr_native_value = seen
    entity.async_write_ha_state.reset_mock()

    entity._handle_dispatch(_event(is_replay=True, fields={}))
    entity.async_write_ha_state.assert_not_called()
    assert entity._attr_native_value == seen

    entity._handle_dispatch(_event(is_repaint=True, is_replay=False, fields={}))
    entity.async_write_ha_state.assert_called_once()
    assert entity._attr_native_value == seen

    newer = datetime(2026, 11, 3, 10, 6, tzinfo=UTC)
    coord.last_seen[_DEVICE_KEY] = newer
    entity._handle_dispatch(_event(is_replay=False, is_repaint=False, fields={}))
    assert entity._attr_native_value == newer


# --------------------------------------------------------------------------- #
# Event entity still suppresses replay (no regression)
# --------------------------------------------------------------------------- #


async def test_event_entity_still_suppresses_replay(hass, hub_entry_builder):
    """Rtl433Event must not fire on is_replay (pre-existing contract)."""
    entry = hub_entry_builder(
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

    entity._handle_dispatch(
        NormalizedEvent(
            device_key="Honeywell-Doorbell-7",
            model="Honeywell-Doorbell",
            fields={"secret_knock": 1},
            is_replay=True,
        )
    )
    entity._trigger_event.assert_not_called()


# --------------------------------------------------------------------------- #
# Dynamic entity creation seed (new device while last frame is replay)
# --------------------------------------------------------------------------- #


def test_dynamic_entity_creation_does_not_seed_from_replay_cache():
    """Platform new-device path constructs Rtl433Sensor with coordinator.devices set.

    If the first frame was a reconnect replay, the entity must stay empty until live.
    """
    replay = _event(fields={_FIELD: -13.0}, is_replay=True)
    coord = _coord(devices={_DEVICE_KEY: replay})
    sensor = Rtl433Sensor(coord, "hub", _DEVICE_KEY, _MODEL, _temp_desc())
    assert sensor._attr_native_value is None


# --------------------------------------------------------------------------- #
# Documentation: repaint write ≠ new measurement
# --------------------------------------------------------------------------- #


def test_repaint_state_write_is_availability_not_new_measurement():
    """force_update does not matter: repaint must not change native_value.

    ``async_write_ha_state`` on repaint is legitimate when ``available`` flips.
    That is distinct from inventing temperature samples in the Recorder.
    """
    sensor = _sensor(descriptor=_temp_desc(force_update=True))
    sensor._attr_native_value = 3.0
    sensor._handle_dispatch(
        _event(fields={_FIELD: 99.0}, is_repaint=True, is_replay=False)
    )
    assert sensor._attr_native_value == 3.0
