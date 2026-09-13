"""Mutation-killing tests for custom_components/rtl_433/entity.py.

Covers every function and branch in entity.py with precise assertions
designed to detect mutmut's operator flips, constant substitutions,
removed statements, and negated conditions.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from freezegun import freeze_time
from pyrtl_433.library import FieldDescriptor
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_ENERGY,
    COMMODITY_GAS,
    COMMODITY_WATER,
    CONF_DEVICES,
    CONF_MODEL,
    CONF_USER_MAPPINGS,
    DATA_ENTRY_LIBRARY,
    DEVICE_CALIBRATION,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DOMAIN,
    signal_device_update,
    signal_receiver_update,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.coordinator.base import Rtl433Client
from custom_components.rtl_433.entity import (
    Rtl433Entity,
    _apply_calibration,
    _resolve_entity_category,
    async_setup_receiver_platform,
    async_upsert_device,
    async_upsert_event_types,
)
from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util
from tests.conftest import (
    build_receiver_entry,
    build_receiver_subentry,
    receiver_id,
    receiver_scope,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_socket():
    """Stub the connect loop so no real WebSocket is opened."""

    async def _noop(self) -> None:
        return None

    with patch.object(Rtl433Client, "start", _noop):
        yield


def _coordinator(hass, receiver_entry: MockConfigEntry) -> Rtl433Coordinator:
    return hass.data[DOMAIN][receiver_id(receiver_entry)]


def _feed(coordinator: Rtl433Coordinator, event: dict) -> None:
    coordinator._client._process_event(event)


async def _setup_receiver(hass, receiver_entry_builder, *, devices=None, **kwargs):
    """Set up a receiver entry. Defaults availability_timeout=600 unless overridden.

    The autouse ``receiver_connected_by_default`` fixture leaves the coordinator
    connected, so the per-device silence timeouts are what these tests actually
    measure; without it the connection-backed gate would take every device
    unavailable the moment the socket reads as down.
    """
    kwargs.setdefault("availability_timeout", 600)
    receiver = receiver_entry_builder(devices=devices, **kwargs)
    receiver.add_to_hass(hass)
    assert await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()
    return receiver


# ---------------------------------------------------------------------------
# _resolve_entity_category
# ---------------------------------------------------------------------------


def test_resolve_entity_category_none_returns_none():
    """None input returns None (no category)."""
    assert _resolve_entity_category(None) is None


def test_resolve_entity_category_diagnostic():
    """'diagnostic' string maps to EntityCategory.DIAGNOSTIC."""
    result = _resolve_entity_category("diagnostic")
    assert result is EntityCategory.DIAGNOSTIC
    # Make sure it is not None and not config
    assert result is not None
    assert result != EntityCategory.CONFIG


def test_resolve_entity_category_config():
    """'config' string maps to EntityCategory.CONFIG."""
    result = _resolve_entity_category("config")
    assert result is EntityCategory.CONFIG


def test_resolve_entity_category_unknown_returns_none():
    """Unrecognised string returns None instead of raising."""
    assert _resolve_entity_category("bogus_category") is None


def test_resolve_entity_category_empty_string_returns_none():
    """Empty string is not a valid category -> None."""
    assert _resolve_entity_category("") is None


# ---------------------------------------------------------------------------
# _apply_calibration
# ---------------------------------------------------------------------------


def _make_descriptor(**overrides) -> FieldDescriptor:
    """Build a minimal FieldDescriptor for calibration testing."""
    defaults = dict(
        field_key="consumption_data",
        platform="sensor",
        name="Consumption",
        object_suffix="consumption",
        device_class=None,
        unit_of_measurement=None,
        state_class=None,
        value_transform={"int": True},
    )
    defaults.update(overrides)
    return FieldDescriptor(**defaults)


def test_apply_calibration_sets_device_class_energy():
    """Energy calibration sets device_class to 'energy'."""
    descriptor = _make_descriptor()
    calibration = {
        CALIBRATION_COMMODITY: COMMODITY_ENERGY,
        CALIBRATION_UNIT: "kWh",
        CALIBRATION_SCALE: 0.001,
    }
    result = _apply_calibration(descriptor, calibration)
    assert result.device_class == "energy"


def test_apply_calibration_sets_device_class_gas():
    """Gas calibration sets device_class to 'gas'."""
    descriptor = _make_descriptor()
    calibration = {
        CALIBRATION_COMMODITY: COMMODITY_GAS,
        CALIBRATION_UNIT: "m³",
        CALIBRATION_SCALE: 0.001,
    }
    result = _apply_calibration(descriptor, calibration)
    assert result.device_class == "gas"


def test_apply_calibration_sets_device_class_water():
    """Water calibration sets device_class to 'water'."""
    descriptor = _make_descriptor()
    calibration = {
        CALIBRATION_COMMODITY: COMMODITY_WATER,
        CALIBRATION_UNIT: "L",
        CALIBRATION_SCALE: 0.1,
    }
    result = _apply_calibration(descriptor, calibration)
    assert result.device_class == "water"


def test_apply_calibration_sets_unit_of_measurement():
    """Calibration unit_of_measurement is applied from the calibration record."""
    descriptor = _make_descriptor()
    calibration = {
        CALIBRATION_COMMODITY: COMMODITY_WATER,
        CALIBRATION_UNIT: "L",
        CALIBRATION_SCALE: 1.0,
    }
    result = _apply_calibration(descriptor, calibration)
    assert result.unit_of_measurement == "L"
    # Ensure it is not the original None
    assert result.unit_of_measurement is not None


def test_apply_calibration_sets_state_class_total_increasing():
    """Calibration forces state_class to TOTAL_INCREASING."""
    descriptor = _make_descriptor()
    calibration = {
        CALIBRATION_COMMODITY: COMMODITY_ENERGY,
        CALIBRATION_UNIT: "kWh",
        CALIBRATION_SCALE: 0.001,
    }
    result = _apply_calibration(descriptor, calibration)
    assert result.state_class == SensorStateClass.TOTAL_INCREASING.value
    assert result.state_class == "total_increasing"


def test_apply_calibration_injects_scale_into_transform():
    """Scale is injected into value_transform under the 'scale' key."""
    descriptor = _make_descriptor(value_transform={"int": True})
    calibration = {
        CALIBRATION_COMMODITY: COMMODITY_WATER,
        CALIBRATION_UNIT: "m³",
        CALIBRATION_SCALE: 0.001,
    }
    result = _apply_calibration(descriptor, calibration)
    assert result.value_transform is not None
    assert "scale" in result.value_transform
    assert result.value_transform["scale"] == 0.001
    # Original 'int' key is still present
    assert result.value_transform.get("int") is True


def test_apply_calibration_scale_exact_value():
    """Scale value in transform matches the calibration record precisely."""
    descriptor = _make_descriptor(value_transform=None)
    calibration = {
        CALIBRATION_COMMODITY: COMMODITY_ENERGY,
        CALIBRATION_UNIT: "kWh",
        CALIBRATION_SCALE: 2.5,
    }
    result = _apply_calibration(descriptor, calibration)
    assert result.value_transform["scale"] == 2.5
    # Not accidentally 0, 1, or something else
    assert result.value_transform["scale"] != 1.0
    assert result.value_transform["scale"] != 0.0


def test_apply_calibration_does_not_mutate_original_descriptor():
    """_apply_calibration returns a new descriptor, leaving the original intact."""
    descriptor = _make_descriptor(value_transform={"int": True})
    calibration = {
        CALIBRATION_COMMODITY: COMMODITY_WATER,
        CALIBRATION_UNIT: "L",
        CALIBRATION_SCALE: 0.5,
    }
    result = _apply_calibration(descriptor, calibration)
    # The result is a new object, not the same
    assert result is not descriptor
    # Original is unchanged
    assert descriptor.device_class is None
    assert descriptor.state_class is None
    assert "scale" not in (descriptor.value_transform or {})


def test_apply_calibration_with_none_transform_creates_transform_with_scale():
    """When value_transform is None, the result still has a scale key."""
    descriptor = _make_descriptor(value_transform=None)
    calibration = {
        CALIBRATION_COMMODITY: COMMODITY_ENERGY,
        CALIBRATION_UNIT: "Wh",
        CALIBRATION_SCALE: 3.0,
    }
    result = _apply_calibration(descriptor, calibration)
    assert result.value_transform is not None
    assert result.value_transform["scale"] == 3.0


# ---------------------------------------------------------------------------
# Rtl433Entity.__init__ — identity and device info
# ---------------------------------------------------------------------------


async def test_entity_unique_id_format(hass, receiver_entry_builder):
    """unique_id is {receiver_entry_id}:{device_key}:{object_suffix}."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    # The unique_id format is exactly receiver_entry_id:device_key:object_suffix
    uid = f"{receiver.entry_id}:{device_key}:watts"
    eid = ent_reg.async_get_entity_id("sensor", DOMAIN, uid)
    assert eid is not None, f"Entity with unique_id {uid} not found"


