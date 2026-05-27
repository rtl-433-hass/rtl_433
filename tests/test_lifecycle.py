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
from unittest.mock import patch

from freezegun import freeze_time
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.rtl_433.const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DEVICES,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_HUB_ENTRY_ID,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
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
async def test_hub_diagnostic_sensors(hass, hub_entry_builder):
    """Hub diagnostic sensors render coordinator.meta / coordinator.stats."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)
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

    def state(suffix):
        eid = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{hub.entry_id}:hub:{suffix}"
        )
        assert eid is not None, suffix
        return hass.states.get(eid)

    # --- SDR/meta sensors ------------------------------------------------- #
    cf = state("center_frequency")
    assert cf.state == "433920000"
    assert cf.attributes["device_class"] == "frequency"
    assert cf.attributes["unit_of_measurement"] == "Hz"
    assert state("sample_rate").state == "250000"
    assert state("conversion_mode").state == "1"
    assert state("hop_interval").state == "600"
    assert state("gain").state == "auto"  # empty string -> auto
    assert state("ppm_error").state == "0"

    # --- Server-stats sensors --------------------------------------------- #
    events_state = state("decoded_events")
    assert events_state.state == "40"
    assert events_state.attributes["state_class"] == "total_increasing"
    assert state("ook_frames").state == "12"
    assert state("fsk_frames").state == "3"
    assert state("enabled_decoders").state == "5"

    # --- Array fields / since are attributes, not their own entities ------ #
    assert cf.attributes["frequencies"] == [433920000]
    assert cf.attributes["hop_times"] == [600]
    assert events_state.attributes["stats"] == [{"name": "Acurite", "events": 40}]
    assert events_state.attributes["since"] == "2026-05-26T10:00:00"

    # All ten hub sensors are diagnostic and live on the hub device.
    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    cf_eid = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{hub.entry_id}:hub:center_frequency"
    )
    cf_entry = ent_reg.async_get(cf_eid)
    assert cf_entry.device_id == hub_device.id
    assert cf_entry.entity_category == "diagnostic"


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

    def _recording_lookup(field_key, registry=None):
        seen_registries.append(registry)
        return real_lookup(field_key, registry)

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
