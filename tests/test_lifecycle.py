"""End-to-end lifecycle tests for the rtl_433 integration (single-hub model).

These are the heaviest, most valuable tests: a single hub ``ConfigEntry`` is set
up through ``async_setup_entry``, events are fed through the live coordinator,
and assertions are made against the entity / device registries. The WebSocket
connect loop is stubbed out (``_connect_loop`` is a no-op) so no socket is
opened; events are injected by calling the coordinator's frame handler directly,
exactly as ``_read_frames`` would.

Covered:
* nested-device + entity creation from a seeded devices map, with the right
  unique_ids / device_class / unit / state_class and ``via_device`` linkage;
* dynamic add of a brand-new device with discovery ON (and NOT added with
  discovery OFF);
* a dynamic late field that creates a new entity and persists across a reload;
* ``RestoreEntity`` restore of a previous value before any live event;
* remove a nested device via ``async_remove_config_entry_device`` -> entities
  gone + ``device_key`` gone from the map + coordinator runtime state evicted ->
  with discovery ON, feed the device again -> it re-appears (Clarification #4);
* the 0.1.0 -> nested migration: a v1 hub + two v1 device entries with
  pre-seeded registry devices/entities fold into the single hub with unchanged
  unique_ids / entity_ids and a populated devices map.
"""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from unittest.mock import patch

from freezegun import freeze_time
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_WATER,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DEVICES,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_HUB_ENTRY_ID,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    DEVICE_CALIBRATION,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_HUB,
    signal_hub_update,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

LEGACY_OBSERVED_FIELDS = "observed_fields"


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


async def _setup_hub(hass, hub_entry_builder, *, devices=None, **kwargs):
    """Set up a single hub entry (optionally pre-seeded) and return it."""
    hub = hub_entry_builder(availability_timeout=600, devices=devices, **kwargs)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    return hub


def _ts(value):
    """Drop sub-second precision for timestamp comparisons.

    A ``device_class=timestamp`` sensor renders its state at whole-second ISO
    precision, while ``coordinator.last_seen`` keeps the microseconds from
    ``dt_util.utcnow()``. Comparing both sides truncated to the second keeps the
    assertions about *which* timestamp is shown without depending on HA's
    sub-second rendering.
    """
    return value.replace(microsecond=0)


# --------------------------------------------------------------------------- #
# Seeded devices map -> entities recreated on the hub entry.                   #
# --------------------------------------------------------------------------- #
async def test_seeded_device_creates_entities_with_metadata(hass, hub_entry_builder):
    """Seeding the devices map recreates entities with the right metadata."""
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

    ent_reg = er.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

    # Entities exist for the seeded fields with the correct unique_ids, even
    # before any live event arrives.
    power = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:watts")
    energy = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:kwh")
    assert power is not None
    assert energy is not None

    # Now feed a live event and assert classes / units / state_class.
    _feed(
        _coordinator(hass, hub),
        {
            "model": "EnergyMeter-2000",
            "id": 1234,
            "power_W": 1450.5,
            "energy_kWh": 88.21,
        },
    )
    await hass.async_block_till_done()

    state = hass.states.get(power)
    assert state.state == "1450.5"
    assert state.attributes["device_class"] == "power"
    assert state.attributes["unit_of_measurement"] == "W"
    assert state.attributes["state_class"] == "measurement"
    assert hass.states.get(energy).attributes["state_class"] == "total_increasing"

    # The nested device is registered under the hub via via_device.
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, prefix)})
    assert device_entry is not None
    assert device_entry.via_device_id is not None
    # The via_device is the hub device.
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    assert device_entry.via_device_id == hub_device.id


async def test_seeded_binary_sensor_created(hass, hub_entry_builder):
    """A contact device's seeded field yields an inverted opening binary sensor."""
    device_key = "GenericDoor-X1-88"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={device_key: {CONF_MODEL: "GenericDoor-X1", DEVICE_FIELDS: ["closed"]}},
    )

    ent_reg = er.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"
    opening = ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{prefix}:opening")
    assert opening is not None

    _feed(
        _coordinator(hass, hub),
        {"model": "GenericDoor-X1", "id": 88, "closed": 0},
    )
    await hass.async_block_till_done()

    state = hass.states.get(opening)
    # closed == 0 inverts to "on" (open).
    assert state.state == "on"
    assert state.attributes["device_class"] == "opening"