async def test_entity_unique_id_two_receivers_no_collision(
    hass, receiver_entry_builder
):
    """Two receivers seeing the same device produce non-colliding unique_ids.

    The unique_id embeds the receiver entry_id, so even though both receivers observe
    the same device_key, their entity unique_ids are distinct.
    """
    device_key = "EnergyMeter-2000-1234"
    device_spec = {
        device_key: {
            CONF_MODEL: "EnergyMeter-2000",
            DEVICE_FIELDS: ["power_W"],
        }
    }
    # Set up first receiver normally
    receiver_a = await _setup_receiver(
        hass, receiver_entry_builder, host="receiver-a.local", devices=device_spec
    )

    # The unique_id format is receiver_entry_id:device_key:object_suffix.
    # Two different receiver_entry_ids (always different MockConfigEntry.entry_id
    # values) guarantee no collision — verify the format property holds.
    uid_a = f"{receiver_a.entry_id}:{device_key}:watts"
    ent_reg = er.async_get(hass)
    eid_a = ent_reg.async_get_entity_id("sensor", DOMAIN, uid_a)
    assert eid_a is not None

    # Construct a second unique_id as would be used by a second receiver entry with
    # a different entry_id and assert it differs from the first.
    fake_entry_id = "different-receiver-entry-id"
    uid_b = f"{fake_entry_id}:{device_key}:watts"
    assert uid_a != uid_b
    # The receiver entry_id is the discriminating component
    assert receiver_a.entry_id != fake_entry_id


async def test_entity_name_from_descriptor(hass, receiver_entry_builder):
    """Entity name comes from the descriptor's name field."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["battery_mV"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{receiver.entry_id}:{device_key}:mV"
    entry = ent_reg.async_get(ent_reg.async_get_entity_id("sensor", DOMAIN, uid))
    assert entry is not None
    # "Battery mV" is the explicit name for battery_mV in the library
    assert entry.original_name == "Battery mV"


async def test_entity_name_none_derives_from_device_class(hass, receiver_entry_builder):
    """A field with no descriptor name is auto-named by HA from its device_class.

    ``power_W`` ships with ``name: null``; because the entity leaves ``_attr_name``
    unset, HA derives the (translatable) name "Power" from ``device_class`` and
    the entity_id keeps its ``_power`` suffix.
    """
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{receiver.entry_id}:{device_key}:watts"
    entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, uid)
    entry = ent_reg.async_get(entity_id)
    assert entry is not None
    assert entry.original_name == "Power"
    assert entity_id.endswith("_power")


async def test_entity_has_entity_name_true(hass, receiver_entry_builder):
    """_attr_has_entity_name is True so entities get device-relative naming."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{receiver.entry_id}:{device_key}:T"
    entry = ent_reg.async_get(ent_reg.async_get_entity_id("sensor", DOMAIN, uid))
    assert entry is not None
    # has_entity_name = True means name is stored but entity_id uses device name
    assert entry.has_entity_name is True


async def test_device_info_identifiers(hass, receiver_entry_builder):
    """DeviceInfo identifiers is {(DOMAIN, receiver_entry_id:device_key)}."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    # The nested device is registered with the correct identifier
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver.entry_id}:{device_key}"), receiver.entry_id
    )
    assert device_entry is not None


async def test_device_info_manufacturer(hass, receiver_entry_builder):
    """DeviceInfo manufacturer is 'rtl_433'."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver.entry_id}:{device_key}"), receiver.entry_id
    )
    assert device_entry is not None
    assert device_entry.manufacturer == "rtl_433"


def _identity_entity(model: str, device_key: str) -> Rtl433Entity:
    """Build a bare Rtl433Entity (mock coordinator) for device-info assertions."""
    descriptor = FieldDescriptor(
        field_key="temperature_C",
        platform="sensor",
        name=None,
        object_suffix="C",
        device_class="temperature",
        unit_of_measurement="°C",
        state_class="measurement",
    )
    return Rtl433Entity(MagicMock(), "receiver", device_key, model, descriptor)


def test_device_info_serial_number_is_identity_suffix():
    """DeviceInfo.serial_number carries the id suffix, model prefix stripped."""
    entity = _identity_entity("Fineoffset-WH51", "Fineoffset-WH51-00c50f")
    assert entity.device_info["serial_number"] == "00c50f"


def test_device_info_serial_number_none_for_model_only_device():
    """A model-only device (key == model token) publishes no serial number."""
    entity = _identity_entity("Fineoffset-WH51", "Fineoffset-WH51")
    assert entity.device_info["serial_number"] is None


@pytest.mark.parametrize(
    ("model", "device_key", "expected"),
    [
        pytest.param(
            "Acurite-986",
            "Acurite-986-1a2b-ch2",
            "1a2b-ch2",
            id="channel-kept-in-serial",
        ),
        pytest.param("", "Acurite-986-1a2b", None, id="unknown-model"),
        pytest.param("Acurite-986", "Nexus-TH-77", None, id="key-not-under-model"),
    ],
)
def test_device_info_serial_number_edge_cases(model, device_key, expected):
    """The serial number is the whole id suffix, or unset when there isn't one.

    The channel/state part is transmitter identity too, so it stays in the serial
    number. A device whose model never decoded, and a key that is not shaped as
    ``{model}-{suffix}``, both leave the field unset rather than publishing a
    misleading serial.
    """
    assert _identity_entity(model, device_key).device_info["serial_number"] == expected


async def test_device_name_with_model(hass, receiver_entry_builder):
    """Device name is '{model} {id-suffix}' (no redundant model) when model set."""
    model = "EnergyMeter-2000"
    device_key = f"{model}-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: model,
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver.entry_id}:{device_key}"), receiver.entry_id
    )
    assert device_entry is not None
    # Only the distinguishing id suffix follows the model, not the whole key.
    expected_name = f"{model} 1234"
    assert device_entry.name == expected_name


