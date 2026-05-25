"""End-to-end lifecycle tests for the rtl_433 integration.

These are the heaviest, most valuable tests: a real hub + device ``ConfigEntry``
are set up through ``async_setup_entry``, events are fed through the live
coordinator, and assertions are made against the entity / device registries.
The WebSocket connect loop is stubbed out (``_connect_loop`` is a no-op) so no
socket is opened; events are injected by calling the coordinator's frame handler
directly, exactly as ``_read_frames`` would.

Covered: entity creation with correct unique_ids / classes / units, dynamic
late-field creation that persists across a reload, ``RestoreEntity`` restore,
and cascade removal of a hub (no orphan device entries / devices / entities).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.rtl_433.const import DOMAIN
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er


@pytest.fixture(autouse=True)
def _no_socket():
    """Stub the connect loop so the coordinator never opens a real WebSocket."""

    async def _noop(self) -> None:
        return None

    with patch.object(Rtl433Coordinator, "_connect_loop", _noop):
        yield


def _coordinator(hass: HomeAssistant, hub_entry: MockConfigEntry) -> Rtl433Coordinator:
    """Return the live coordinator created for a loaded hub entry."""
    return hass.data[DOMAIN][hub_entry.entry_id]


def _feed(coordinator: Rtl433Coordinator, event: dict) -> None:
    """Inject a single event as the coordinator's text-frame path would."""
    coordinator._handle_text_frame(json.dumps(event))


async def _setup_hub_and_device(
    hass, hub_entry_builder, device_entry_builder, *, model, device_key, options=None
):
    """Set up a hub + child device entry and return (hub, device, coordinator)."""
    hub = hub_entry_builder(availability_timeout=600)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    coordinator = _coordinator(hass, hub)

    device = device_entry_builder(
        hub_entry_id=hub.entry_id,
        device_key=device_key,
        model=model,
        options=options,
    )
    device.add_to_hass(hass)
    assert await hass.config_entries.async_setup(device.entry_id)
    await hass.async_block_till_done()

    return hub, device, coordinator


async def test_entities_created_with_correct_metadata(
    hass, hub_entry_builder, device_entry_builder, events
):
    """A power sensor's fields become entities with the right unique_ids/units."""
    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub, device, coordinator = await _setup_hub_and_device(
        hass,
        hub_entry_builder,
        device_entry_builder,
        model="EnergyMeter-2000",
        device_key=device_key,
    )

    # Feed the event, then let the dynamic-add listeners build the entities.
    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

    power = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:watts")
    assert power is not None
    state = hass.states.get(power)
    assert state is not None
    assert state.state == "1450.5"
    assert state.attributes["device_class"] == "power"
    assert state.attributes["unit_of_measurement"] == "W"
    assert state.attributes["state_class"] == "measurement"

    # Energy uses total_increasing.
    energy = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:kwh")
    assert energy is not None
    assert hass.states.get(energy).attributes["state_class"] == "total_increasing"

    # The device is registered and linked under the hub via_device.
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, prefix)})
    assert device_entry is not None
    assert device_entry.via_device_id is not None


async def test_binary_sensors_created(
    hass, hub_entry_builder, device_entry_builder, events
):
    """A contact device yields an opening binary sensor with inverted payload."""
    contact_event = events("contact_leak.json")[0]  # GenericDoor-X1, closed: 0
    device_key = "GenericDoor-X1-88"

    hub, device, coordinator = await _setup_hub_and_device(
        hass,
        hub_entry_builder,
        device_entry_builder,
        model="GenericDoor-X1",
        device_key=device_key,
    )

    _feed(coordinator, contact_event)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

    opening = ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{prefix}:opening")
    assert opening is not None
    state = hass.states.get(opening)
    # closed == 0 inverts to "on" (open).
    assert state.state == "on"
    assert state.attributes["device_class"] == "opening"


async def test_dynamic_late_field_creates_entity_and_persists_across_reload(
    hass, hub_entry_builder, device_entry_builder, events
):
    """A field that appears only in a later event creates an entity that survives reload."""
    first, second = events("acurite_temp_humidity.json")  # battery_ok only in #2
    device_key = "Acurite-606TX-42"

    hub, device, coordinator = await _setup_hub_and_device(
        hass,
        hub_entry_builder,
        device_entry_builder,
        model="Acurite-606TX",
        device_key=device_key,
    )

    ent_reg = er.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

    # First event: temperature + humidity, but no battery.
    _feed(coordinator, first)
    await hass.async_block_till_done()
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:T") is not None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:H") is not None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:B") is None

    # Later event introduces battery_ok -> a new Battery entity appears.
    _feed(coordinator, second)
    await hass.async_block_till_done()
    battery = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:B")
    assert battery is not None
    # battery_ok=1 maps to the 100% battery sensor.
    assert hass.states.get(battery).state == "100"

    # The observed field set was persisted to the device entry options.
    assert "battery_ok" in device.options.get("observed_fields", [])

    # Reload the device entry: the battery entity must be recreated from the
    # persisted observed-field set, even before any new event arrives.
    assert await hass.config_entries.async_reload(device.entry_id)
    await hass.async_block_till_done()
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:B") is not None


async def test_restore_entity_restores_last_state(
    hass, hub_entry_builder, device_entry_builder
):
    """A restored sensor shows its previous value before any live event arrives."""
    device_key = "Acurite-606TX-42"
    prefix_dev = device_key
    # Pre-seed a restore-state cache for the temperature sensor's entity_id.
    # The unique_id includes the hub entry id, so we let setup create the
    # registry entry first, then assert via the value seeded into the state.
    hub = hub_entry_builder(availability_timeout=600)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    prefix = f"{hub.entry_id}:{prefix_dev}"
    # The temperature entity_id HA will assign for this device + field.
    restore_entity_id = "sensor.acurite_606tx_acurite_606tx_42_temperature"

    mock_restore_cache(
        hass,
        (State(restore_entity_id, "19.9"),),
    )

    device = device_entry_builder(
        hub_entry_id=hub.entry_id,
        device_key=device_key,
        model="Acurite-606TX",
        # Persisted observed field so the entity is recreated on setup with no
        # live event needed.
        options={"observed_fields": ["temperature_C"]},
    )
    device.add_to_hass(hass)
    assert await hass.config_entries.async_setup(device.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    temp = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:T")
    assert temp is not None
    # No live event was fed, so the value is the restored one.
    assert hass.states.get(temp).state == "19.9"


async def test_cascade_removal_leaves_no_orphans(
    hass, hub_entry_builder, device_entry_builder, events
):
    """Removing a hub removes its child device entries, devices, and entities."""
    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub, device, coordinator = await _setup_hub_and_device(
        hass,
        hub_entry_builder,
        device_entry_builder,
        model="EnergyMeter-2000",
        device_key=device_key,
    )
    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

    # Sanity: entity + device exist before removal.
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:watts") is not None
    assert dev_reg.async_get_device(identifiers={(DOMAIN, prefix)}) is not None
    assert hass.config_entries.async_get_entry(device.entry_id) is not None

    # Remove the hub -> async_remove_entry cascades to the child device entry.
    assert await hass.config_entries.async_remove(hub.entry_id)
    await hass.async_block_till_done()

    # The child device config entry is gone (no orphan entry).
    assert hass.config_entries.async_get_entry(device.entry_id) is None
    # The device and its entities are gone (no orphan device / entity).
    assert dev_reg.async_get_device(identifiers={(DOMAIN, prefix)}) is None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:watts") is None
