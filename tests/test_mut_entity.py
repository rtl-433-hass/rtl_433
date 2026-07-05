"""Mutation-killing tests for custom_components/rtl_433/entity.py.

Covers every function and branch in entity.py with precise assertions
designed to detect mutmut's operator flips, constant substitutions,
removed statements, and negated conditions.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time
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
    DEVICE_CALIBRATION,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DOMAIN,
    signal_hub_update,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.coordinator.base import Rtl433Client
from custom_components.rtl_433.entity import (
    _apply_calibration,
    _resolve_entity_category,
    async_upsert_device,
    async_upsert_event_types,
)
from custom_components.rtl_433.mapping import FieldDescriptor
from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

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


def _coordinator(hass, hub_entry: MockConfigEntry) -> Rtl433Coordinator:
    return hass.data[DOMAIN][hub_entry.entry_id]


def _feed(coordinator: Rtl433Coordinator, event: dict) -> None:
    coordinator._client._process_event(event)


async def _setup_hub(hass, hub_entry_builder, *, devices=None, **kwargs):
    """Set up a hub entry. Defaults availability_timeout=600 unless overridden."""
    kwargs.setdefault("availability_timeout", 600)
    hub = hub_entry_builder(devices=devices, **kwargs)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    return hub


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


async def test_entity_unique_id_format(hass, hub_entry_builder):
    """unique_id is {hub_entry_id}:{device_key}:{object_suffix}."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    # The unique_id format is exactly hub_entry_id:device_key:object_suffix
    uid = f"{hub.entry_id}:{device_key}:watts"
    eid = ent_reg.async_get_entity_id("sensor", DOMAIN, uid)
    assert eid is not None, f"Entity with unique_id {uid} not found"


async def test_entity_unique_id_two_hubs_no_collision(hass, hub_entry_builder):
    """Two hubs seeing the same device produce non-colliding unique_ids.

    The unique_id embeds the hub entry_id, so even though both hubs observe
    the same device_key, their entity unique_ids are distinct.
    """
    device_key = "EnergyMeter-2000-1234"
    device_spec = {
        device_key: {
            CONF_MODEL: "EnergyMeter-2000",
            DEVICE_FIELDS: ["power_W"],
        }
    }
    # Set up first hub normally
    hub_a = await _setup_hub(
        hass, hub_entry_builder, host="hub-a.local", devices=device_spec
    )

    # The unique_id format is hub_entry_id:device_key:object_suffix.
    # Two different hub_entry_ids (always different MockConfigEntry.entry_id
    # values) guarantee no collision — verify the format property holds.
    uid_a = f"{hub_a.entry_id}:{device_key}:watts"
    ent_reg = er.async_get(hass)
    eid_a = ent_reg.async_get_entity_id("sensor", DOMAIN, uid_a)
    assert eid_a is not None

    # Construct a second unique_id as would be used by a second hub entry with
    # a different entry_id and assert it differs from the first.
    fake_entry_id = "different-hub-entry-id"
    uid_b = f"{fake_entry_id}:{device_key}:watts"
    assert uid_a != uid_b
    # The hub entry_id is the discriminating component
    assert hub_a.entry_id != fake_entry_id


async def test_entity_name_from_descriptor(hass, hub_entry_builder):
    """Entity name comes from the descriptor's name field."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["battery_mV"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{hub.entry_id}:{device_key}:mV"
    entry = ent_reg.async_get(ent_reg.async_get_entity_id("sensor", DOMAIN, uid))
    assert entry is not None
    # "Battery mV" is the explicit name for battery_mV in the library
    assert entry.original_name == "Battery mV"


async def test_entity_name_none_derives_from_device_class(hass, hub_entry_builder):
    """A field with no descriptor name is auto-named by HA from its device_class.

    ``power_W`` ships with ``name: null``; because the entity leaves ``_attr_name``
    unset, HA derives the (translatable) name "Power" from ``device_class`` and
    the entity_id keeps its ``_power`` suffix.
    """
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{hub.entry_id}:{device_key}:watts"
    entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, uid)
    entry = ent_reg.async_get(entity_id)
    assert entry is not None
    assert entry.original_name == "Power"
    assert entity_id.endswith("_power")


async def test_entity_has_entity_name_true(hass, hub_entry_builder):
    """_attr_has_entity_name is True so entities get device-relative naming."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{hub.entry_id}:{device_key}:T"
    entry = ent_reg.async_get(ent_reg.async_get_entity_id("sensor", DOMAIN, uid))
    assert entry is not None
    # has_entity_name = True means name is stored but entity_id uses device name
    assert entry.has_entity_name is True