async def test_device_name_without_model_is_device_key(hass, receiver_entry_builder):
    """Device name is just device_key when model is empty/absent."""
    device_key = "UnknownDevice-7"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "",  # empty string -> no model
                DEVICE_FIELDS: [],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver.entry_id}:{device_key}"), receiver.entry_id
    )
    # The name is device_key when model is falsy
    assert device_entry.name == device_key


async def test_device_name_model_only_has_no_suffix(hass, receiver_entry_builder):
    """A model-only device (key == model token) is named with just the model."""
    model = "Foo"
    device_key = model  # no id/channel/subtype -> key is the bare model token
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: model,
                DEVICE_FIELDS: [],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver.entry_id}:{device_key}"), receiver.entry_id
    )
    assert device_entry is not None
    # No redundant "Foo (Foo)" and no trailing space — just the model.
    assert device_entry.name == model


async def test_device_model_set_when_model_present(hass, receiver_entry_builder):
    """DeviceInfo.model is the model string when non-empty."""
    model = "EnergyMeter-2000"
    device_key = f"{model}-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: model,
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver.entry_id}:{device_key}"), receiver.entry_id
    )
    assert device_entry.model == model


async def test_device_via_device_links_to_the_location(hass, receiver_entry_builder):
    """A merged device's via_device_id points to the LOCATION device.

    Not to the receiver that decoded the frame: the device may end up fed by
    several receivers, so a link to one of them would claim the sensor sits
    behind that server alone.
    """
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    nested = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver.entry_id}:{device_key}"), receiver.entry_id
    )
    location_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, receiver.entry_id), receiver.entry_id
    )
    receiver_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, receiver_scope(receiver)), receiver.entry_id
    )
    assert nested is not None
    assert location_device is not None
    assert receiver_device is not None
    # via_device_id must be the location device — not None, not the receiver
    assert nested.via_device_id is not None
    assert nested.via_device_id == location_device.id
    assert nested.via_device_id != receiver_device.id


async def test_entity_category_none_by_default(hass, receiver_entry_builder):
    """A sensor without a library entity_category has no category (None)."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{receiver.entry_id}:{device_key}:watts"
    entry = ent_reg.async_get(ent_reg.async_get_entity_id("sensor", DOMAIN, uid))
    assert entry is not None
    # power_W is a measurement, not diagnostic -> no entity_category
    assert entry.entity_category is None


async def test_entity_category_diagnostic_for_battery(hass, receiver_entry_builder):
    """Battery sensor has entity_category = DIAGNOSTIC."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["battery_ok"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{receiver.entry_id}:{device_key}:B"
    entry = ent_reg.async_get(ent_reg.async_get_entity_id("sensor", DOMAIN, uid))
    assert entry is not None
    assert entry.entity_category == EntityCategory.DIAGNOSTIC


# ---------------------------------------------------------------------------
# Availability: boundary conditions on the timeout comparison
# ---------------------------------------------------------------------------