# --------------------------------------------------------------------------- #
# Hub connectivity binary_sensor follows coordinator.connected.                #
# --------------------------------------------------------------------------- #
async def test_hub_connectivity_sensor(hass, hub_entry_builder):
    """The hub connectivity binary_sensor tracks ``coordinator.connected``."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)

    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{hub.entry_id}:hub:connectivity"
    )
    assert entity_id is not None

    # Mark connected and notify -> state on.
    coordinator.connected = True
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"

    # A shutdown frame flips it back off (Task 1 path).
    _feed(coordinator, {"shutdown": "goodbye"})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"

    # The entity belongs to the hub device.
    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    assert ent_reg.async_get(entity_id).device_id == hub_device.id


# --------------------------------------------------------------------------- #
# Hub meta/SDR + server-stats diagnostic sensors render coordinator state.     #
# --------------------------------------------------------------------------- #
# The five SDR sensors whose concept is folded into a Plan 6 control while
# management is on, so their diagnostic sensor is suppressed (each concept keeps
# exactly one entity). Center frequency is intentionally NOT folded, and the
# server-stats sensors are never folded.
_FOLDED_SDR_SUFFIXES = (
    "sample_rate",
    "ppm_error",
    "gain",
    "conversion_mode",
    "hop_interval",
)


async def test_hub_diagnostic_sensors_managed(hass, hub_entry_builder):
    """In managed mode (default) the folded SDR sensors are suppressed.

    Only the center-frequency SDR sensor + the four server-stats sensors remain
    on the hub device; the five folded concepts (sample rate, ppm, gain,
    conversion mode, hop interval) are now number/select/switch controls, so
    their diagnostic sensors must be absent.
    """
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)
    assert coordinator.manage_settings is True
    coordinator.meta = {
        "center_frequency": 433920000,
        "samp_rate": 250000,
        "conversion_mode": 1,
        "hop_interval": 600,
        "frequencies": [433920000],
        "hop_times": [600],
        "gain": "",  # empty string -> rendered as "auto"
        "ppm_error": 0,
    }
    coordinator.stats = {
        "enabled": 5,
        "since": "2026-05-26T10:00:00",
        "frames": {"count": 12, "fsk": 3, "events": 40},
        "stats": [{"name": "Acurite", "events": 40}],
    }
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    def sensor_id(suffix):
        return ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{hub.entry_id}:hub:{suffix}"
        )

    # --- The five folded SDR sensors are suppressed in managed mode ------- #
    for suffix in _FOLDED_SDR_SUFFIXES:
        assert sensor_id(suffix) is None, suffix

    # --- The center-frequency SDR sensor still renders -------------------- #
    cf = hass.states.get(sensor_id("center_frequency"))
    assert cf.state == "433920000"
    assert cf.attributes["device_class"] == "frequency"
    assert cf.attributes["unit_of_measurement"] == "Hz"
    assert cf.attributes["frequencies"] == [433920000]
    assert cf.attributes["hop_times"] == [600]

    # --- Server-stats sensors are never folded ---------------------------- #
    events_state = hass.states.get(sensor_id("decoded_events"))
    assert events_state.state == "40"
    assert events_state.attributes["state_class"] == "total_increasing"
    # Frame counters are cumulative -> TOTAL_INCREASING (records statistics);
    # enabled decoders is a gauge -> MEASUREMENT.
    ook_state = hass.states.get(sensor_id("ook_frames"))
    assert ook_state.state == "12"
    assert ook_state.attributes["state_class"] == "total_increasing"
    fsk_state = hass.states.get(sensor_id("fsk_frames"))
    assert fsk_state.state == "3"
    assert fsk_state.attributes["state_class"] == "total_increasing"
    enabled_state = hass.states.get(sensor_id("enabled_decoders"))
    assert enabled_state.state == "5"
    assert enabled_state.attributes["state_class"] == "measurement"
    assert events_state.attributes["stats"] == [{"name": "Acurite", "events": 40}]
    assert events_state.attributes["since"] == "2026-05-26T10:00:00"

    # The surviving hub sensors are diagnostic and live on the hub device.
    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    cf_entry = ent_reg.async_get(sensor_id("center_frequency"))
    assert cf_entry.device_id == hub_device.id
    assert cf_entry.entity_category == "diagnostic"


async def test_hub_diagnostic_sensors_unmanaged(hass, hub_entry_builder):
    """With management off, all six SDR sensors render and no controls exist."""
    hub = await _setup_hub(
        hass, hub_entry_builder, options={CONF_MANAGE_SETTINGS: False}
    )
    coordinator = _coordinator(hass, hub)
    assert coordinator.manage_settings is False
    coordinator.meta = {
        "center_frequency": 433920000,
        "samp_rate": 250000,
        "conversion_mode": 1,
        "hop_interval": 600,
        "frequencies": [433920000],
        "hop_times": [600],
        "gain": "",  # empty string -> rendered as "auto"
        "ppm_error": 0,
    }
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    def state(suffix):
        eid = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{hub.entry_id}:hub:{suffix}"
        )
        assert eid is not None, suffix
        return hass.states.get(eid)

    # All six SDR sensors are present (nothing folded away).
    assert state("center_frequency").state == "433920000"
    assert state("sample_rate").state == "250000"
    assert state("conversion_mode").state == "1"
    assert state("hop_interval").state == "600"
    assert state("gain").state == "auto"  # empty string -> auto
    assert state("ppm_error").state == "0"

    # No SDR control entities exist when management is off.
    for platform in ("number", "select", "switch"):
        for suffix in _FOLDED_SDR_SUFFIXES + ("center_frequency", "gain_auto"):
            assert (
                ent_reg.async_get_entity_id(
                    platform, DOMAIN, f"{hub.entry_id}:hub:{suffix}"
                )
                is None
            )


# --------------------------------------------------------------------------- #
# Dynamic add of a brand-new device, gated by the discovery toggle.            #
# --------------------------------------------------------------------------- #
async def test_new_device_added_when_discovery_on(hass, hub_entry_builder, events):
    """Feeding an unseen device with discovery on creates a nested device."""
    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)

    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:watts") is not None
    assert dev_reg.async_get_device(identifiers={(DOMAIN, prefix)}) is not None
    # The new device was folded into the hub's devices map.
    assert device_key in hub.data.get(CONF_DEVICES, {})
    assert "power_W" in hub.data[CONF_DEVICES][device_key][DEVICE_FIELDS]


async def test_new_device_not_added_when_discovery_off(hass, hub_entry_builder, events):
    """Feeding an unseen device with discovery off adds no device/entities."""
    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=False)
    coordinator = _coordinator(hass, hub)

    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:watts") is None
    assert dev_reg.async_get_device(identifiers={(DOMAIN, prefix)}) is None
    assert device_key not in hub.data.get(CONF_DEVICES, {})


# --------------------------------------------------------------------------- #
# Dynamic late field creates an entity that persists across a reload.          #
# --------------------------------------------------------------------------- #
async def test_late_field_creates_entity_and_persists_across_reload(
    hass, hub_entry_builder, events
):
    """A field that appears only in a later event creates a surviving entity."""
    first, second = events("acurite_temp_humidity.json")  # battery_ok only in #2
    device_key = "Acurite-606TX-42"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)

    ent_reg = er.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

    # First event: temperature + humidity (new device), but no battery.
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

    # The observed field set was persisted to the hub devices map.
    assert "battery_ok" in hub.data[CONF_DEVICES][device_key][DEVICE_FIELDS]

    # Reload the hub: the battery entity must be recreated from the persisted
    # devices map, even before any new event arrives.
    assert await hass.config_entries.async_reload(hub.entry_id)
    await hass.async_block_till_done()
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:B") is not None


# --------------------------------------------------------------------------- #
# RestoreEntity restore.                                                       #
# --------------------------------------------------------------------------- #
async def test_restore_entity_restores_last_state(hass, hub_entry_builder):
    """A restored sensor shows its previous value before any live event arrives."""
    device_key = "Acurite-606TX-42"
    restore_entity_id = "sensor.acurite_606tx_acurite_606tx_42_temperature"

    mock_restore_cache(hass, (State(restore_entity_id, "19.9"),))

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
    prefix = f"{hub.entry_id}:{device_key}"
    temp = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:T")
    assert temp is not None
    # No live event was fed, so the value is the restored one.
    assert hass.states.get(temp).state == "19.9"


# --------------------------------------------------------------------------- #
# Remove a nested device -> evicted -> re-appears with discovery on.           #
# --------------------------------------------------------------------------- #
async def test_remove_device_then_re_add_with_discovery_on(
    hass, hub_entry_builder, events
):
    """Removing a nested device clears it; with discovery on it re-appears."""
    from custom_components.rtl_433 import async_remove_config_entry_device

    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)

    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"

    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, prefix)})
    assert device_entry is not None
    assert device_key in coordinator.devices

    # async_remove_config_entry_device refuses the hub device.
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    assert await async_remove_config_entry_device(hass, hub, hub_device) is False

    # Removing the nested device returns True; map + coordinator state cleared.
    assert await async_remove_config_entry_device(hass, hub, device_entry) is True
    assert device_key not in hub.data.get(CONF_DEVICES, {})
    assert device_key not in coordinator.devices
    assert device_key not in coordinator.last_seen
    assert device_key not in coordinator.device_fields

    # Let HA actually drop the registry device + entities (as the device page
    # delete would), then confirm they are gone.
    dev_reg.async_remove_device(device_entry.id)
    await hass.async_block_till_done()
    assert dev_reg.async_get_device(identifiers={(DOMAIN, prefix)}) is None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:watts") is None

    # With discovery still on, the device transmits again -> it re-appears
    # WITHOUT a reload (Clarification #4). This exercises the full eviction path:
    # async_remove_config_entry_device evicts the coordinator runtime state AND
    # calls the entity platforms' per-device removers, which drop the stale
    # ``created`` dedup entries and tear down the per-device field listener, so
    # the next event recreates the device and its entities cleanly.
    _feed(coordinator, power_event)
    await hass.async_block_till_done()
    assert dev_reg.async_get_device(identifiers={(DOMAIN, prefix)}) is not None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:watts") is not None
    assert device_key in hub.data.get(CONF_DEVICES, {})


# --------------------------------------------------------------------------- #
# Entity setup uses the cached merged registry (no event-loop YAML load).      #
# --------------------------------------------------------------------------- #
async def test_entity_setup_uses_cached_registry_not_event_loop_load(
    hass, hub_entry_builder
):
    """Entity setup must resolve descriptors via the hub's cached merged registry.

    Regression: the platform previously called ``lookup(field_key)`` with no
    registry, which triggered the lazy module-level ``load_library`` and opened
    YAML files on the event loop (Home Assistant flags this as a blocking call).
    The descriptor lookups during setup and dynamic add must use the registry
    cached on ``hass.data[DOMAIN][DATA_LIBRARY]`` instead.
    """
    from custom_components.rtl_433 import entity as entity_mod
    from custom_components.rtl_433.const import DATA_LIBRARY

    real_lookup = entity_mod.lookup
    seen_registries: list = []

    def _recording_lookup(field_key, model=None, registry=None):
        seen_registries.append(registry)
        return real_lookup(field_key, model, registry)

    with patch.object(entity_mod, "lookup", _recording_lookup):
        hub = await _setup_hub(
            hass,
            hub_entry_builder,
            devices={
                "Acurite-606TX-42": {
                    CONF_MODEL: "Acurite-606TX",
                    DEVICE_FIELDS: ["temperature_C"],
                }
            },
        )
        # A live event drives dynamic field creation, exercising the lookup path.
        _feed(
            _coordinator(hass, hub),
            {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.0},
        )
        await hass.async_block_till_done()

    cached_registry = hass.data[DOMAIN][DATA_LIBRARY][0]
    assert cached_registry is not None
    assert seen_registries, "expected descriptor lookups during entity setup"
    # Every lookup used the cached registry object — never None (the lazy,
    # event-loop file-loading path).
    assert all(reg is cached_registry for reg in seen_registries)


# --------------------------------------------------------------------------- #
# Phantom "unknown" device cleanup on hub setup.                               #
# --------------------------------------------------------------------------- #
async def test_phantom_unknown_device_cleaned_up(hass, hub_entry_builder):
    """A pre-fix phantom ``unknown`` device is dropped from the map + registry."""
    real_key = "Acurite-606TX-42"
    hub = hub_entry_builder(
        availability_timeout=600,
        devices={
            "unknown": {CONF_MODEL: "", DEVICE_FIELDS: ["frequencies"]},
            real_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]},
        },
    )
    hub.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    # Pre-seed a stale phantom registry device as a prior version would have.
    dev_reg.async_get_or_create(
        config_entry_id=hub.entry_id,
        identifiers={(DOMAIN, f"{hub.entry_id}:unknown")},
    )

    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    # The phantom record + registry device are gone; the real device remains.
    assert "unknown" not in hub.data.get(CONF_DEVICES, {})
    assert real_key in hub.data[CONF_DEVICES]
    assert (
        dev_reg.async_get_device(identifiers={(DOMAIN, f"{hub.entry_id}:unknown")})
        is None
    )
    # The hub device itself is untouched.
    assert dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)}) is not None

    # Re-running setup is a no-op (reload) — nothing left to clean.
    assert await hass.config_entries.async_reload(hub.entry_id)
    await hass.async_block_till_done()
    assert "unknown" not in hub.data.get(CONF_DEVICES, {})
    assert real_key in hub.data[CONF_DEVICES]
    assert (
        dev_reg.async_get_device(identifiers={(DOMAIN, f"{hub.entry_id}:unknown")})
        is None
    )


# --------------------------------------------------------------------------- #
# 0.1.0 -> nested migration.                                                   #
# --------------------------------------------------------------------------- #
async def test_migration_folds_legacy_device_entries_into_hub(hass):
    """A v1 hub + two v1 device entries migrate to one hub with devices map."""
    hub_entry_id = "huboldid01"
    key_a = "Acurite-606TX-42"
    key_b = "EnergyMeter-2000-1234"

    # Legacy v1 hub entry.
    hub = MockConfigEntry(
        domain=DOMAIN,
        title="rtl_433 (rtl433.local)",
        version=1,
        unique_id="hub:rtl433.local:8433",
        entry_id=hub_entry_id,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
            CONF_HOST: "rtl433.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
        },
    )
    # Legacy v1 device entries, each recording its parent hub.
    device_a = MockConfigEntry(
        domain=DOMAIN,
        title="Acurite-606TX",
        version=1,
        unique_id=f"{hub_entry_id}:{key_a}",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_HUB_ENTRY_ID: hub_entry_id,
            CONF_DEVICE_KEY: key_a,
            CONF_MODEL: "Acurite-606TX",
        },
        options={LEGACY_OBSERVED_FIELDS: ["temperature_C", "humidity"]},
    )
    device_b = MockConfigEntry(
        domain=DOMAIN,
        title="EnergyMeter-2000",
        version=1,
        unique_id=f"{hub_entry_id}:{key_b}",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_HUB_ENTRY_ID: hub_entry_id,
            CONF_DEVICE_KEY: key_b,
            CONF_MODEL: "EnergyMeter-2000",
        },
        # This device carried a per-device timeout override in 0.1.0.
        options={
            LEGACY_OBSERVED_FIELDS: ["power_W"],
            CONF_AVAILABILITY_TIMEOUT: 120,
        },
    )
    for entry in (hub, device_a, device_b):
        entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    # Pre-seed the registry devices owned by the legacy child entries, plus a
    # couple of entities each — keyed exactly as 0.1.0 created them.
    device_a_dev = dev_reg.async_get_or_create(
        config_entry_id=device_a.entry_id,
        identifiers={(DOMAIN, f"{hub_entry_id}:{key_a}")},
    )
    device_b_dev = dev_reg.async_get_or_create(
        config_entry_id=device_b.entry_id,
        identifiers={(DOMAIN, f"{hub_entry_id}:{key_b}")},
    )
    temp_entry = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{hub_entry_id}:{key_a}:T",
        config_entry=device_a,
        device_id=device_a_dev.id,
    )
    hum_entry = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{hub_entry_id}:{key_a}:H",
        config_entry=device_a,
        device_id=device_a_dev.id,
    )
    watts_entry = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{hub_entry_id}:{key_b}:watts",
        config_entry=device_b,
        device_id=device_b_dev.id,
    )
    # Capture the pre-migration entity_ids/unique_ids to assert they are stable.
    pre = {e.unique_id: e.entity_id for e in (temp_entry, hum_entry, watts_entry)}

    # Run setup -> async_migrate_entry executes for the hub (and children).
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    # Only the hub config entry remains.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].entry_id == hub.entry_id

    # Both devices are now associated with the hub config entry.
    for key in (key_a, key_b):
        device = dev_reg.async_get_device(
            identifiers={(DOMAIN, f"{hub_entry_id}:{key}")}
        )
        assert device is not None
        assert hub.entry_id in device.config_entries
        assert device_a.entry_id not in device.config_entries
        assert device_b.entry_id not in device.config_entries

    # The seeded entities still exist, unchanged, and now owned by the hub.
    for unique_id, entity_id in pre.items():
        new_entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
        assert new_entity_id == entity_id  # entity_id preserved
        entity = ent_reg.async_get(new_entity_id)
        assert entity.config_entry_id == hub.entry_id

    # The hub's devices map carries both keys with folded fields + override.
    devices = hub.data[CONF_DEVICES]
    assert set(devices) == {key_a, key_b}
    assert devices[key_a][DEVICE_FIELDS] == ["humidity", "temperature_C"]
    assert devices[key_a][CONF_MODEL] == "Acurite-606TX"
    assert devices[key_b][DEVICE_FIELDS] == ["power_W"]
    assert devices[key_b][DEVICE_TIMEOUT_OVERRIDE] == 120


# --------------------------------------------------------------------------- #
# Per-device synthetic "Last seen" sensor.                                     #
# --------------------------------------------------------------------------- #
async def test_last_seen_created_for_every_device(hass, hub_entry_builder):
    """Every device gets exactly one enabled diagnostic timestamp Last-seen.

    Covers both the seeded-map setup path (including a device with no mapped
    fields) and the live-event new-device path with discovery on.
    """
    mapped_key = "EnergyMeter-2000-1234"
    bare_key = "MysteryThing-7"  # no library-mapped fields
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        discovery_enabled=True,
        devices={
            mapped_key: {CONF_MODEL: "EnergyMeter-2000", DEVICE_FIELDS: ["power_W"]},
            bare_key: {CONF_MODEL: "MysteryThing", DEVICE_FIELDS: []},
        },
    )

    ent_reg = er.async_get(hass)

    def last_seen_ids(key: str) -> list[str]:
        """All sensor-platform entity entries whose unique_id ends in :last_seen."""
        suffix = f"{hub.entry_id}:{key}:last_seen"
        return [
            e.entity_id
            for e in ent_reg.entities.values()
            if e.platform == DOMAIN and e.domain == "sensor" and e.unique_id == suffix
        ]

    # Exactly one Last-seen sensor per seeded device — even the no-field one.
    for key in (mapped_key, bare_key):
        ids = last_seen_ids(key)
        assert len(ids) == 1, (key, ids)
        entry = ent_reg.async_get(ids[0])
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert entry.disabled_by is None  # enabled by default

    # The state exposes the timestamp device_class once added (seeded with a
    # baseline "now" by the base entity even before any live event).
    mapped_eid = last_seen_ids(mapped_key)[0]
    assert hass.states.get(mapped_eid).attributes["device_class"] == "timestamp"

    # A brand-new device fed via a live event (discovery on) also gets exactly
    # one Last-seen sensor.
    new_key = "Acurite-606TX-42"
    _feed(
        _coordinator(hass, hub),
        {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4},
    )
    await hass.async_block_till_done()
    assert len(last_seen_ids(new_key)) == 1


async def test_last_seen_updates_and_stays_available(hass, hub_entry_builder):
    """A live event sets Last-seen, and it survives the silence-timeout watchdog.

    After the device falls silent past the 600s timeout, its measurement sensor
    reads ``unavailable`` while the Last-seen sensor stays available with the
    unchanged prior timestamp (the always-available override).
    """
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {CONF_MODEL: "EnergyMeter-2000", DEVICE_FIELDS: ["power_W"]}
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    last_seen_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:last_seen"
    )
    watts_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts"
    )
    assert last_seen_eid is not None
    assert watts_eid is not None

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()

    # Value equals the coordinator's last_seen (compare parsed tz-aware datetimes).
    state = hass.states.get(last_seen_eid)
    assert _ts(dt_util.parse_datetime(state.state)) == _ts(
        coordinator.last_seen[device_key]
    )
    assert state.attributes["device_class"] == "timestamp"
    seen_at = coordinator.last_seen[device_key]

    # Advance past the 600s timeout and run the watchdog.
    with freeze_time(start + timedelta(seconds=601)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()

    # The measurement sensor flips unavailable; Last-seen stays available with
    # the unchanged timestamp.
    assert hass.states.get(watts_eid).state == "unavailable"
    last_seen_state = hass.states.get(last_seen_eid)
    assert last_seen_state.state != "unavailable"
    assert _ts(dt_util.parse_datetime(last_seen_state.state)) == _ts(seen_at)
    assert coordinator.last_seen[device_key] == seen_at


async def test_last_seen_restores_prior_not_baseline(hass, hub_entry_builder):
    """A restored Last-seen shows the prior timestamp, not "now"/the baseline.

    Then a real event overwrites it with the fresh ``coordinator.last_seen``.
    """
    device_key = "Acurite-606TX-42"
    restore_eid = "sensor.acurite_606tx_acurite_606tx_42_last_seen"
    prior = "2026-05-20T08:30:00+00:00"
    mock_restore_cache(hass, (State(restore_eid, prior),))

    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:last_seen"
    )
    assert eid is not None
    # The hardcoded restore entity_id must match the registry-assigned one, or
    # the restore cache would not be picked up.
    assert eid == restore_eid

    # No live event yet: the sensor shows the restored prior time, NOT a fresh
    # baseline "now". Comparing parsed datetimes guards against ISO formatting.
    restored_state = hass.states.get(eid)
    assert dt_util.parse_datetime(restored_state.state) == dt_util.parse_datetime(prior)

    # A real event updates it to the fresh coordinator.last_seen value.
    _feed(coordinator, {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4})
    await hass.async_block_till_done()
    updated_state = hass.states.get(eid)
    assert _ts(dt_util.parse_datetime(updated_state.state)) == _ts(
        coordinator.last_seen[device_key]
    )
    assert dt_util.parse_datetime(updated_state.state) != dt_util.parse_datetime(prior)


async def test_no_last_seen_on_binary_sensor(hass, hub_entry_builder):
    """No Last-seen on the binary_sensor platform; the sensor one still exists."""
    device_key = "GenericDoor-X1-88"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={device_key: {CONF_MODEL: "GenericDoor-X1", DEVICE_FIELDS: ["closed"]}},
    )
    ent_reg = er.async_get(hass)
    assert (
        ent_reg.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{hub.entry_id}:{device_key}:last_seen"
        )
        is None
    )
    # But the sensor-platform Last-seen still exists for the device.
    assert (
        ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:last_seen"
        )
        is not None
    )


# --------------------------------------------------------------------------- #
# Event platform: value-as-type firing, auto-populate, persistence, restore.   #
# --------------------------------------------------------------------------- #
async def test_event_fires_value_as_type_and_auto_populates(hass, hub_entry_builder):
    """A multi-value event field fires each value as its type and grows the set.

    Delivering ``A``, ``B``, ``A`` fires those event types in order, auto-populates
    ``event_types`` to include both, persists the sorted set to the devices map,
    and exposes no attributes on the fired event beyond the standard event_type
    (no custom payload).
    """
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["button"]}},
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    button_eid = ent_reg.async_get_entity_id(
        "event", DOMAIN, f"{hub.entry_id}:{device_key}:button"
    )
    assert button_eid is not None

    # No event yet -> the event entity reads "unknown" with an empty type set.
    initial = hass.states.get(button_eid)
    assert initial.state == "unknown"
    assert initial.attributes["event_types"] == []

    fired: list[str] = []
    for code in ("A", "B", "A"):
        _feed(coordinator, {"model": "Acurite-606TX", "id": 42, "button": code})
        await hass.async_block_till_done()
        fired.append(hass.states.get(button_eid).attributes["event_type"])

    # The fired event_type tracked each value in order (including the repeat).
    assert fired == ["A", "B", "A"]

    state = hass.states.get(button_eid)
    # event_types grew to include both observed values.
    assert state.attributes["event_types"] == ["A", "B"]
    # The fired event exposes only the standard event_type beyond the type set;
    # no custom payload attributes were attached.
    standard = {"event_types", "event_type", "device_class", "friendly_name"}
    assert set(state.attributes) <= standard
    assert state.attributes["device_class"] == "button"

    # Observed types persisted to the devices map (sorted union).
    entry = hass.config_entries.async_get_entry(hub.entry_id)
    persisted = entry.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES]
    assert persisted["button"] == ["A", "B"]


async def test_event_single_value_momentary_fires_each_transmission(
    hass, hub_entry_builder
):
    """A single-value momentary event fires its one type on every transmission."""
    device_key = "Honeywell-Doorbell-7"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Honeywell-Doorbell",
                DEVICE_FIELDS: ["secret_knock"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    doorbell_eid = ent_reg.async_get_entity_id(
        "event", DOMAIN, f"{hub.entry_id}:{device_key}:secret_knock"
    )
    assert doorbell_eid is not None

    # Two genuine transmissions of the same value each fire (distinct objects).
    # Freeze the clock at two distinct instants so the fire timestamp advances
    # on the second transmission (a same-value repeat is a fresh ``normalize()``
    # object, so the identity dedupe does NOT suppress it — a doorbell pressed
    # twice fires twice).
    start = dt_util.utcnow()
    timestamps: list[str] = []
    for offset in (0, 5):
        with freeze_time(start + timedelta(seconds=offset)):
            _feed(
                coordinator,
                {"model": "Honeywell-Doorbell", "id": 7, "secret_knock": 1},
            )
            await hass.async_block_till_done()
        state = hass.states.get(doorbell_eid)
        assert state.attributes["event_type"] == "1"
        timestamps.append(state.state)

    # The second transmission fired again (a later, distinct timestamp).
    assert timestamps[1] != timestamps[0]
    assert dt_util.parse_datetime(timestamps[1]) > dt_util.parse_datetime(timestamps[0])
    state = hass.states.get(doorbell_eid)
    assert state.attributes["event_types"] == ["1"]
    assert state.attributes["device_class"] == "doorbell"


async def test_event_rebuilds_event_types_from_persisted(hass, hub_entry_builder):
    """A pre-seeded device exposes its persisted event_types before any event.

    Seeding ``DEVICE_EVENT_TYPES`` in the device record makes the rebuilt entity
    advertise those valid types immediately (HA validates ``_trigger_event``
    against the list), even though no event has fired yet.
    """
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
    ent_reg = er.async_get(hass)
    button_eid = ent_reg.async_get_entity_id(
        "event", DOMAIN, f"{hub.entry_id}:{device_key}:button"
    )
    assert button_eid is not None

    # Before any event arrives, the entity already advertises the persisted types
    # and has not fired (state "unknown", event_type None).
    state = hass.states.get(button_eid)
    assert state.attributes["event_types"] == ["A", "B"]
    assert state.state == "unknown"
    assert state.attributes["event_type"] is None


async def test_event_always_available_and_no_double_fire_on_watchdog(
    hass, hub_entry_builder
):
    """An event entity stays available past the timeout and does not re-fire.

    A sibling measurement sensor on the same device goes ``unavailable`` once the
    silence timeout elapses, but the event entity (always-available) does not.
    The watchdog re-dispatches the cached last event by the same object, so the
    event entity's identity dedupe suppresses a re-fire: its last-fire state and
    timestamp are unchanged across the watchdog tick.
    """
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                # A measurement field (temperature) plus the event field, so the
                # device has a sibling that *can* time out.
                DEVICE_FIELDS: ["temperature_C", "button"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    button_eid = ent_reg.async_get_entity_id(
        "event", DOMAIN, f"{hub.entry_id}:{device_key}:button"
    )
    temp_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:T"
    )
    assert button_eid is not None
    assert temp_eid is not None

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(
            coordinator,
            {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.0, "button": "A"},
        )
        await hass.async_block_till_done()

    fired = hass.states.get(button_eid)
    assert fired.attributes["event_type"] == "A"
    fired_at = fired.state  # ISO timestamp of the fire

    # Advance past the 600s timeout and run the watchdog, which re-dispatches the
    # cached last event (same NormalizedEvent object) for the now-stale device.
    with freeze_time(start + timedelta(seconds=601)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()

    # The measurement sensor flips unavailable; the event entity does not.
    assert hass.states.get(temp_eid).state == "unavailable"
    after = hass.states.get(button_eid)
    assert after.state != "unavailable"
    # Identity dedupe suppressed a re-fire: last-fire state/type are unchanged.
    assert after.state == fired_at
    assert after.attributes["event_type"] == "A"


async def test_event_restores_last_fire_across_reload(hass, hub_entry_builder):
    """A reload restores the last fired event without firing a new one.

    After firing an event and reloading the entry, the rebuilt entity's state
    reflects the last fired event (HA restores it) and reload did not itself fire
    a fresh event (same timestamp / type, no construction-time replay).
    """
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["button"]}},
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    button_eid = ent_reg.async_get_entity_id(
        "event", DOMAIN, f"{hub.entry_id}:{device_key}:button"
    )
    assert button_eid is not None

    _feed(coordinator, {"model": "Acurite-606TX", "id": 42, "button": "A"})
    await hass.async_block_till_done()
    before = hass.states.get(button_eid)
    assert before.attributes["event_type"] == "A"
    fired_at = before.state

    # Reload the entry: the entity is rebuilt and HA restores the last fired
    # event. Construction must NOT replay the coordinator's cached event.
    assert await hass.config_entries.async_reload(hub.entry_id)
    await hass.async_block_till_done()

    after = hass.states.get(button_eid)
    # State reflects the last fire, restored verbatim (no new fire on reload).
    assert after.state == fired_at
    assert after.attributes["event_type"] == "A"
    # The persisted event_types survived the reload (seed the rebuilt entity).
    assert after.attributes["event_types"] == ["A"]


# --------------------------------------------------------------------------- #
# Replay suppression: event entities don't re-fire on reconnect replay, while  #
# sensors still seed; a genuinely-fresh event at reconnect still fires.        #
# --------------------------------------------------------------------------- #
async def _doorbell_hub(hass, hub_entry_builder):
    """Set up a hub with a seeded Honeywell doorbell event device + its eid."""
    device_key = "Honeywell-Doorbell-7"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "Honeywell-Doorbell",
                DEVICE_FIELDS: ["secret_knock"],
            }
        },
    )
    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id(
        "event", DOMAIN, f"{hub.entry_id}:{device_key}:secret_knock"
    )
    assert eid is not None
    return hub, _coordinator(hass, hub), eid


def _doorbell_press(events, event_time: str) -> dict:
    """Return the recorded doorbell-press fixture frame with ``time`` overridden.

    Uses the project event fixture (a real Honeywell-Doorbell ``secret_knock``
    frame) so the replay tests exercise a genuine event-device shape; the per-frame
    ``time`` is the only thing each scenario varies.
    """
    frame = dict(events("doorbell_event.json")[0])
    frame["time"] = event_time
    return frame


async def test_gap_event_is_suppressed_and_logged(
    hass, hub_entry_builder, events, caplog
):
    """A stale gap event after reconnect does NOT fire and is logged at INFO.

    Feed a live press (advances the mark, fires once), then — as a reconnect would
    — feed a frame whose ``time`` is newer than the mark but older than
    ``REPLAY_STALE_THRESHOLD`` (a transmission missed during the disconnect). The
    event entity must NOT fire (its last-fire state/timestamp are unchanged) yet
    the suppression is recorded at INFO so the user sees the real-but-stale press.
    """
    hub, coordinator, eid = await _doorbell_hub(hass, hub_entry_builder)

    # Live press at 10:00:00 UTC fires once (use ``Z`` so age is tz-independent).
    with freeze_time("2026-05-25T10:00:00+00:00"):
        _feed(coordinator, _doorbell_press(events, "2026-05-25T10:00:00Z"))
        await hass.async_block_till_done()
    fired = hass.states.get(eid)
    assert fired.attributes["event_type"] == "1"
    fired_at = fired.state

    # Reconnect: a gap event at 10:01:00 arrives at 10:05:00 -> 240s old -> stale.
    with (
        freeze_time("2026-05-25T10:05:00+00:00"),
        caplog.at_level(logging.INFO, logger="custom_components.rtl_433"),
    ):
        _feed(coordinator, _doorbell_press(events, "2026-05-25T10:01:00Z"))
        await hass.async_block_till_done()

    # The event entity did NOT fire again: last-fire state/timestamp unchanged.
    after = hass.states.get(eid)
    assert after.state == fired_at
    assert after.attributes["event_type"] == "1"
    # The suppression was logged at INFO (mentions the suppressed transmission).
    assert any(
        "suppressed" in r.message and r.levelno == logging.INFO for r in caplog.records
    )


async def test_no_double_fire_on_blip_replay(hass, hub_entry_builder, events):
    """Replaying the same frame (``time <= mark``) does NOT re-fire the event.

    A brief blip re-sends the recently-fired buffer tail; the high-water mark
    recognises it as already-seen so the event entity does not fire again.
    """
    hub, coordinator, eid = await _doorbell_hub(hass, hub_entry_builder)

    with freeze_time("2026-05-25T10:00:00+00:00"):
        _feed(coordinator, _doorbell_press(events, "2026-05-25T10:00:00Z"))
        await hass.async_block_till_done()
    fired_at = hass.states.get(eid).state

    # Reconnect replays the exact same frame (time == mark) -> no re-fire.
    with freeze_time("2026-05-25T10:00:03+00:00"):
        _feed(coordinator, _doorbell_press(events, "2026-05-25T10:00:00Z"))
        await hass.async_block_till_done()

    after = hass.states.get(eid)
    assert after.state == fired_at  # unchanged -> did not re-fire
    assert after.attributes["event_type"] == "1"


async def test_fresh_event_at_reconnect_still_fires(hass, hub_entry_builder, events):
    """A genuinely-fresh event at reconnect fires ("never drop a real one").

    With the mark already set by a prior live press, a frame whose ``time`` is
    within ``REPLAY_STALE_THRESHOLD`` of ``now`` is live and must fire even though
    it arrives right after a reconnect — there is no fixed suppression window.
    """
    hub, coordinator, eid = await _doorbell_hub(hass, hub_entry_builder)

    with freeze_time("2026-05-25T10:00:00+00:00"):
        _feed(coordinator, _doorbell_press(events, "2026-05-25T10:00:00Z"))
        await hass.async_block_till_done()
    first_fired_at = hass.states.get(eid).state

    # Fresh press 10s later (time ~ now) -> live -> fires (new, later timestamp).
    with freeze_time("2026-05-25T10:00:10+00:00"):
        _feed(coordinator, _doorbell_press(events, "2026-05-25T10:00:10Z"))
        await hass.async_block_till_done()

    after = hass.states.get(eid)
    assert after.attributes["event_type"] == "1"
    assert after.state != first_fired_at
    assert dt_util.parse_datetime(after.state) > dt_util.parse_datetime(first_fired_at)


async def test_sensor_seeds_from_replay_but_event_does_not_fire(
    hass, hub_entry_builder, caplog
):
    """A reconnect replay seeds a sensor's value while the event stays unfired.

    A device with both a measurement field (temperature) and an event field
    (button) is fed ONLY a stale replayed frame (no prior live frame). The sensor
    must seed its value and the device snapshot must record it, but the event
    entity must stay ``unknown`` (never fired) and ``last_seen`` is NOT stamped.
    """
    device_key = "Acurite-606TX-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        discovery_enabled=True,
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C", "button"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    temp_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:{device_key}:T"
    )
    button_eid = ent_reg.async_get_entity_id(
        "event", DOMAIN, f"{hub.entry_id}:{device_key}:button"
    )
    assert temp_eid is not None
    assert button_eid is not None

    # The base entity baselines ``last_seen`` to "now" at setup (so a restored
    # value shows until the timeout). A replay must not *advance* it, so capture
    # the baseline and assert the replayed frame leaves it untouched.
    baseline_seen = coordinator.last_seen[device_key]

    # Stale frame (frame time far behind now) -> replay: seeds sensor, no fire.
    with (
        freeze_time("2026-05-25T10:30:00+00:00"),
        caplog.at_level(logging.INFO, logger="custom_components.rtl_433"),
    ):
        _feed(
            coordinator,
            {
                "model": "Acurite-606TX",
                "id": 42,
                "temperature_C": 21.4,
                "button": "A",
                "time": "2026-05-25T10:00:00Z",
            },
        )
        await hass.async_block_till_done()

    # The sensor seeded its value from the replay.
    assert hass.states.get(temp_eid).state == "21.4"
    assert coordinator.devices[device_key].fields["temperature_C"] == 21.4
    # The event entity never fired (still unknown, no event_type).
    button_state = hass.states.get(button_eid)
    assert button_state.state == "unknown"
    assert button_state.attributes["event_type"] is None
    # Liveness was NOT refreshed by the replay: last_seen unchanged from the
    # setup baseline (the replay must not stamp it to the frame/now time).
    assert coordinator.last_seen[device_key] == baseline_seen
    # The suppressed event-field transmission was logged at INFO.
    assert any(
        "suppressed" in r.message and r.levelno == logging.INFO for r in caplog.records
    )


# --------------------------------------------------------------------------- #
# Component B — per-device calibration: reload gating + sensor wiring.         #
# --------------------------------------------------------------------------- #
async def test_calibration_change_reloads_hub_unrelated_upsert_does_not(
    hass, hub_entry_builder
):
    """A calibration change reloads the hub; an unrelated upsert does not.

    Writing a calibration into the devices map flips the coordinator's snapshot,
    so ``_async_update_listener`` reloads the hub. A routine devices-map upsert
    (a new field) leaves the calibration sub-record untouched, so the snapshot is
    unchanged and no reload fires.
    """
    from custom_components.rtl_433.entity import async_upsert_device

    device_key = "ERT-SCM-9001"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {CONF_MODEL: "ERT-SCM", DEVICE_FIELDS: ["consumption_data"]}
        },
    )

    calibration = {
        CALIBRATION_COMMODITY: COMMODITY_WATER,
        CALIBRATION_UNIT: "L",
        CALIBRATION_SCALE: 0.1,
    }

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        # Unrelated devices-map upsert (a newly observed field) must NOT reload.
        await async_upsert_device(
            hass, hub, device_key, model="ERT-SCM", fields=["battery_ok"]
        )
        await hass.async_block_till_done()
        assert reload_spy.call_count == 0

        # Writing a calibration into the record DOES reload (snapshot differs).
        devices = {k: dict(v) for k, v in hub.data[CONF_DEVICES].items()}
        devices[device_key][DEVICE_CALIBRATION] = calibration
        hass.config_entries.async_update_entry(
            hub, data={**hub.data, CONF_DEVICES: devices}
        )
        await hass.async_block_till_done()
        assert reload_spy.call_count == 1
        reload_spy.assert_called_with(hub.entry_id)


async def test_calibrated_consumption_sensor_is_energy_eligible(
    hass, hub_entry_builder
):
    """A water calibration rebuilds the consumption sensor as Energy-eligible.

    Seeding a ``{water, L, 0.1}`` calibration on a ``consumption_data`` device and
    setting the hub up makes the consumption ``Rtl433Sensor`` report
    ``device_class == water``, the chosen native unit, ``state_class ==
    total_increasing``, and a value of ``raw * 0.1``.
    """
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

    ent_reg = er.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"
    consumption = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{prefix}:consumption")
    assert consumption is not None

    # Feed a raw counter; the calibration scale (0.1) is applied to the value.
    _feed(
        _coordinator(hass, hub),
        {"model": "ERT-SCM", "id": 9001, "consumption_data": 1000},
    )
    await hass.async_block_till_done()

    state = hass.states.get(consumption)
    assert state.attributes["device_class"] == COMMODITY_WATER
    assert state.attributes["unit_of_measurement"] == "L"
    assert state.attributes["state_class"] == "total_increasing"
    # 1000 (raw) * 0.1 (scale) == 100.0.
    assert float(state.state) == 100.0


# --------------------------------------------------------------------------- #
# New-device persistent notification: restart-safe gating + de-duplication.    #
# --------------------------------------------------------------------------- #
# ``new_device_callback`` (custom_components/rtl_433/__init__.py) does
# ``from homeassistant.components import persistent_notification`` then calls
# ``persistent_notification.async_create(...)``, so the bound name to patch is on
# the integration module, NOT the homeassistant.components package. ``async_create``
# is a ``@callback`` (sync) helper, so ``patch``'s default (a sync ``MagicMock``,
# NOT an ``AsyncMock``) is the right stand-in.
_NOTIFY_TARGET = "custom_components.rtl_433.persistent_notification.async_create"


def _notify_id(entry_id: str, device_key: str) -> str:
    """The stable per-device notification id the callback must use."""
    return f"{DOMAIN}_new_device_{entry_id}_{device_key}"


async def test_new_device_notifies_once_with_stable_id_and_message(
    hass, hub_entry_builder, events
):
    """A genuinely-new device (discovery on) raises exactly one notification.

    The device is absent from the persisted ``entry.data[CONF_DEVICES]`` map, so
    the first sighting is genuinely new: ``async_create`` is called once with the
    stable ``{DOMAIN}_new_device_{entry_id}_{device_key}`` id and a message naming
    the model, the device key, and the hub. A second sighting in the same session
    (now persisted) does NOT notify again.
    """
    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)

    with patch(_NOTIFY_TARGET) as notify:
        _feed(coordinator, power_event)
        await hass.async_block_till_done()

        notify.assert_called_once()
        # The notification id is the stable per-device id.
        assert notify.call_args.kwargs["notification_id"] == _notify_id(
            hub.entry_id, device_key
        )
        # The message names the model, the device key, and the hub title.
        message = notify.call_args.args[1]
        assert "EnergyMeter-2000" in message
        assert device_key in message
        assert hub.title in message

        # Second sighting of the now-adopted device: no second notification.
        _feed(coordinator, power_event)
        await hass.async_block_till_done()
        notify.assert_called_once()


async def test_no_notification_for_known_device_on_restart_reload(
    hass, hub_entry_builder
):
    """The regression guard: a device already in the persisted map never notifies.

    Seed the hub with the device already present in ``entry.data[CONF_DEVICES]``
    (the restart-safe "ever-adopted" record), set the hub up (``coordinator.devices``
    starts empty), then feed that device's frame. The callback fires because the
    device is new to the in-memory ``coordinator.devices`` — but the persisted-map
    gate suppresses the notification. The reload variant re-confirms it: after
    ``async_reload`` empties ``coordinator.devices`` again, a fresh frame for the
    known device still raises nothing.
    """
    device_key = "EnergyMeter-2000-1234"
    frame = {"model": "EnergyMeter-2000", "id": 1234, "power_W": 1450.5}

    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        discovery_enabled=True,
        devices={
            device_key: {
                CONF_MODEL: "EnergyMeter-2000",
                DEVICE_FIELDS: ["power_W"],
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    # The in-memory set starts empty, so this device looks ``is_new`` to it.
    assert device_key not in coordinator.devices

    with patch(_NOTIFY_TARGET) as notify:
        _feed(coordinator, frame)
        await hass.async_block_till_done()
        # Known device (in the persisted map) -> the callback fired but did NOT
        # raise a notification.
        assert device_key in coordinator.devices
        notify.assert_not_called()

    # Reload variant: the reload rebuilds the coordinator with an empty
    # ``devices`` again, so the known device once more looks new in memory — and
    # must still raise nothing.
    with patch(_NOTIFY_TARGET) as notify:
        assert await hass.config_entries.async_reload(hub.entry_id)
        await hass.async_block_till_done()
        coordinator = _coordinator(hass, hub)
        assert device_key not in coordinator.devices

        _feed(coordinator, frame)
        await hass.async_block_till_done()
        notify.assert_not_called()


async def test_no_notification_when_discovery_off(hass, hub_entry_builder, events):
    """With discovery off the callback never fires, so no notification is raised."""
    power_event = events("power_sensor.json")[0]

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=False)
    coordinator = _coordinator(hass, hub)

    with patch(_NOTIFY_TARGET) as notify:
        _feed(coordinator, power_event)
        await hass.async_block_till_done()
        notify.assert_not_called()


async def test_delete_then_re_transmit_re_notifies_same_id(
    hass, hub_entry_builder, events
):
    """A deleted device that re-transmits re-notifies with the same stable id.

    Adopting the device notifies once. ``async_remove_config_entry_device`` drops
    it from the persisted map (and evicts the coordinator runtime state via
    ``forget_device``), making it genuinely new again, so a later transmission
    raises the notification a second time — with the SAME stable id, so the panel
    entry is replaced rather than stacked.
    """
    from custom_components.rtl_433 import async_remove_config_entry_device

    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)
    dev_reg = dr.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"
    expected_id = _notify_id(hub.entry_id, device_key)

    with patch(_NOTIFY_TARGET) as notify:
        # First adoption notifies once.
        _feed(coordinator, power_event)
        await hass.async_block_till_done()
        notify.assert_called_once()
        assert notify.call_args.kwargs["notification_id"] == expected_id

        # Remove the nested device: drops it from the map + evicts runtime state.
        device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, prefix)})
        assert device_entry is not None
        assert await async_remove_config_entry_device(hass, hub, device_entry) is True
        dev_reg.async_remove_device(device_entry.id)
        await hass.async_block_till_done()
        assert device_key not in hub.data.get(CONF_DEVICES, {})

        # Re-transmitting is genuinely new again -> a SECOND notification with the
        # SAME stable id (replaces, does not stack).
        _feed(coordinator, power_event)
        await hass.async_block_till_done()
        assert notify.call_count == 2
        assert notify.call_args.kwargs["notification_id"] == expected_id