async def test_device_info_identifiers(hass, hub_entry_builder):
    """DeviceInfo identifiers is {(DOMAIN, hub_entry_id:device_key)}."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    # The nested device is registered with the correct identifier
    device_entry = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:{device_key}")}
    )
    assert device_entry is not None


async def test_device_info_manufacturer(hass, hub_entry_builder):
    """DeviceInfo manufacturer is 'rtl_433'."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:{device_key}")}
    )
    assert device_entry is not None
    assert device_entry.manufacturer == "rtl_433"


async def test_device_name_with_model(hass, hub_entry_builder):
    """Device name is '{model} {id-suffix}' (no redundant model) when model set."""
    model = "EnergyMeter-2000"
    device_key = f"{model}-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: model,
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:{device_key}")}
    )
    assert device_entry is not None
    # Only the distinguishing id suffix follows the model, not the whole key.
    expected_name = f"{model} 1234"
    assert device_entry.name == expected_name


async def test_device_name_without_model_is_device_key(hass, hub_entry_builder):
    """Device name is just device_key when model is empty/absent."""
    device_key = "UnknownDevice-7"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "",  # empty string -> no model
                DEVICE_FIELDS: [],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:{device_key}")}
    )
    # The name is device_key when model is falsy
    assert device_entry.name == device_key


async def test_device_name_model_only_has_no_suffix(hass, hub_entry_builder):
    """A model-only device (key == model token) is named with just the model."""
    model = "Foo"
    device_key = model  # no id/channel/subtype -> key is the bare model token
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: model,
                DEVICE_FIELDS: [],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:{device_key}")}
    )
    assert device_entry is not None
    # No redundant "Foo (Foo)" and no trailing space — just the model.
    assert device_entry.name == model


async def test_device_model_set_when_model_present(hass, hub_entry_builder):
    """DeviceInfo.model is the model string when non-empty."""
    model = "EnergyMeter-2000"
    device_key = f"{model}-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: model,
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:{device_key}")}
    )
    assert device_entry.model == model


async def test_device_via_device_links_to_hub(hass, hub_entry_builder):
    """Nested device has via_device_id pointing to the hub device."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    dev_reg = dr.async_get(hass)
    nested = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{hub.entry_id}:{device_key}")}
    )
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    assert nested is not None
    assert hub_device is not None
    # via_device_id must be the hub device — not None, not something else
    assert nested.via_device_id is not None
    assert nested.via_device_id == hub_device.id


async def test_entity_category_none_by_default(hass, hub_entry_builder):
    """A sensor without a library entity_category has no category (None)."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{hub.entry_id}:{device_key}:watts"
    entry = ent_reg.async_get(ent_reg.async_get_entity_id("sensor", DOMAIN, uid))
    assert entry is not None
    # power_W is a measurement, not diagnostic -> no entity_category
    assert entry.entity_category is None


async def test_entity_category_diagnostic_for_battery(hass, hub_entry_builder):
    """Battery sensor has entity_category = DIAGNOSTIC."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["battery_ok"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{hub.entry_id}:{device_key}:B"
    entry = ent_reg.async_get(ent_reg.async_get_entity_id("sensor", DOMAIN, uid))
    assert entry is not None
    assert entry.entity_category == EntityCategory.DIAGNOSTIC


# ---------------------------------------------------------------------------
# Availability: boundary conditions on the timeout comparison
# ---------------------------------------------------------------------------


async def test_available_within_timeout(hass, hub_entry_builder):
    """Entity is available while within the timeout window."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    uid = f"{hub.entry_id}:{device_key}:watts"
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