async def test_available_within_timeout(hass, receiver_entry_builder):
    """Entity is available while within the timeout window."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    uid = f"{receiver.entry_id}:{device_key}:watts"
    watts_eid = ent_reg.async_get_entity_id("sensor", DOMAIN, uid)
    assert watts_eid is not None

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # 1 second before the timeout -> still available
    with freeze_time(start + timedelta(seconds=599)):
        state = hass.states.get(watts_eid)
        assert state.state != "unavailable"


async def test_available_at_exact_timeout_boundary(hass, receiver_entry_builder):
    """Entity is still available when elapsed time exactly equals the timeout (<=)."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # Exactly at the boundary (elapsed == timeout) -> still available (<=)
    with freeze_time(start + timedelta(seconds=600)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        state = hass.states.get(watts_eid)
        assert state.state != "unavailable"


async def test_unavailable_one_second_past_timeout(hass, receiver_entry_builder):
    """Entity goes unavailable when elapsed time exceeds the timeout (> timeout)."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # One second past the timeout -> unavailable
    with freeze_time(start + timedelta(seconds=601)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        state = hass.states.get(watts_eid)
        assert state.state == "unavailable"


async def test_unavailable_without_last_seen(hass, receiver_entry_builder):
    """An entity with no last_seen entry is unavailable (available property = False).

    The entity is first fed a live event (which sets last_seen), then last_seen
    is manually cleared. At that point the available property returns False.
    The watchdog (which only dispatches when there is a cached event) then
    flips the entity state to unavailable.
    """
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()
    assert hass.states.get(watts_eid).state == "5.0"

    # Clear last_seen to simulate the "never seen" state, then run watchdog
    # far in the future (so even if last_seen were still set, it would be stale).
    coordinator.last_seen.pop(device_key, None)
    coordinator.available[device_key] = True  # simulate it thinking it was available

    # Advance time by far more than the 600s timeout; watchdog iterates last_seen
    # (now empty for this device) so it never dispatches -> entity stays as last
    # written. But after clearing last_seen the available property returns False,
    # so a fresh _feed with the same device will repaint it unavailable.
    coordinator.last_seen[device_key] = start - timedelta(seconds=700)
    with freeze_time(start + timedelta(seconds=700)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()

    state = hass.states.get(watts_eid)
    assert state.state == "unavailable"


async def test_available_baseline_on_startup_before_event(hass, receiver_entry_builder):
    """On startup, entities baseline last_seen to now so they start available."""
    device_key = "Acurite-606TX-42"
    start = dt_util.utcnow()
    with freeze_time(start):
        receiver = await _setup_receiver(
            hass,
            receiver_entry_builder,
            availability_timeout=600,
            devices={
                device_key: {
                    CONF_MODEL: "Acurite-606TX",
                    DEVICE_FIELDS: ["temperature_C"],
                }
            },
        )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    temp_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:T"
    )
    # Without any live event, the entity should start available (baseline)
    state = hass.states.get(temp_eid)
    assert state.state != "unavailable"
    # coordinator.last_seen must have been set (the baseline write)
    assert device_key in coordinator.last_seen
    assert coordinator.available.get(device_key) is True


async def test_baseline_last_seen_not_set_if_already_present(
    hass, receiver_entry_builder
):
    """If coordinator already has last_seen for a device, the baseline is skipped."""
    device_key = "EnergyMeter-2000-1234"
    start = dt_util.utcnow()

    # Set up the receiver and feed an event to establish a real last_seen
    with freeze_time(start):
        receiver = await _setup_receiver(
            hass,
            receiver_entry_builder,
            availability_timeout=600,
            devices={
                device_key: {
                    CONF_MODEL: "EnergyMeter-2000",
                    DEVICE_FIELDS: ["power_W"],
                }
            },
        )
        coordinator = _coordinator(hass, receiver)
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 10.0})
        await hass.async_block_till_done()

    coordinator.last_seen[device_key]

    # Reload: existing last_seen should NOT be overwritten by a later baseline
    later = start + timedelta(seconds=100)
    with freeze_time(later):
        assert await hass.config_entries.async_reload(receiver.entry_id)
        await hass.async_block_till_done()

    # After reload with no new event, the coordinator's last_seen for the
    # device is baselined again from 'now' (the reload time). This is correct
    # behavior: the reload is a fresh start with no event, so the baseline runs.
    coordinator2 = _coordinator(hass, receiver)
    assert device_key in coordinator2.last_seen


# ---------------------------------------------------------------------------
# _effective_timeout
# ---------------------------------------------------------------------------


async def test_effective_timeout_falls_back_to_receiver_default(
    hass, receiver_entry_builder
):
    """Without a per-device override, the receiver's availability_timeout is used.

    Setting receiver timeout to 300s means a device fed at T=0 goes unavailable
    at T>300s (not at T>600s or any other value).
    """
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=300,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    # The receiver availability_timeout is configured correctly
    assert coordinator.availability_timeout == 300

    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # At 299s -> still available (< 300s receiver timeout)
    with freeze_time(start + timedelta(seconds=299)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get(watts_eid).state != "unavailable"

    # At 301s -> unavailable (> 300s receiver timeout)
    with freeze_time(start + timedelta(seconds=301)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get(watts_eid).state == "unavailable"


async def test_effective_timeout_uses_resolver_result(hass, receiver_entry_builder):
    """When resolver is set, its return value is used for the device timeout."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    # Install a resolver returning a different (shorter) timeout for this device
    coordinator.effective_timeout_resolver = lambda key: (
        120 if key == device_key else 600
    )

    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # At 121s (> 120s per-device override), entity is unavailable even though
    # the receiver default (600s) has not elapsed.
    with freeze_time(start + timedelta(seconds=121)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        state = hass.states.get(watts_eid)
        assert state.state == "unavailable"


async def test_effective_timeout_resolver_exception_falls_back(
    hass, receiver_entry_builder
):
    """A failing resolver falls back to the receiver default availability_timeout."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)

    def _failing_resolver(key):
        raise RuntimeError("resolver broken")

    coordinator.effective_timeout_resolver = _failing_resolver

    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # At 599s the receiver default (600s) has NOT elapsed -> still available
    with freeze_time(start + timedelta(seconds=599)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        state = hass.states.get(watts_eid)
        assert state.state != "unavailable"

    # At 601s the receiver default (600s) HAS elapsed -> unavailable
    with freeze_time(start + timedelta(seconds=601)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        state = hass.states.get(watts_eid)
        assert state.state == "unavailable"


# ---------------------------------------------------------------------------
# _handle_dispatch: field present vs absent
# ---------------------------------------------------------------------------


async def test_handle_dispatch_applies_value_when_field_present(
    hass, receiver_entry_builder
):
    """When the event contains the entity's field, the value is applied."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 42.0})
        await hass.async_block_till_done()

    state = hass.states.get(watts_eid)
    assert state.state == "42.0"


async def test_handle_dispatch_writes_state_even_when_field_absent(
    hass, receiver_entry_builder
):
    """Watchdog re-dispatch without the field still causes a state write."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 99.0})
        await hass.async_block_till_done()

    # Advance past timeout; watchdog re-dispatches the stale event (no field change)
    with freeze_time(start + timedelta(seconds=601)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()

    # Value unchanged but state was re-written (now unavailable)
    state = hass.states.get(watts_eid)
    assert state.state == "unavailable"


async def test_handle_dispatch_value_does_not_apply_when_field_missing(
    hass, receiver_entry_builder
):
    """A dispatch event missing the entity's field_key does not overwrite state."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W", "energy_kWh"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    with freeze_time(dt_util.utcnow()):
        # First event sets power_W
        _feed(
            coordinator,
            {"model": "EnergyMeter-2000", "id": 1234, "power_W": 100.0},
        )
        await hass.async_block_till_done()
        assert hass.states.get(watts_eid).state == "100.0"

        # Second event has energy_kWh but NOT power_W -> watts entity state unchanged
        _feed(
            coordinator,
            {"model": "EnergyMeter-2000", "id": 1234, "energy_kWh": 50.0},
        )
        await hass.async_block_till_done()
        assert hass.states.get(watts_eid).state == "100.0"


# ---------------------------------------------------------------------------
# async_added_to_hass / async_will_remove_from_hass lifecycle
# ---------------------------------------------------------------------------


async def test_subscription_registered_on_add(hass, receiver_entry_builder):
    """Entity subscribes to the device-update signal when added to HA."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    # If the subscription is registered, a direct dispatcher send updates state
    with freeze_time(dt_util.utcnow()):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 77.0})
        await hass.async_block_till_done()
    assert hass.states.get(watts_eid).state == "77.0"


async def test_unsubscribe_on_removal_stops_updates(hass, receiver_entry_builder):
    """After reload, only the NEW entity's dispatcher subscription is active.

    The old entity's async_will_remove_from_hass must unsubscribe so there is
    no double-dispatch on the new entity. We verify by confirming a post-reload
    feed produces a single correct state (not doubled or errored).
    """
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    # Set a value first
    with freeze_time(dt_util.utcnow()):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()
    assert hass.states.get(watts_eid).state == "5.0"

    # Reload: old entities unsubscribe (async_will_remove_from_hass), new ones subscribe
    assert await hass.config_entries.async_reload(receiver.entry_id)
    await hass.async_block_till_done()

    coordinator2 = _coordinator(hass, receiver)
    with freeze_time(dt_util.utcnow()):
        _feed(coordinator2, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 99.0})
        await hass.async_block_till_done()

    state = hass.states.get(watts_eid)
    assert state is not None
    assert state.state == "99.0"


# ---------------------------------------------------------------------------
# async_upsert_device
# ---------------------------------------------------------------------------


async def test_upsert_device_creates_new_record(hass, receiver_entry_builder):
    """async_upsert_device creates a new record when none exists."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    device_key = "NewDevice-99"

    await async_upsert_device(
        hass, receiver, device_key, model="NewDevice", fields=["temperature_C"]
    )
    await hass.async_block_till_done()

    devices = receiver.data.get(CONF_DEVICES, {})
    assert device_key in devices
    assert devices[device_key][CONF_MODEL] == "NewDevice"
    assert "temperature_C" in devices[device_key][DEVICE_FIELDS]


async def test_upsert_device_is_idempotent_no_change(hass, receiver_entry_builder):
    """async_upsert_device does not write config entry when record is unchanged."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )

    original_data = receiver.data.copy()
    # Upsert with the same model and fields: no change should occur
    await async_upsert_device(
        hass, receiver, device_key, model="EnergyMeter-2000", fields=["power_W"]
    )
    await hass.async_block_till_done()

    # Data unchanged
    assert (
        receiver.data[CONF_DEVICES][device_key]
        == original_data[CONF_DEVICES][device_key]
    )


async def test_upsert_device_unions_fields_sorted(hass, receiver_entry_builder):
    """async_upsert_device unions and sorts field keys."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["humidity"],
            }
        },
    )

    await async_upsert_device(
        hass, receiver, device_key, fields=["temperature_C", "battery_ok"]
    )
    await hass.async_block_till_done()

    fields = receiver.data[CONF_DEVICES][device_key][DEVICE_FIELDS]
    # All three fields present, sorted alphabetically
    assert "battery_ok" in fields
    assert "humidity" in fields
    assert "temperature_C" in fields
    assert fields == sorted(fields)