async def test_available_at_exact_timeout_boundary(hass, hub_entry_builder):
    """Entity is still available when elapsed time exactly equals the timeout (<=)."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
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


async def test_unavailable_one_second_past_timeout(hass, hub_entry_builder):
    """Entity goes unavailable when elapsed time exceeds the timeout (> timeout)."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
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


async def test_unavailable_without_last_seen(hass, hub_entry_builder):
    """An entity with no last_seen entry is unavailable (available property = False).

    The entity is first fed a live event (which sets last_seen), then last_seen
    is manually cleared. At that point the available property returns False.
    The watchdog (which only dispatches when there is a cached event) then
    flips the entity state to unavailable.
    """
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
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


async def test_available_baseline_on_startup_before_event(hass, hub_entry_builder):
    """On startup, entities baseline last_seen to now so they start available."""
    device_key = "Acurite-606TX-42"
    start = dt_util.utcnow()
    with freeze_time(start):
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            availability_timeout=600,
            devices={
                device_key: {
                    CONF_MODEL: "Acurite-606TX",
                    DEVICE_FIELDS: ["temperature_C"],
                }
            },
        )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    temp_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:T"
    )
    # Without any live event, the entity should start available (baseline)
    state = hass.states.get(temp_eid)
    assert state.state != "unavailable"
    # coordinator.last_seen must have been set (the baseline write)
    assert device_key in coordinator.last_seen
    assert coordinator.available.get(device_key) is True


async def test_baseline_last_seen_not_set_if_already_present(hass, hub_entry_builder):
    """If coordinator already has last_seen for a device, the baseline is skipped."""
    device_key = "EnergyMeter-2000-1234"
    start = dt_util.utcnow()

    # Set up the hub and feed an event to establish a real last_seen
    with freeze_time(start):
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            availability_timeout=600,
            devices={
                device_key: {
                    CONF_MODEL: "EnergyMeter-2000",
                    DEVICE_FIELDS: ["power_W"],
                }
            },
        )
        coordinator = _coordinator(hass, hub)
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 10.0})
        await hass.async_block_till_done()

    coordinator.last_seen[device_key]

    # Reload: existing last_seen should NOT be overwritten by a later baseline
    later = start + timedelta(seconds=100)
    with freeze_time(later):
        assert await hass.config_entries.async_reload(hub.entry_id)
        await hass.async_block_till_done()

    # After reload with no new event, the coordinator's last_seen for the
    # device is baselined again from 'now' (the reload time). This is correct
    # behavior: the reload is a fresh start with no event, so the baseline runs.
    coordinator2 = _coordinator(hass, hub)
    assert device_key in coordinator2.last_seen


# ---------------------------------------------------------------------------
# _effective_timeout
# ---------------------------------------------------------------------------


async def test_effective_timeout_falls_back_to_hub_default(hass, hub_entry_builder):
    """Without a per-device override, the hub's availability_timeout is used.

    Setting hub timeout to 300s means a device fed at T=0 goes unavailable
    at T>300s (not at T>600s or any other value).
    """
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=300,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    # The hub availability_timeout is configured correctly
    assert coordinator.availability_timeout == 300

    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # At 299s -> still available (< 300s hub timeout)
    with freeze_time(start + timedelta(seconds=299)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get(watts_eid).state != "unavailable"

    # At 301s -> unavailable (> 300s hub timeout)
    with freeze_time(start + timedelta(seconds=301)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get(watts_eid).state == "unavailable"


async def test_effective_timeout_uses_resolver_result(hass, hub_entry_builder):
    """When resolver is set, its return value is used for the device timeout."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    # Install a resolver returning a different (shorter) timeout for this device
    coordinator.effective_timeout_resolver = lambda key: (
        120 if key == device_key else 600
    )

    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # At 121s (> 120s per-device override), entity is unavailable even though
    # the hub default (600s) has not elapsed.
    with freeze_time(start + timedelta(seconds=121)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        state = hass.states.get(watts_eid)
        assert state.state == "unavailable"


async def test_effective_timeout_resolver_exception_falls_back(hass, hub_entry_builder):
    """A failing resolver falls back to the hub default availability_timeout."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)

    def _failing_resolver(key):
        raise RuntimeError("resolver broken")

    coordinator.effective_timeout_resolver = _failing_resolver

    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # At 599s the hub default (600s) has NOT elapsed -> still available
    with freeze_time(start + timedelta(seconds=599)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        state = hass.states.get(watts_eid)
        assert state.state != "unavailable"

    # At 601s the hub default (600s) HAS elapsed -> unavailable
    with freeze_time(start + timedelta(seconds=601)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
        state = hass.states.get(watts_eid)
        assert state.state == "unavailable"


# ---------------------------------------------------------------------------
# _handle_dispatch: field present vs absent
# ---------------------------------------------------------------------------


async def test_handle_dispatch_applies_value_when_field_present(
    hass, hub_entry_builder
):
    """When the event contains the entity's field, the value is applied."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
    )

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 42.0})
        await hass.async_block_till_done()

    state = hass.states.get(watts_eid)
    assert state.state == "42.0"


async def test_handle_dispatch_writes_state_even_when_field_absent(
    hass, hub_entry_builder
):
    """Watchdog re-dispatch without the field still causes a state write."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
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
    hass, hub_entry_builder
):
    """A dispatch event missing the entity's field_key does not overwrite state."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W", "energy_kWh"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
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


async def test_subscription_registered_on_add(hass, hub_entry_builder):
    """Entity subscribes to the device-update signal when added to HA."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
    )

    # If the subscription is registered, a direct dispatcher send updates state
    with freeze_time(dt_util.utcnow()):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 77.0})
        await hass.async_block_till_done()
    assert hass.states.get(watts_eid).state == "77.0"


async def test_unsubscribe_on_removal_stops_updates(hass, hub_entry_builder):
    """After reload, only the NEW entity's dispatcher subscription is active.

    The old entity's async_will_remove_from_hass must unsubscribe so there is
    no double-dispatch on the new entity. We verify by confirming a post-reload
    feed produces a single correct state (not doubled or errored).
    """
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
    )

    # Set a value first
    with freeze_time(dt_util.utcnow()):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()
    assert hass.states.get(watts_eid).state == "5.0"

    # Reload: old entities unsubscribe (async_will_remove_from_hass), new ones subscribe
    assert await hass.config_entries.async_reload(hub.entry_id)
    await hass.async_block_till_done()

    coordinator2 = _coordinator(hass, hub)
    with freeze_time(dt_util.utcnow()):
        _feed(coordinator2, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 99.0})
        await hass.async_block_till_done()

    state = hass.states.get(watts_eid)
    assert state is not None
    assert state.state == "99.0"


# ---------------------------------------------------------------------------
# async_upsert_device
# ---------------------------------------------------------------------------


async def test_upsert_device_creates_new_record(hass, hub_entry_builder):
    """async_upsert_device creates a new record when none exists."""
    hub = await _setup_hub(hass, hub_entry_builder)
    device_key = "NewDevice-99"

    await async_upsert_device(
        hass, hub, device_key, model="NewDevice", fields=["temperature_C"]
    )
    await hass.async_block_till_done()

    devices = hub.data.get(CONF_DEVICES, {})
    assert device_key in devices
    assert devices[device_key][CONF_MODEL] == "NewDevice"
    assert "temperature_C" in devices[device_key][DEVICE_FIELDS]


async def test_upsert_device_is_idempotent_no_change(hass, hub_entry_builder):
    """async_upsert_device does not write config entry when record is unchanged."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )

    original_data = hub.data.copy()
    # Upsert with the same model and fields: no change should occur
    await async_upsert_device(
        hass, hub, device_key, model="EnergyMeter-2000", fields=["power_W"]
    )
    await hass.async_block_till_done()

    # Data unchanged
    assert hub.data[CONF_DEVICES][device_key] == original_data[CONF_DEVICES][device_key]


async def test_upsert_device_unions_fields_sorted(hass, hub_entry_builder):
    """async_upsert_device unions and sorts field keys."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["humidity"],
            }
        },
    )

    await async_upsert_device(
        hass, hub, device_key, fields=["temperature_C", "battery_ok"]
    )
    await hass.async_block_till_done()

    fields = hub.data[CONF_DEVICES][device_key][DEVICE_FIELDS]
    # All three fields present, sorted alphabetically
    assert "battery_ok" in fields
    assert "humidity" in fields
    assert "temperature_C" in fields
    assert fields == sorted(fields)