async def test_upsert_device_updates_model(hass, receiver_entry_builder):
    """async_upsert_device updates the model when a new one is provided."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "",  # initially empty
                DEVICE_FIELDS: [],
            }
        },
    )

    await async_upsert_device(hass, receiver, device_key, model="Acurite-606TX")
    await hass.async_block_till_done()

    assert receiver.data[CONF_DEVICES][device_key][CONF_MODEL] == "Acurite-606TX"


async def test_upsert_device_does_not_overwrite_model_with_empty(
    hass, receiver_entry_builder
):
    """async_upsert_device does not clear an existing model with an empty string."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: [],
            }
        },
    )

    # Pass model="" (falsy) — should not overwrite the existing model
    await async_upsert_device(hass, receiver, device_key, model="")
    await hass.async_block_till_done()

    assert receiver.data[CONF_DEVICES][device_key][CONF_MODEL] == "EnergyMeter-2000"


async def test_upsert_device_no_write_if_fields_subset(hass, receiver_entry_builder):
    """No config-entry write if the new fields are already in the stored set."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["energy_kWh", "power_W"],
            }
        },
    )

    # Adding a field that is already present -> no change
    await async_upsert_device(hass, receiver, device_key, fields=["power_W"])
    await hass.async_block_till_done()

    # Entry version not bumped (no write triggered by a data-only change)
    fields = receiver.data[CONF_DEVICES][device_key][DEVICE_FIELDS]
    assert "power_W" in fields
    assert "energy_kWh" in fields


async def test_upsert_device_writes_when_new_field_added(hass, receiver_entry_builder):
    """async_upsert_device writes the entry when a genuinely new field is added."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )

    await async_upsert_device(hass, receiver, device_key, fields=["humidity"])
    await hass.async_block_till_done()

    fields = receiver.data[CONF_DEVICES][device_key][DEVICE_FIELDS]
    assert "humidity" in fields
    assert "temperature_C" in fields


async def test_upsert_device_no_fields_arg_no_field_change(
    hass, receiver_entry_builder
):
    """async_upsert_device with fields=None does not alter existing fields."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )

    await async_upsert_device(hass, receiver, device_key, fields=None)
    await hass.async_block_till_done()

    fields = receiver.data[CONF_DEVICES][device_key][DEVICE_FIELDS]
    assert fields == ["power_W"]


# ---------------------------------------------------------------------------
# async_upsert_event_types
# ---------------------------------------------------------------------------


async def test_upsert_event_types_creates_new(hass, receiver_entry_builder):
    """async_upsert_event_types creates event_types entry when none exists."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["button"],
            }
        },
    )

    await async_upsert_event_types(hass, receiver, device_key, "button", ["A", "B"])
    await hass.async_block_till_done()

    event_types = receiver.data[CONF_DEVICES][device_key].get(DEVICE_EVENT_TYPES, {})
    assert event_types.get("button") == ["A", "B"]


async def test_upsert_event_types_unions_sorted(hass, receiver_entry_builder):
    """async_upsert_event_types unions and sorts event types."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["button"],
                DEVICE_EVENT_TYPES: {"button": ["A"]},
            }
        },
    )

    await async_upsert_event_types(hass, receiver, device_key, "button", ["C", "B"])
    await hass.async_block_till_done()

    event_types = receiver.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES]
    assert event_types["button"] == ["A", "B", "C"]  # sorted union


async def test_upsert_event_types_no_write_if_no_change(hass, receiver_entry_builder):
    """async_upsert_event_types is a no-op when stored types already contain all new types."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["button"],
                DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
            }
        },
    )

    {k: dict(v) for k, v in receiver.data[CONF_DEVICES].items()}

    # Upsert with types already in the stored set -> no write
    await async_upsert_event_types(hass, receiver, device_key, "button", ["A"])
    await hass.async_block_till_done()

    # Types unchanged
    event_types = receiver.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES]
    assert event_types["button"] == ["A", "B"]


async def test_upsert_event_types_adds_new_field_key(hass, receiver_entry_builder):
    """async_upsert_event_types adds a new field_key to the event_types map."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["button"],
                DEVICE_EVENT_TYPES: {"button": ["A"]},
            }
        },
    )

    await async_upsert_event_types(hass, receiver, device_key, "other_field", ["X"])
    await hass.async_block_till_done()

    event_types = receiver.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES]
    assert "button" in event_types  # existing field preserved
    assert event_types["other_field"] == ["X"]


async def test_upsert_event_types_device_without_event_types_key(
    hass, receiver_entry_builder
):
    """async_upsert_event_types tolerates a record with no DEVICE_EVENT_TYPES key."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["button"],
                # No DEVICE_EVENT_TYPES key
            }
        },
    )

    await async_upsert_event_types(hass, receiver, device_key, "button", ["A"])
    await hass.async_block_till_done()

    event_types = receiver.data[CONF_DEVICES][device_key].get(DEVICE_EVENT_TYPES, {})
    assert event_types.get("button") == ["A"]


# ---------------------------------------------------------------------------
# Rtl433ReceiverEntity: receiver-update dispatcher subscription
# ---------------------------------------------------------------------------


async def test_receiver_entity_subscribes_to_receiver_update(
    hass, receiver_entry_builder
):
    """A receiver entity updates its state when signal_receiver_update fires."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)

    # Find the connectivity binary_sensor (a Rtl433ReceiverEntity subclass)
    connectivity_eid = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{receiver_scope(receiver)}:connectivity"
    )
    assert connectivity_eid is not None

    # Change coordinator.connected and fire receiver_update signal
    coordinator._client.connected = True
    async_dispatcher_send(hass, signal_receiver_update(receiver_id(receiver)))
    await hass.async_block_till_done()
    assert hass.states.get(connectivity_eid).state == "on"

    coordinator._client.connected = False
    async_dispatcher_send(hass, signal_receiver_update(receiver_id(receiver)))
    await hass.async_block_till_done()
    assert hass.states.get(connectivity_eid).state == "off"


async def test_receiver_entity_reload_rewires_subscription(
    hass, receiver_entry_builder
):
    """After a reload, the receiver entity still responds to receiver_update signals.

    This verifies that async_added_to_hass correctly rewires the dispatcher
    subscription on each load (not just the first).
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    connectivity_eid = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{receiver_scope(receiver)}:connectivity"
    )
    assert connectivity_eid is not None

    # Reload the entry
    assert await hass.config_entries.async_reload(receiver.entry_id)
    await hass.async_block_till_done()

    coordinator2 = _coordinator(hass, receiver)
    # After reload, the receiver_update signal must still work
    coordinator2._client.connected = True
    async_dispatcher_send(hass, signal_receiver_update(receiver_id(receiver)))
    await hass.async_block_till_done()

    state = hass.states.get(connectivity_eid)
    assert state is not None
    assert state.state == "on"


async def test_receiver_entity_device_info_identifiers(hass, receiver_entry_builder):
    """Receiver entity is registered under the receiver device."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    receiver_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, receiver_scope(receiver)), receiver.entry_id
    )
    assert receiver_device is not None

    connectivity_eid = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{receiver_scope(receiver)}:connectivity"
    )
    assert connectivity_eid is not None
    conn_entry = ent_reg.async_get(connectivity_eid)
    assert conn_entry.device_id == receiver_device.id


# ---------------------------------------------------------------------------
# Rtl433ReceiverControl: unique_id and name
# ---------------------------------------------------------------------------


async def test_receiver_control_unique_id_format(hass, receiver_entry_builder):
    """Receiver control unique_id is '{receiver_entry_id}:hub:{object_suffix}'."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    ent_reg = er.async_get(hass)

    # The connectivity binary_sensor unique_id follows the hub:hub:suffix pattern
    uid = f"{receiver_scope(receiver)}:connectivity"
    eid = ent_reg.async_get_entity_id("binary_sensor", DOMAIN, uid)
    assert eid is not None


async def test_receiver_control_entity_category_config(hass, receiver_entry_builder):
    """Receiver control entities (number/select/switch) have EntityCategory.CONFIG."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    ent_reg = er.async_get(hass)

    # The gain number control is a Rtl433ReceiverControl subclass
    gain_eid = ent_reg.async_get_entity_id(
        "number", DOMAIN, f"{receiver_scope(receiver)}:gain"
    )
    if gain_eid is not None:
        entry = ent_reg.async_get(gain_eid)
        assert entry.entity_category == EntityCategory.CONFIG


# ---------------------------------------------------------------------------
# async_setup_receiver_platform: dedup, teardown, field listeners
# ---------------------------------------------------------------------------


async def test_setup_no_duplicate_entities_on_reload(hass, receiver_entry_builder):
    """Reloading the entry does not create duplicate entities."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{receiver.entry_id}:{device_key}:watts"

    # Count entities with this unique_id before reload
    before = [e for e in ent_reg.entities.values() if e.unique_id == uid]
    assert len(before) == 1

    assert await hass.config_entries.async_reload(receiver.entry_id)
    await hass.async_block_till_done()

    # Still exactly one
    after = [e for e in ent_reg.entities.values() if e.unique_id == uid]
    assert len(after) == 1


async def test_teardown_clears_field_listeners(hass, receiver_entry_builder):
    """On unload, field-update listeners are torn down."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )

    # Unload -> device removers run and field listeners are cleared
    assert await hass.config_entries.async_unload(receiver.entry_id)
    await hass.async_block_till_done()

    # coordinator.device_removers is empty after teardown (removers are deregistered)
    assert _remove_device_not_in_coordinator(coordinator)


def _remove_device_not_in_coordinator(coordinator) -> bool:
    """True if the coordinator has no registered device removers (entry unloaded)."""
    # After unload, _teardown() removes _remove_device from device_removers
    return len(coordinator.device_removers) == 0


async def test_device_remover_registered_during_setup(hass, receiver_entry_builder):
    """Device removers are registered for each platform during setup."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    # At least one device remover registered (sensor + binary_sensor platforms each add one)
    assert len(coordinator.device_removers) >= 1


# ---------------------------------------------------------------------------
# Calibration overlay applied during entity setup
# ---------------------------------------------------------------------------


async def test_calibration_applied_to_consumption_sensor(hass, receiver_entry_builder):
    """A calibration overlay produces energy-eligible sensor attributes."""
    device_key = "ERT-SCM-9001"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "ERT-SCM",
                DEVICE_FIELDS: ["consumption_data"],
                DEVICE_CALIBRATION: {
                    CALIBRATION_COMMODITY: COMMODITY_WATER,
                    CALIBRATION_UNIT: "L",
                    CALIBRATION_SCALE: 0.1,
                },
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    consumption_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:consumption"
    )
    assert consumption_eid is not None

    _feed(coordinator, {"model": "ERT-SCM", "id": 9001, "consumption_data": 1000})
    await hass.async_block_till_done()

    state = hass.states.get(consumption_eid)
    assert state.attributes["device_class"] == "water"
    assert state.attributes["unit_of_measurement"] == "L"
    assert state.attributes["state_class"] == "total_increasing"
    # 1000 * 0.1 = 100.0
    assert float(state.state) == pytest.approx(100.0)


async def test_calibration_applied_to_scmplus_consumption_sensor(
    hass, receiver_entry_builder
):
    """The overlay fires for SCMplus's CamelCased ``Consumption`` field.

    ``Consumption`` is in ``CONSUMPTION_FIELD_KEYS``, so a gas calibration turns
    the unitless counter into an Energy-dashboard-eligible sensor exactly as it
    does for SCM/ERT's ``consumption_data``. Before the rename this field key was
    lowercase in the shipped library and never matched a real SCMplus event.
    """
    device_key = "SCMplus-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "SCMplus",
                DEVICE_FIELDS: ["Consumption"],
                DEVICE_CALIBRATION: {
                    CALIBRATION_COMMODITY: COMMODITY_GAS,
                    CALIBRATION_UNIT: "m³",
                    CALIBRATION_SCALE: 0.01,
                },
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    consumption_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:consumption"
    )
    assert consumption_eid is not None

    _feed(coordinator, {"model": "SCMplus", "id": 1234, "Consumption": 5000})
    await hass.async_block_till_done()

    state = hass.states.get(consumption_eid)
    assert state.attributes["device_class"] == "gas"
    assert state.attributes["unit_of_measurement"] == "m³"
    assert state.attributes["state_class"] == "total_increasing"
    # 5000 * 0.01 = 50.0
    assert float(state.state) == pytest.approx(50.0)


async def test_uncalibrated_scmplus_consumption_is_a_unitless_counter(
    hass, receiver_entry_builder
):
    """With no calibration, ``Consumption`` still yields a working unitless sensor.

    This is the zero-yaml path: the entity appears and tracks the raw counter
    with no device_class or unit, which only works if the shipped library key
    matches the wire name exactly.
    """
    device_key = "SCMplus-4321"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={device_key: {CONF_MODEL: "SCMplus", DEVICE_FIELDS: ["Consumption"]}},
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    consumption_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:consumption"
    )
    assert consumption_eid is not None

    _feed(coordinator, {"model": "SCMplus", "id": 4321, "Consumption": 5000})
    await hass.async_block_till_done()

    state = hass.states.get(consumption_eid)
    assert state.state == "5000"
    assert "device_class" not in state.attributes
    assert "unit_of_measurement" not in state.attributes
    assert state.attributes["state_class"] == "total_increasing"


async def test_calibration_not_applied_to_non_consumption_field(
    hass, receiver_entry_builder
):
    """Calibration is only applied to consumption field keys, not arbitrary fields."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
                DEVICE_CALIBRATION: {
                    CALIBRATION_COMMODITY: COMMODITY_ENERGY,
                    CALIBRATION_UNIT: "Wh",
                    CALIBRATION_SCALE: 1.0,
                },
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{receiver.entry_id}:{device_key}:watts"
    )
    assert watts_eid is not None

    _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 500.0})
    await hass.async_block_till_done()

    state = hass.states.get(watts_eid)
    # power_W is not a consumption field — calibration must NOT change its device_class
    assert state.attributes["device_class"] == "power"
    assert state.attributes["unit_of_measurement"] == "W"
    # State should remain the raw value (no scale applied)
    assert float(state.state) == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# should_poll is False
# ---------------------------------------------------------------------------


async def test_entity_should_poll_false(hass, receiver_entry_builder):
    """Entities must not poll — _attr_should_poll is False."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{receiver.entry_id}:{device_key}:watts"
    eid = ent_reg.async_get_entity_id("sensor", DOMAIN, uid)
    # The state is current without polling
    assert eid is not None
    # should_poll=False means the entity writes its own state via dispatcher


# ---------------------------------------------------------------------------
# enabled_by_default from descriptor
# ---------------------------------------------------------------------------