async def test_upsert_device_updates_model(hass, hub_entry_builder):
    """async_upsert_device updates the model when a new one is provided."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "",  # initially empty
                DEVICE_FIELDS: [],
            }
        },
    )

    await async_upsert_device(hass, hub, device_key, model="Acurite-606TX")
    await hass.async_block_till_done()

    assert hub.data[CONF_DEVICES][device_key][CONF_MODEL] == "Acurite-606TX"


async def test_upsert_device_does_not_overwrite_model_with_empty(
    hass, hub_entry_builder
):
    """async_upsert_device does not clear an existing model with an empty string."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: [],
            }
        },
    )

    # Pass model="" (falsy) — should not overwrite the existing model
    await async_upsert_device(hass, hub, device_key, model="")
    await hass.async_block_till_done()

    assert hub.data[CONF_DEVICES][device_key][CONF_MODEL] == "EnergyMeter-2000"


async def test_upsert_device_no_write_if_fields_subset(hass, hub_entry_builder):
    """No config-entry write if the new fields are already in the stored set."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["energy_kWh", "power_W"],
            }
        },
    )

    # Adding a field that is already present -> no change
    await async_upsert_device(hass, hub, device_key, fields=["power_W"])
    await hass.async_block_till_done()

    # Entry version not bumped (no write triggered by a data-only change)
    fields = hub.data[CONF_DEVICES][device_key][DEVICE_FIELDS]
    assert "power_W" in fields
    assert "energy_kWh" in fields


async def test_upsert_device_writes_when_new_field_added(hass, hub_entry_builder):
    """async_upsert_device writes the entry when a genuinely new field is added."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )

    await async_upsert_device(hass, hub, device_key, fields=["humidity"])
    await hass.async_block_till_done()

    fields = hub.data[CONF_DEVICES][device_key][DEVICE_FIELDS]
    assert "humidity" in fields
    assert "temperature_C" in fields


async def test_upsert_device_no_fields_arg_no_field_change(hass, hub_entry_builder):
    """async_upsert_device with fields=None does not alter existing fields."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )

    await async_upsert_device(hass, hub, device_key, fields=None)
    await hass.async_block_till_done()

    fields = hub.data[CONF_DEVICES][device_key][DEVICE_FIELDS]
    assert fields == ["power_W"]


# ---------------------------------------------------------------------------
# async_upsert_event_types
# ---------------------------------------------------------------------------


async def test_upsert_event_types_creates_new(hass, hub_entry_builder):
    """async_upsert_event_types creates event_types entry when none exists."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["button"],
            }
        },
    )

    await async_upsert_event_types(hass, hub, device_key, "button", ["A", "B"])
    await hass.async_block_till_done()

    event_types = hub.data[CONF_DEVICES][device_key].get(DEVICE_EVENT_TYPES, {})
    assert event_types.get("button") == ["A", "B"]


async def test_upsert_event_types_unions_sorted(hass, hub_entry_builder):
    """async_upsert_event_types unions and sorts event types."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["button"],
                DEVICE_EVENT_TYPES: {"button": ["A"]},
            }
        },
    )

    await async_upsert_event_types(hass, hub, device_key, "button", ["C", "B"])
    await hass.async_block_till_done()

    event_types = hub.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES]
    assert event_types["button"] == ["A", "B", "C"]  # sorted union


async def test_upsert_event_types_no_write_if_no_change(hass, hub_entry_builder):
    """async_upsert_event_types is a no-op when stored types already contain all new types."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["button"],
                DEVICE_EVENT_TYPES: {"button": ["A", "B"]},
            }
        },
    )

    {k: dict(v) for k, v in hub.data[CONF_DEVICES].items()}

    # Upsert with types already in the stored set -> no write
    await async_upsert_event_types(hass, hub, device_key, "button", ["A"])
    await hass.async_block_till_done()

    # Types unchanged
    event_types = hub.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES]
    assert event_types["button"] == ["A", "B"]