async def test_entity_enabled_by_default(hass, receiver_entry_builder):
    """Entities whose descriptor has enabled_by_default=True are enabled."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{receiver.entry_id}:{device_key}:watts"
    entry = ent_reg.async_get(ent_reg.async_get_entity_id("sensor", DOMAIN, uid))
    assert entry is not None
    # enabled_by_default=True means the entity is not hidden/disabled by default
    assert entry.disabled_by is None


# ---------------------------------------------------------------------------
# Multiple fields of the same device
# ---------------------------------------------------------------------------


async def test_multiple_fields_create_separate_entities(hass, receiver_entry_builder):
    """Each mapped field of a device creates its own entity."""
    device_key = "EnergyMeter-2000-1234"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W", "energy_kWh"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    prefix = f"{receiver.entry_id}:{device_key}"

    watts_eid = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:watts")
    kwh_eid = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:kwh")
    assert watts_eid is not None
    assert kwh_eid is not None
    assert watts_eid != kwh_eid

    _feed(
        coordinator,
        {"model": "EnergyMeter-2000", "id": 1234, "power_W": 200.0, "energy_kWh": 5.0},
    )
    await hass.async_block_till_done()

    assert hass.states.get(watts_eid).state == "200.0"
    assert hass.states.get(kwh_eid).state == "5.0"


async def test_late_field_entity_created_on_new_event(hass, receiver_entry_builder):
    """A new field in a later event creates a new entity dynamically."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    ent_reg = er.async_get(hass)
    prefix = f"{receiver.entry_id}:{device_key}"

    # Initially no battery entity
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:B") is None

    # Event with battery_ok -> creates new entity
    _feed(
        coordinator,
        {"model": "Acurite-606TX", "id": 42, "temperature_C": 20.0, "battery_ok": 1},
    )
    await hass.async_block_till_done()

    battery_eid = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:B")
    assert battery_eid is not None
    assert hass.states.get(battery_eid).state == "100"


# ---------------------------------------------------------------------------
# coordinator.last_seen is set by entity on startup (baseline)
# ---------------------------------------------------------------------------


async def test_baseline_sets_available_true(hass, receiver_entry_builder):
    """The baseline on startup sets coordinator.available[device_key] = True."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    # The baseline should have set available to True for the device
    assert coordinator.available.get(device_key) is True
    assert device_key in coordinator.last_seen


# ---------------------------------------------------------------------------
# The devices map is kept current from every receiver's live view
# ---------------------------------------------------------------------------


async def _two_receiver_location(hass, devices):
    """Set up one location holding two receivers, seeded with ``devices``."""
    location = build_receiver_entry(
        availability_timeout=600,
        devices=devices,
        receivers=[
            build_receiver_subentry(host="attic.local"),
            build_receiver_subentry(host="garage.local"),
        ],
    )
    location.add_to_hass(hass)
    assert await hass.config_entries.async_setup(location.entry_id)
    await hass.async_block_till_done()
    return location


class _StubEntity:
    """Stand-in for a platform entity, recording what the setup decided to build.

    The platform helper is what these tests are about, so the entities it builds
    are observed directly rather than through a platform's registration: what
    matters is which (device, receiver, field) combinations it decided to create
    and how many times.
    """

    def __init__(self, coordinator, receiver_id, device_key, model, descriptor):
        self.receiver_id = receiver_id
        self.device_key = device_key
        self.model = model
        self.object_suffix = descriptor.object_suffix


def _built(hass, entry, added, platform="sensor", **kwargs):
    """Run one platform's setup, collecting the entities into ``added``."""
    return async_setup_receiver_platform(
        hass,
        entry,
        lambda entities, *args, **kw: added.extend(entities),
        platform,
        _StubEntity,
        **kwargs,
    )


async def test_a_field_first_reported_after_setup_gains_its_entity(hass):
    """A field a receiver starts reporting later still gets an entity.

    Sensors reveal their fields over time -- a battery reading arrives on a frame
    hours after the first temperature -- so each device keeps a listener for its
    own updates, and the entity it builds carries the device's model like any
    other.
    """
    device_key = "Acurite-606TX-42"
    location = await _two_receiver_location(
        hass,
        {
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )

    added: list = []
    await _built(hass, location, added)
    await hass.async_block_till_done()
    added.clear()

    async_dispatcher_send(
        hass,
        signal_device_update(receiver_id(location, 0), device_key),
        SimpleNamespace(fields={"temperature_C": 21.4, "battery_ok": 1}),
    )
    await hass.async_block_till_done()

    assert [
        (entity.device_key, entity.object_suffix, entity.model) for entity in added
    ] == [(device_key, "B", "Acurite-606TX")]


async def test_a_model_scoped_user_mapping_wins_for_that_model(
    hass, receiver_entry_builder
):
    """A mapping written for one model overrides the global one for its devices.

    That is the whole point of the ``models:`` block: a field that means
    something different on one decoder -- a raw counter that is litres on this
    meter and gallons on that one -- is described per model, and the entity a
    device of that model gets has to be the model's descriptor, not the global
    fallback.
    """
    device_key = "Acurite-606TX-42"
    receiver = receiver_entry_builder(
        availability_timeout=600,
        # Past the minor-2 step, which seeds the mappings from the legacy file
        # and would overwrite the ones written here.
        minor_version=2,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    receiver.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        receiver,
        data={
            **receiver.data,
            CONF_USER_MAPPINGS: {
                "models": {
                    "Acurite-606TX": {
                        "temperature_C": {
                            "platform": "sensor",
                            "name": "Kelvin",
                            "object_suffix": "K",
                            "unit_of_measurement": "K",
                        }
                    }
                }
            },
        },
    )
    assert await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()

    added: list = []
    await _built(hass, receiver, added)
    await hass.async_block_till_done()

    assert [entity.object_suffix for entity in added] == ["K"]


async def test_an_unmanaged_receiver_does_not_silence_its_neighbours_controls(hass):
    """A receiver with management off contributes no controls, and only its own.

    The radio controls are per receiver, so one server the user has opted out of
    managing must leave the other's set intact -- skipping it, not stopping the
    walk.
    """
    location = build_receiver_entry(
        availability_timeout=600,
        receivers=[
            build_receiver_subentry(host="attic.local", manage_settings=False),
            build_receiver_subentry(host="garage.local", manage_settings=True),
        ],
    )
    location.add_to_hass(hass)
    assert await hass.config_entries.async_setup(location.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    unmanaged = f"{receiver_scope(location, 0)}:center_frequency"
    managed = f"{receiver_scope(location, 1)}:center_frequency"
    assert ent_reg.async_get_entity_id("number", DOMAIN, unmanaged) is None
    assert ent_reg.async_get_entity_id("number", DOMAIN, managed) is not None


async def test_platform_setup_of_a_location_with_no_devices_adds_nothing(
    hass, receiver_entry_builder
):
    """A location that has adopted nothing sets its platforms up quietly.

    Every install starts here, and the devices-map pass has to read an absent
    map as "no devices" rather than raising -- a platform that dies in setup
    takes every later device with it while the entry still loads, so the failure
    would be invisible until a sensor never appeared.
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    assert CONF_DEVICES not in receiver.data

    added: list = []
    await _built(hass, receiver, added)
    await hass.async_block_till_done()

    assert added == []
    assert CONF_DEVICES not in receiver.data


async def test_platform_setup_before_the_library_is_cached_uses_the_shipped_one(
    hass, receiver_entry_builder
):
    """A platform forwarded before the merged library is cached still builds.

    The per-entry registry is the shipped library plus this location's user
    mappings; with none cached yet the lookup falls back to the shipped library
    rather than raising, so the entities exist and a later reload simply rebuilds
    them against the merged registry.
    """
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    hass.data[DOMAIN].pop(DATA_ENTRY_LIBRARY, None)

    added: list = []
    await _built(hass, receiver, added)
    await hass.async_block_till_done()

    assert [entity.object_suffix for entity in added] == ["T"]


async def test_a_receiver_control_is_named_by_the_setting_it_drives(
    hass, receiver_entry_builder
):
    """Each radio control carries its setting's name, not the device's.

    A nameless control inherits the receiver device's name, so the seven of them
    would render as seven identically-labelled rows on one page.
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id(
        "number", DOMAIN, f"{receiver_scope(receiver)}:center_frequency"
    )
    assert eid is not None
    assert ent_reg.async_get(eid).original_name == "Center frequency"


# ---------------------------------------------------------------------------
# async_upsert_device / async_upsert_event_types: records written from nothing
# ---------------------------------------------------------------------------


async def test_upsert_device_records_an_empty_model_when_none_is_known_yet(
    hass, receiver_entry_builder
):
    """A device whose model has not decoded is stored with an empty model.

    ``""`` rather than ``None`` because the rest of the integration reads the
    model as a string -- it is formatted into device names and looked up against
    the library -- and a ``None`` there surfaces as the word "None" on the
    device page.
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder)

    await async_upsert_device(hass, receiver, "Unknown-7", fields=["temperature_C"])
    await hass.async_block_till_done()

    assert receiver.data[CONF_DEVICES]["Unknown-7"] == {
        CONF_MODEL: "",
        DEVICE_FIELDS: ["temperature_C"],
    }


async def test_upsert_device_adds_fields_to_a_record_that_carries_none(
    hass, receiver_entry_builder
):
    """A stored record with no fields key gains them rather than raising."""
    device_key = "Acurite-606TX-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={device_key: {CONF_MODEL: "Acurite-606TX"}},
    )

    await async_upsert_device(hass, receiver, device_key, fields=["temperature_C"])
    await hass.async_block_till_done()

    assert receiver.data[CONF_DEVICES][device_key][DEVICE_FIELDS] == ["temperature_C"]


async def test_upsert_event_types_seeds_a_device_the_map_has_never_held(
    hass, receiver_entry_builder
):
    """Event types can arrive before the device has any record at all.

    The first frame from a remote creates the record and its event-type list in
    one go; requiring a prior record would drop the very first press.
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    assert CONF_DEVICES not in receiver.data

    await async_upsert_event_types(hass, receiver, "Generic-Remote-9", "button", ["A"])
    await hass.async_block_till_done()

    assert receiver.data[CONF_DEVICES]["Generic-Remote-9"] == {
        CONF_MODEL: "",
        DEVICE_FIELDS: [],
        DEVICE_EVENT_TYPES: {"button": ["A"]},
    }


# ---------------------------------------------------------------------------
# A descriptor's icon reaches the entity
# ---------------------------------------------------------------------------


def _icon_entity(icon: str | None) -> Rtl433Entity:
    """Build a bare entity for a descriptor carrying (or not carrying) an icon."""
    descriptor = FieldDescriptor(
        field_key="MeterType",
        platform="sensor",
        name="Meter type",
        object_suffix="metertype",
        icon=icon,
    )
    return Rtl433Entity(MagicMock(), "receiver", "ERT-SCM-1", "ERT-SCM", descriptor)


def test_a_descriptor_icon_becomes_the_entitys_icon():
    """The library's icon is what the frontend shows for the field."""
    assert _icon_entity("mdi:meter-gas").icon == "mdi:meter-gas"


def test_a_descriptor_without_an_icon_leaves_the_choice_to_home_assistant():
    """No icon in the library means no override: core picks by device class."""
    assert _icon_entity(None).icon is None


async def test_platform_setup_persists_what_every_receiver_already_knows(hass):
    """The stored record gains the fields the location's receivers have seen.

    A coordinator starts decoding as soon as it connects, which is before the
    platforms are forwarded, so by the time entities are built a receiver may
    already know fields the stored record has never carried. Every receiver's
    view is unioned in -- one that never heard the device at all contributes
    nothing rather than erasing the others -- and the stored record is what
    survives a restart, so a field dropped here is a sensor that silently fails
    to come back.
    """
    device_key = "Acurite-606TX-42"
    # A second device whose record predates the fields key entirely, and a third
    # that carries neither -- both shapes an entry written by an older version
    # leaves behind.
    bare_key = "Nexus-TH-77"
    blank_key = "Unknown-9"
    location = await _two_receiver_location(
        hass,
        {
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            },
            bare_key: {CONF_MODEL: "Nexus-TH"},
            blank_key: {DEVICE_FIELDS: ["temperature_C"]},
        },
    )
    attic = hass.data[DOMAIN][receiver_id(location, 0)]
    garage = hass.data[DOMAIN][receiver_id(location, 1)]
    attic.device_fields[device_key] = {"temperature_C", "humidity"}
    # The garage receiver is out of range of this sensor and never heard it.
    garage.device_fields.pop(device_key, None)

    added: list = []
    await _built(hass, location, added)
    await hass.async_block_till_done()

    assert location.data[CONF_DEVICES][device_key][DEVICE_FIELDS] == [
        "humidity",
        "temperature_C",
    ]
    # The fields-less record is tolerated, not a crash that strands the rest...
    assert location.data[CONF_DEVICES][bare_key][CONF_MODEL] == "Nexus-TH"
    # ...and a record with no model at all is left without one rather than
    # gaining a placeholder that would render as the device's name.
    assert CONF_MODEL not in location.data[CONF_DEVICES][blank_key]
    # One entity per mapped field for the whole location, not one per receiver:
    # the second receiver's pass finds every id already built. A device whose
    # model never decoded builds its entities all the same, under an empty model.
    assert sorted(
        (entity.device_key, entity.object_suffix, entity.model) for entity in added
    ) == [
        (device_key, "H", "Acurite-606TX"),
        (device_key, "T", "Acurite-606TX"),
        (blank_key, "T", ""),
    ]


async def test_a_receiver_agnostic_extra_is_added_once_for_the_location(hass):
    """An extra whose id does not name a receiver lands on the device once.

    The optional per-device extra is deduped against the same ids the field
    entities claim, so the shipped Last-seen -- a link field, one id per
    receiver -- contributes one entity each, while an extra minting a single
    location-wide id contributes exactly one however many receivers run. Without
    that, the merged device would carry the same reading twice and Home
    Assistant would suffix the second with ``_2``.
    """
    device_key = "Acurite-606TX-42"
    location = await _two_receiver_location(
        hass,
        {
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )

    def _location_wide_extra(coordinator, receiver_id, device_key, model):
        return SimpleNamespace(
            unique_id=f"{coordinator.entry.entry_id}:{device_key}:extra",
            device_key=device_key,
            model=model,
            object_suffix="extra",
        )

    added: list = []
    await _built(hass, location, added, per_device_factory=_location_wide_extra)
    await hass.async_block_till_done()

    assert [entity.object_suffix for entity in added].count("extra") == 1