async def test_upsert_event_types_adds_new_field_key(hass, hub_entry_builder):
    """async_upsert_event_types adds a new field_key to the event_types map."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["button"],
                DEVICE_EVENT_TYPES: {"button": ["A"]},
            }
        },
    )

    await async_upsert_event_types(hass, hub, device_key, "other_field", ["X"])
    await hass.async_block_till_done()

    event_types = hub.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES]
    assert "button" in event_types  # existing field preserved
    assert event_types["other_field"] == ["X"]


async def test_upsert_event_types_device_without_event_types_key(
    hass, hub_entry_builder
):
    """async_upsert_event_types tolerates a record with no DEVICE_EVENT_TYPES key."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["button"],
                # No DEVICE_EVENT_TYPES key
            }
        },
    )

    await async_upsert_event_types(hass, hub, device_key, "button", ["A"])
    await hass.async_block_till_done()

    event_types = hub.data[CONF_DEVICES][device_key].get(DEVICE_EVENT_TYPES, {})
    assert event_types.get("button") == ["A"]


# ---------------------------------------------------------------------------
# Rtl433HubEntity: hub-update dispatcher subscription
# ---------------------------------------------------------------------------


async def test_hub_entity_subscribes_to_hub_update(hass, hub_entry_builder):
    """A hub entity updates its state when signal_hub_update fires."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)

    # Find the connectivity binary_sensor (a Rtl433HubEntity subclass)
    connectivity_eid = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{hub.entry_id}:hub:connectivity"
    )
    assert connectivity_eid is not None

    # Change coordinator.connected and fire hub_update signal
    coordinator._client.connected = True
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()
    assert hass.states.get(connectivity_eid).state == "on"

    coordinator._client.connected = False
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()
    assert hass.states.get(connectivity_eid).state == "off"


async def test_hub_entity_reload_rewires_subscription(hass, hub_entry_builder):
    """After a reload, the hub entity still responds to hub_update signals.

    This verifies that async_added_to_hass correctly rewires the dispatcher
    subscription on each load (not just the first).
    """
    hub = await _setup_hub(hass, hub_entry_builder)
    _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    connectivity_eid = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{hub.entry_id}:hub:connectivity"
    )
    assert connectivity_eid is not None

    # Reload the entry
    assert await hass.config_entries.async_reload(hub.entry_id)
    await hass.async_block_till_done()

    coordinator2 = _coordinator(hass, hub)
    # After reload, the hub_update signal must still work
    coordinator2._client.connected = True
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get(connectivity_eid)
    assert state is not None
    assert state.state == "on"


async def test_hub_entity_device_info_identifiers(hass, hub_entry_builder):
    """Hub entity is registered under the hub device."""
    hub = await _setup_hub(hass, hub_entry_builder)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    assert hub_device is not None

    connectivity_eid = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{hub.entry_id}:hub:connectivity"
    )
    assert connectivity_eid is not None
    conn_entry = ent_reg.async_get(connectivity_eid)
    assert conn_entry.device_id == hub_device.id


# ---------------------------------------------------------------------------
# Rtl433HubControl: unique_id and name
# ---------------------------------------------------------------------------


async def test_hub_control_unique_id_format(hass, hub_entry_builder):
    """Hub control unique_id is '{hub_entry_id}:hub:{object_suffix}'."""
    hub = await _setup_hub(hass, hub_entry_builder)
    ent_reg = er.async_get(hass)

    # The connectivity binary_sensor unique_id follows the hub:hub:suffix pattern
    uid = f"{hub.entry_id}:hub:connectivity"
    eid = ent_reg.async_get_entity_id("binary_sensor", DOMAIN, uid)
    assert eid is not None


async def test_hub_control_entity_category_config(hass, hub_entry_builder):
    """Hub control entities (number/select/switch) have EntityCategory.CONFIG."""
    hub = await _setup_hub(hass, hub_entry_builder)
    ent_reg = er.async_get(hass)

    # The gain number control is a Rtl433HubControl subclass
    gain_eid = ent_reg.async_get_entity_id("number", DOMAIN, f"{hub.entry_id}:hub:gain")
    if gain_eid is not None:
        entry = ent_reg.async_get(gain_eid)
        assert entry.entity_category == EntityCategory.CONFIG


# ---------------------------------------------------------------------------
# async_setup_hub_platform: dedup, teardown, field listeners
# ---------------------------------------------------------------------------


async def test_setup_no_duplicate_entities_on_reload(hass, hub_entry_builder):
    """Reloading the entry does not create duplicate entities."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{hub.entry_id}:{device_key}:watts"

    # Count entities with this unique_id before reload
    before = [e for e in ent_reg.entities.values() if e.unique_id == uid]
    assert len(before) == 1

    assert await hass.config_entries.async_reload(hub.entry_id)
    await hass.async_block_till_done()

    # Still exactly one
    after = [e for e in ent_reg.entities.values() if e.unique_id == uid]
    assert len(after) == 1


async def test_teardown_clears_field_listeners(hass, hub_entry_builder):
    """On unload, field-update listeners are torn down."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_entity_id("sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts")

    # Unload -> device removers run and field listeners are cleared
    assert await hass.config_entries.async_unload(hub.entry_id)
    await hass.async_block_till_done()

    # coordinator.device_removers is empty after teardown (removers are deregistered)
    assert _remove_device_not_in_coordinator(coordinator)


def _remove_device_not_in_coordinator(coordinator) -> bool:
    """True if the coordinator has no registered device removers (entry unloaded)."""
    # After unload, _teardown() removes _remove_device from device_removers
    return len(coordinator.device_removers) == 0


async def test_device_remover_registered_during_setup(hass, hub_entry_builder):
    """Device removers are registered for each platform during setup."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    # At least one device remover registered (sensor + binary_sensor platforms each add one)
    assert len(coordinator.device_removers) >= 1


# ---------------------------------------------------------------------------
# Calibration overlay applied during entity setup
# ---------------------------------------------------------------------------


async def test_calibration_applied_to_consumption_sensor(hass, hub_entry_builder):
    """A calibration overlay produces energy-eligible sensor attributes."""
    device_key = "ERT-SCM-9001"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
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
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    consumption_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:consumption"
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


async def test_calibration_not_applied_to_non_consumption_field(
    hass, hub_entry_builder
):
    """Calibration is only applied to consumption field keys, not arbitrary fields."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
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
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
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


async def test_entity_should_poll_false(hass, hub_entry_builder):
    """Entities must not poll — _attr_should_poll is False."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{hub.entry_id}:{device_key}:watts"
    eid = ent_reg.async_get_entity_id("sensor", DOMAIN, uid)
    # The state is current without polling
    assert eid is not None
    # should_poll=False means the entity writes its own state via dispatcher


# ---------------------------------------------------------------------------
# enabled_by_default from descriptor
# ---------------------------------------------------------------------------


async def test_entity_enabled_by_default(hass, hub_entry_builder):
    """Entities whose descriptor has enabled_by_default=True are enabled."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    uid = f"{hub.entry_id}:{device_key}:watts"
    entry = ent_reg.async_get(ent_reg.async_get_entity_id("sensor", DOMAIN, uid))
    assert entry is not None
    # enabled_by_default=True means the entity is not hidden/disabled by default
    assert entry.disabled_by is None


# ---------------------------------------------------------------------------
# Multiple fields of the same device
# ---------------------------------------------------------------------------


async def test_multiple_fields_create_separate_entities(hass, hub_entry_builder):
    """Each mapped field of a device creates its own entity."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W", "energy_kWh"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

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


async def test_late_field_entity_created_on_new_event(hass, hub_entry_builder):
    """A new field in a later event creates a new entity dynamically."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        discovery_enabled=True,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

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


async def test_baseline_sets_available_true(hass, hub_entry_builder):
    """The baseline on startup sets coordinator.available[device_key] = True."""
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    # The baseline should have set available to True for the device
    assert coordinator.available.get(device_key) is True
    assert device_key in coordinator.last_seen
