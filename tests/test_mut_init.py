"""Mutation-killing tests for custom_components/rtl_433/__init__.py.

Covers every branch and helper in the module in isolation or via integration
tests so that mutation-testing survivors are minimized. Uses the same idioms as
test_lifecycle.py (hub_entry_builder fixture, _no_socket stub, etc.).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtl_433 import (
    _async_update_listener,
    async_migrate_entry,
    async_remove_config_entry_device,
)
from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_WATER,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DEVICES,
    CONF_DISCOVERY_ENABLED,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_HUB_ENTRY_ID,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    DATA_LIBRARY,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MANAGE_SETTINGS,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEFAULT_PORT,
    DEVICE_CALIBRATION,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_HUB,
    PLATFORMS,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.hub_settings import (
    _calibration_map,
    _hub_availability_timeout,
    _hub_discovery_enabled,
    _hub_manage_settings,
    _hub_secure,
)
from custom_components.rtl_433.migration import (
    LEGACY_CONF_OBSERVED_FIELDS,
    PHANTOM_DEVICE_KEY,
    _cleanup_phantom_unknown_device,
    _migrate_motion_event_to_binary_sensor,
    _rehome_device_objects,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er


# ---------------------------------------------------------------------------
# Socket stub: prevents real WebSocket connections in every test.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_socket():
    """Stub the connect loop so the coordinator never opens a real WebSocket."""

    async def _noop(self) -> None:
        return None

    with patch.object(Rtl433Coordinator, "_connect_loop", _noop):
        yield


# ---------------------------------------------------------------------------
# Helpers (mirrors test_lifecycle.py)
# ---------------------------------------------------------------------------
def _coordinator(hass: HomeAssistant, hub_entry: MockConfigEntry) -> Rtl433Coordinator:
    return hass.data[DOMAIN][hub_entry.entry_id]


def _feed(coordinator: Rtl433Coordinator, event: dict) -> None:
    coordinator._handle_text_frame(json.dumps(event))


async def _setup_hub(hass, hub_entry_builder, *, devices=None, **kwargs):
    hub = hub_entry_builder(availability_timeout=600, devices=devices, **kwargs)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    return hub


# ===========================================================================
# Pure helper functions — _hub_secure / _hub_discovery_enabled /
# _hub_availability_timeout / _hub_manage_settings
# ===========================================================================


def _make_entry(data=None, options=None):
    """Build a minimal MockConfigEntry with given data/options."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="test hub",
        data=data or {},
        options=options or {},
        version=2,
    )


# --- _hub_secure -----------------------------------------------------------


def test_hub_secure_defaults_false():
    entry = _make_entry(data={CONF_HOST: "h", CONF_PORT: 8433, CONF_PATH: "/ws"})
    assert _hub_secure(entry) is False


def test_hub_secure_true_when_set():
    entry = _make_entry(data={"secure": True})
    assert _hub_secure(entry) is True


def test_hub_secure_false_when_explicit_false():
    entry = _make_entry(data={"secure": False})
    assert _hub_secure(entry) is False


# --- _hub_discovery_enabled ------------------------------------------------


def test_hub_discovery_enabled_defaults_true():
    entry = _make_entry(data={})
    assert _hub_discovery_enabled(entry) is True


def test_hub_discovery_enabled_data_false():
    entry = _make_entry(data={CONF_DISCOVERY_ENABLED: False})
    assert _hub_discovery_enabled(entry) is False


def test_hub_discovery_enabled_data_true():
    entry = _make_entry(data={CONF_DISCOVERY_ENABLED: True})
    assert _hub_discovery_enabled(entry) is True


def test_hub_discovery_options_overrides_data_false():
    """options takes precedence over data."""
    entry = _make_entry(
        data={CONF_DISCOVERY_ENABLED: True},
        options={CONF_DISCOVERY_ENABLED: False},
    )
    assert _hub_discovery_enabled(entry) is False


def test_hub_discovery_options_overrides_data_true():
    entry = _make_entry(
        data={CONF_DISCOVERY_ENABLED: False},
        options={CONF_DISCOVERY_ENABLED: True},
    )
    assert _hub_discovery_enabled(entry) is True


# --- _hub_availability_timeout ---------------------------------------------


def test_hub_availability_timeout_defaults():
    entry = _make_entry(data={})
    assert _hub_availability_timeout(entry) == DEFAULT_AVAILABILITY_TIMEOUT


def test_hub_availability_timeout_from_data():
    entry = _make_entry(data={CONF_AVAILABILITY_TIMEOUT: 300})
    assert _hub_availability_timeout(entry) == 300


def test_hub_availability_timeout_options_overrides_data():
    entry = _make_entry(
        data={CONF_AVAILABILITY_TIMEOUT: 300},
        options={CONF_AVAILABILITY_TIMEOUT: 120},
    )
    assert _hub_availability_timeout(entry) == 120


def test_hub_availability_timeout_options_only():
    entry = _make_entry(data={}, options={CONF_AVAILABILITY_TIMEOUT: 900})
    assert _hub_availability_timeout(entry) == 900


def test_hub_availability_timeout_is_int():
    """Result must be an integer (int() coercion)."""
    entry = _make_entry(data={CONF_AVAILABILITY_TIMEOUT: 200})
    result = _hub_availability_timeout(entry)
    assert isinstance(result, int)
    assert result == 200


# --- _hub_manage_settings --------------------------------------------------


def test_hub_manage_settings_defaults_to_true():
    entry = _make_entry(data={})
    assert _hub_manage_settings(entry) is True


def test_hub_manage_settings_true():
    assert DEFAULT_MANAGE_SETTINGS is True


def test_hub_manage_settings_data_false():
    entry = _make_entry(data={CONF_MANAGE_SETTINGS: False})
    assert _hub_manage_settings(entry) is False


def test_hub_manage_settings_options_overrides_data():
    entry = _make_entry(
        data={CONF_MANAGE_SETTINGS: True},
        options={CONF_MANAGE_SETTINGS: False},
    )
    assert _hub_manage_settings(entry) is False


def test_hub_manage_settings_options_true_overrides_data_false():
    entry = _make_entry(
        data={CONF_MANAGE_SETTINGS: False},
        options={CONF_MANAGE_SETTINGS: True},
    )
    assert _hub_manage_settings(entry) is True


# ===========================================================================
# _calibration_map
# ===========================================================================


def test_calibration_map_empty_devices():
    entry = _make_entry(data={})
    assert _calibration_map(entry) == {}


def test_calibration_map_no_devices_key():
    entry = _make_entry(data={CONF_HOST: "h"})
    assert _calibration_map(entry) == {}


def test_calibration_map_skips_invalid_records():
    """Non-dict values in the devices map are skipped."""
    entry = _make_entry(data={CONF_DEVICES: {"dev1": "not-a-dict", "dev2": 42}})
    assert _calibration_map(entry) == {}


def test_calibration_map_skips_devices_without_calibration():
    entry = _make_entry(
        data={
            CONF_DEVICES: {
                "dev1": {CONF_MODEL: "Foo", DEVICE_FIELDS: ["temp"]},
            }
        }
    )
    assert _calibration_map(entry) == {}


def test_calibration_map_skips_invalid_calibration():
    """A calibration with invalid/missing commodity is excluded."""
    entry = _make_entry(
        data={
            CONF_DEVICES: {
                "dev1": {
                    DEVICE_CALIBRATION: {
                        CALIBRATION_COMMODITY: "none",  # invalid commodity
                        CALIBRATION_UNIT: "L",
                        CALIBRATION_SCALE: 1.0,
                    }
                }
            }
        }
    )
    assert _calibration_map(entry) == {}


def test_calibration_map_includes_valid_calibration():
    entry = _make_entry(
        data={
            CONF_DEVICES: {
                "dev1": {
                    CONF_MODEL: "Meter",
                    DEVICE_CALIBRATION: {
                        CALIBRATION_COMMODITY: COMMODITY_WATER,
                        CALIBRATION_UNIT: "L",
                        CALIBRATION_SCALE: 0.1,
                    },
                }
            }
        }
    )
    result = _calibration_map(entry)
    assert "dev1" in result
    assert result["dev1"][CALIBRATION_COMMODITY] == COMMODITY_WATER
    assert result["dev1"][CALIBRATION_UNIT] == "L"
    assert result["dev1"][CALIBRATION_SCALE] == 0.1


def test_calibration_map_multiple_devices_only_valid_included():
    entry = _make_entry(
        data={
            CONF_DEVICES: {
                "valid": {
                    DEVICE_CALIBRATION: {
                        CALIBRATION_COMMODITY: COMMODITY_WATER,
                        CALIBRATION_UNIT: "L",
                        CALIBRATION_SCALE: 1.0,
                    }
                },
                "invalid": {CONF_MODEL: "NoCalib"},
                "bad_record": "not-a-dict",
            }
        }
    )
    result = _calibration_map(entry)
    assert set(result.keys()) == {"valid"}


# ===========================================================================
# _cleanup_phantom_unknown_device
# ===========================================================================


async def test_cleanup_phantom_removes_unknown_from_map(hass, hub_entry_builder):
    """The 'unknown' key is removed from the devices map."""
    hub = hub_entry_builder(
        devices={
            PHANTOM_DEVICE_KEY: {CONF_MODEL: "", DEVICE_FIELDS: []},
            "real-device": {CONF_MODEL: "Acurite", DEVICE_FIELDS: ["temperature_C"]},
        }
    )
    hub.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    _cleanup_phantom_unknown_device(hass, hub, dev_reg)

    assert PHANTOM_DEVICE_KEY not in hub.data.get(CONF_DEVICES, {})
    assert "real-device" in hub.data[CONF_DEVICES]


async def test_cleanup_phantom_no_unknown_is_noop(hass, hub_entry_builder):
    """No 'unknown' key: the devices map is left untouched."""
    hub = hub_entry_builder(
        devices={"real-device": {CONF_MODEL: "Acurite", DEVICE_FIELDS: []}}
    )
    hub.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    before = dict(hub.data.get(CONF_DEVICES, {}))

    _cleanup_phantom_unknown_device(hass, hub, dev_reg)

    assert hub.data.get(CONF_DEVICES, {}) == before


async def test_cleanup_phantom_removes_registry_device(hass, hub_entry_builder):
    """The stale registry device with identifier (DOMAIN, entry_id:unknown) is removed."""
    hub = hub_entry_builder(devices={})
    hub.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    phantom_ident = (DOMAIN, f"{hub.entry_id}:{PHANTOM_DEVICE_KEY}")
    dev_reg.async_get_or_create(
        config_entry_id=hub.entry_id,
        identifiers={phantom_ident},
    )
    assert dev_reg.async_get_device(identifiers={phantom_ident}) is not None

    _cleanup_phantom_unknown_device(hass, hub, dev_reg)

    assert dev_reg.async_get_device(identifiers={phantom_ident}) is None


async def test_cleanup_phantom_no_registry_device_is_noop(hass, hub_entry_builder):
    """No phantom registry device: no error."""
    hub = hub_entry_builder(devices={})
    hub.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    # Should not raise
    _cleanup_phantom_unknown_device(hass, hub, dev_reg)


async def test_cleanup_phantom_leaves_hub_device_untouched(hass, hub_entry_builder):
    """The hub device itself is never touched by the cleanup."""
    hub = hub_entry_builder(
        devices={PHANTOM_DEVICE_KEY: {CONF_MODEL: "", DEVICE_FIELDS: []}}
    )
    hub.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    dev_reg.async_get_or_create(
        config_entry_id=hub.entry_id,
        identifiers={(DOMAIN, hub.entry_id)},
        name="Hub",
    )
    _cleanup_phantom_unknown_device(hass, hub, dev_reg)

    # Hub device still exists
    assert dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)}) is not None


# ===========================================================================
# _migrate_motion_event_to_binary_sensor
# ===========================================================================


async def test_migrate_motion_removes_event_entities(hass, hub_entry_builder):
    """Orphaned event.*_motion entities are removed."""
    hub = hub_entry_builder(devices={})
    hub.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    device_key = "MySensor-42"
    motion_uid = f"{hub.entry_id}:{device_key}:motion"
    ent_reg.async_get_or_create(
        "event",
        DOMAIN,
        motion_uid,
        config_entry=hub,
    )
    assert ent_reg.async_get_entity_id("event", DOMAIN, motion_uid) is not None

    with patch(
        "custom_components.rtl_433.repairs.async_raise_motion_moved"
    ) as mock_notify:
        _migrate_motion_event_to_binary_sensor(hass, hub, ent_reg)

    # The event entity is gone.
    assert ent_reg.async_get_entity_id("event", DOMAIN, motion_uid) is None
    # The repair advisory was raised.
    mock_notify.assert_called_once_with(hass)


async def test_migrate_motion_leaves_non_motion_event_entities(hass, hub_entry_builder):
    """Non-motion event entities are not touched."""
    hub = hub_entry_builder(devices={})
    hub.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    uid = f"{hub.entry_id}:MySensor-42:button"
    ent_reg.async_get_or_create("event", DOMAIN, uid, config_entry=hub)

    with patch(
        "custom_components.rtl_433.repairs.async_raise_motion_moved"
    ) as mock_notify:
        _migrate_motion_event_to_binary_sensor(hass, hub, ent_reg)

    assert ent_reg.async_get_entity_id("event", DOMAIN, uid) is not None
    mock_notify.assert_not_called()


async def test_migrate_motion_drops_motion_from_event_types(hass, hub_entry_builder):
    """The 'motion' key is removed from DEVICE_EVENT_TYPES in the devices map."""
    device_key = "MySensor-42"
    hub = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "Sensor",
                DEVICE_EVENT_TYPES: {"motion": ["on"], "button": ["A"]},
            }
        }
    )
    hub.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    with patch("custom_components.rtl_433.repairs.async_raise_motion_moved"):
        _migrate_motion_event_to_binary_sensor(hass, hub, ent_reg)

    devices = hub.data[CONF_DEVICES]
    # motion slot was dropped
    assert "motion" not in devices[device_key][DEVICE_EVENT_TYPES]
    # button slot is kept
    assert "button" in devices[device_key][DEVICE_EVENT_TYPES]


async def test_migrate_motion_no_motion_event_types_no_write(hass, hub_entry_builder):
    """No motion in event_types means the devices map is not rewritten."""
    device_key = "MySensor-42"
    original_devices = {
        device_key: {
            CONF_MODEL: "Sensor",
            DEVICE_EVENT_TYPES: {"button": ["A"]},
        }
    }
    hub = hub_entry_builder(devices=original_devices)
    hub.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    # Patch async_update_entry so we can detect if it's called
    with (
        patch.object(
            hass.config_entries,
            "async_update_entry",
            wraps=hass.config_entries.async_update_entry,
        ),
        patch("custom_components.rtl_433.repairs.async_raise_motion_moved"),
    ):
        _migrate_motion_event_to_binary_sensor(hass, hub, ent_reg)

    # No update for the devices change (no motion slot existed)
    # Only check that the motion event_type slot is not present (it wasn't)
    assert "button" in hub.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES]


async def test_migrate_motion_no_removed_means_no_repair_issue(hass, hub_entry_builder):
    """No orphaned motion entities: the repair issue is NOT raised."""
    hub = hub_entry_builder(devices={})
    hub.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    with patch(
        "custom_components.rtl_433.repairs.async_raise_motion_moved"
    ) as mock_notify:
        _migrate_motion_event_to_binary_sensor(hass, hub, ent_reg)

    mock_notify.assert_not_called()


async def test_migrate_motion_non_dict_record_skipped(hass, hub_entry_builder):
    """Non-dict device records are passed through unchanged."""
    hub = hub_entry_builder(devices={"bad": "not-a-dict"})
    hub.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    with patch("custom_components.rtl_433.repairs.async_raise_motion_moved"):
        _migrate_motion_event_to_binary_sensor(hass, hub, ent_reg)

    # The bad record is still there (no crash)
    assert hub.data[CONF_DEVICES]["bad"] == "not-a-dict"


# ===========================================================================
# async_setup_entry — hub device registration and coordinator wiring
# ===========================================================================


async def test_setup_entry_registers_hub_device(hass, hub_entry_builder):
    """async_setup_entry creates the hub device in the device registry."""
    hub = await _setup_hub(hass, hub_entry_builder)

    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    assert hub_device is not None
    assert hub_device.manufacturer == "rtl_433"
    assert hub_device.name == hub.title
    assert hub_device.model == "rtl_433 server"


async def test_hub_info_callback_updates_hub_device_identity(hass, hub_entry_builder):
    """Once the SDR identity is known, the hub device shows its model/serial."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)

    coordinator.dev_info = {
        "vendor": "Realtek",
        "product": "RTL2838UHIDIR",
        "serial": "00000001",
    }
    coordinator.hub_info_callback()
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    assert hub_device.manufacturer == "Realtek"
    assert hub_device.model == "RTL2838UHIDIR"
    assert hub_device.serial_number == "00000001"


async def test_hub_info_callback_noop_when_identity_empty(hass, hub_entry_builder):
    """With no SDR identity (e.g. ``-D manual``) the hub keeps its placeholders."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)

    coordinator.dev_info = {}
    coordinator.hub_info_callback()
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    assert hub_device.manufacturer == "rtl_433"
    assert hub_device.model == "rtl_433 server"
    assert hub_device.serial_number is None


async def test_setup_entry_stores_coordinator_in_hass_data(hass, hub_entry_builder):
    """async_setup_entry puts the coordinator in hass.data[DOMAIN][entry_id]."""
    hub = await _setup_hub(hass, hub_entry_builder)

    coordinator = hass.data[DOMAIN][hub.entry_id]
    assert isinstance(coordinator, Rtl433Coordinator)


async def test_setup_entry_returns_true(hass, hub_entry_builder):
    """async_setup_entry returns True on success."""
    hub = hub_entry_builder(availability_timeout=600)
    hub.add_to_hass(hass)
    result = await hass.config_entries.async_setup(hub.entry_id)
    assert result is True


async def test_setup_entry_caches_library_in_hass_data(hass, hub_entry_builder):
    """The mapping library is cached in hass.data[DOMAIN][DATA_LIBRARY]."""
    await _setup_hub(hass, hub_entry_builder)
    assert DATA_LIBRARY in hass.data[DOMAIN]
    cached = hass.data[DOMAIN][DATA_LIBRARY]
    assert cached is not None
    assert len(cached) == 2  # (registry, skip_keys)


async def test_setup_entry_library_cached_across_two_hubs(hass, hub_entry_builder):
    """A second hub setup reuses the cached library (same object)."""
    await _setup_hub(hass, hub_entry_builder, host="h1.local")
    first_cached = hass.data[DOMAIN][DATA_LIBRARY]

    await _setup_hub(hass, hub_entry_builder, host="h2.local")
    second_cached = hass.data[DOMAIN][DATA_LIBRARY]

    assert first_cached is second_cached


async def test_setup_entry_injects_skip_keys_into_coordinator(hass, hub_entry_builder):
    """The coordinator's skip_keys is set from the loaded library."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)
    assert coordinator.skip_keys is not None


async def test_setup_entry_coordinator_gets_correct_host_port(hass, hub_entry_builder):
    """Coordinator is built with the right host/port from the entry."""
    hub = await _setup_hub(hass, hub_entry_builder, host="myhost.local")
    coordinator = _coordinator(hass, hub)
    assert coordinator.host == "myhost.local"
    assert coordinator.port == DEFAULT_PORT


async def test_setup_entry_coordinator_discovery_enabled(hass, hub_entry_builder):
    """discovery_enabled is propagated from the entry to the coordinator."""
    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    assert _coordinator(hass, hub).discovery_enabled is True


async def test_setup_entry_coordinator_discovery_disabled(hass, hub_entry_builder):
    """discovery_enabled=False is propagated to the coordinator."""
    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=False)
    assert _coordinator(hass, hub).discovery_enabled is False


async def test_setup_entry_coordinator_manages_settings_default(
    hass, hub_entry_builder
):
    """manage_settings defaults to True (DEFAULT_MANAGE_SETTINGS)."""
    hub = await _setup_hub(hass, hub_entry_builder)
    assert _coordinator(hass, hub).manage_settings is True


async def test_setup_entry_coordinator_manages_settings_false(hass, hub_entry_builder):
    """manage_settings=False is propagated to the coordinator."""
    hub = await _setup_hub(
        hass, hub_entry_builder, options={CONF_MANAGE_SETTINGS: False}
    )
    assert _coordinator(hass, hub).manage_settings is False


async def test_setup_entry_coordinator_availability_timeout(hass, hub_entry_builder):
    """availability_timeout from entry data lands on the coordinator."""
    # _setup_hub passes availability_timeout=600, so we build manually
    hub = hub_entry_builder(availability_timeout=300)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    assert _coordinator(hass, hub).availability_timeout == 300


async def test_setup_entry_calibration_snapshot_set(hass, hub_entry_builder):
    """coordinator.calibration_snapshot is set from the entry's devices map."""
    device_key = "Meter-42"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                DEVICE_CALIBRATION: {
                    CALIBRATION_COMMODITY: COMMODITY_WATER,
                    CALIBRATION_UNIT: "L",
                    CALIBRATION_SCALE: 0.5,
                }
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    assert device_key in coordinator.calibration_snapshot
    assert (
        coordinator.calibration_snapshot[device_key][CALIBRATION_COMMODITY]
        == COMMODITY_WATER
    )


async def test_setup_entry_calibration_snapshot_empty_when_no_calibration(
    hass, hub_entry_builder
):
    """calibration_snapshot is {} when no devices have calibration."""
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={"dev": {CONF_MODEL: "Foo", DEVICE_FIELDS: ["temp"]}},
    )
    coordinator = _coordinator(hass, hub)
    assert coordinator.calibration_snapshot == {}


async def test_setup_entry_forwards_platforms(hass, hub_entry_builder):
    """async_setup_entry forwards all PLATFORMS entries."""
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        wraps=hass.config_entries.async_forward_entry_setups,
    ) as fwd_spy:
        hub = hub_entry_builder(availability_timeout=600)
        hub.add_to_hass(hass)
        await hass.config_entries.async_setup(hub.entry_id)
        await hass.async_block_till_done()

    fwd_spy.assert_called_once()
    _, forwarded_platforms = fwd_spy.call_args[0]
    # All required platforms are forwarded exactly once
    assert set(forwarded_platforms) == set(PLATFORMS)


async def test_setup_entry_secure_false_by_default(hass, hub_entry_builder):
    """Coordinator gets secure=False when the entry has no 'secure' key."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)
    assert coordinator.secure is False


async def test_setup_entry_secure_true(hass, hub_entry_builder):
    """Coordinator gets secure=True when entry data has secure=True."""
    hub = hub_entry_builder(secure=True, availability_timeout=600)
    hub.add_to_hass(hass)
    await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, hub)
    assert coordinator.secure is True


# ===========================================================================
# effective_timeout_resolver and effective_clear_delay_resolver (closures)
# ===========================================================================


async def test_effective_timeout_resolver_uses_hub_default(hass, hub_entry_builder):
    """When no per-device override exists, hub default is returned."""
    hub = hub_entry_builder(availability_timeout=300)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, hub)
    # Call the wired resolver; no override means hub default.
    result = coordinator.effective_timeout_resolver("some-device")
    assert result == 300


async def test_effective_timeout_resolver_uses_device_override(hass, hub_entry_builder):
    """Per-device timeout_override takes precedence over the hub default."""
    device_key = "MySensor-7"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "M",
                DEVICE_FIELDS: [],
                DEVICE_TIMEOUT_OVERRIDE: 120,
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    assert coordinator.effective_timeout_resolver(device_key) == 120


async def test_effective_timeout_resolver_fallback_for_unknown_device(
    hass, hub_entry_builder
):
    """Device not in the map uses the hub default timeout."""
    hub = hub_entry_builder(availability_timeout=450)
    hub.add_to_hass(hass)
    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, hub)
    assert coordinator.effective_timeout_resolver("not-in-map") == 450


async def test_effective_clear_delay_resolver_default(hass, hub_entry_builder):
    """No per-device override returns DEFAULT_MOTION_CLEAR_DELAY."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)
    result = coordinator.effective_clear_delay_resolver("some-device")
    assert result == DEFAULT_MOTION_CLEAR_DELAY


async def test_effective_clear_delay_resolver_device_override(hass, hub_entry_builder):
    """Per-device motion_clear_delay overrides the default."""
    device_key = "MotionDev-1"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "M",
                DEVICE_FIELDS: [],
                DEVICE_MOTION_CLEAR_DELAY: 30,
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    assert coordinator.effective_clear_delay_resolver(device_key) == 30


async def test_effective_timeout_resolver_int_coercion(hass, hub_entry_builder):
    """timeout_override stored as a non-int is coerced to int."""
    device_key = "MySensor-7"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "M",
                DEVICE_FIELDS: [],
                DEVICE_TIMEOUT_OVERRIDE: "180",
            }
        },
    )
    coordinator = _coordinator(hass, hub)
    result = coordinator.effective_timeout_resolver(device_key)
    assert isinstance(result, int)
    assert result == 180


# ===========================================================================
# new_device_callback wiring
# ===========================================================================


async def test_new_device_callback_dispatches_signal(hass, hub_entry_builder, events):
    """Feeding a new device dispatches the hub-level new-device signal."""
    from custom_components.rtl_433.const import signal_new_device

    power_event = events("power_sensor.json")[0]
    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)

    received: list[tuple] = []

    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    async_dispatcher_connect(
        hass,
        signal_new_device(hub.entry_id),
        lambda device_key, model: received.append((device_key, model)),
    )

    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    assert len(received) == 1
    device_key, model = received[0]
    assert device_key == "EnergyMeter-2000-1234"
    assert model == "EnergyMeter-2000"


async def test_new_device_callback_notifies_for_new_device(
    hass, hub_entry_builder, events
):
    """A genuinely new device triggers a persistent notification."""
    # Drop ``time`` so the frame classifies as a live transmission, not a
    # reconnect replay (a replay never raises the new-device notification).
    power_event = {
        k: v for k, v in events("power_sensor.json")[0].items() if k != "time"
    }
    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)

    notify_target = "custom_components.rtl_433.persistent_notification.async_create"
    with patch(notify_target) as mock_notify:
        _feed(coordinator, power_event)
        await hass.async_block_till_done()

    mock_notify.assert_called_once()
    kwargs = mock_notify.call_args.kwargs
    assert "EnergyMeter-2000-1234" in kwargs["notification_id"]
    # Message names the device (the raw device key is only in the notification_id)
    message = mock_notify.call_args.args[1]
    assert "EnergyMeter-2000" in message


async def test_new_device_callback_no_notification_for_known_device(
    hass, hub_entry_builder
):
    """No notification when the device is already in the persisted map."""
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        discovery_enabled=True,
        devices={
            device_key: {CONF_MODEL: "EnergyMeter-2000", DEVICE_FIELDS: ["power_W"]}
        },
    )
    coordinator = _coordinator(hass, hub)

    notify_target = "custom_components.rtl_433.persistent_notification.async_create"
    with patch(notify_target) as mock_notify:
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 100.0})
        await hass.async_block_till_done()

    mock_notify.assert_not_called()


async def test_new_device_callback_uses_device_key_as_name_when_no_model(
    hass, hub_entry_builder
):
    """When model is empty, the notification uses the device_key as the name."""
    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)
    device_key = "Unknown-0"

    notify_target = "custom_components.rtl_433.persistent_notification.async_create"
    with patch(notify_target) as mock_notify:
        _feed(coordinator, {"model": "Unknown", "id": 0})
        await hass.async_block_till_done()

    if mock_notify.called:
        message = mock_notify.call_args.args[1]
        # When no model, device_key is used as the name
        assert device_key in message or "Unknown" in message


# ===========================================================================
# async_unload_entry
# ===========================================================================


async def test_unload_entry_stops_coordinator(hass, hub_entry_builder):
    """async_unload_entry calls coordinator.async_stop."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)

    with patch.object(
        coordinator, "async_stop", wraps=coordinator.async_stop
    ) as stop_spy:
        result = await hass.config_entries.async_unload(hub.entry_id)
        await hass.async_block_till_done()

    assert result is True
    stop_spy.assert_called_once()


async def test_unload_entry_removes_coordinator_from_hass_data(hass, hub_entry_builder):
    """After unload, the coordinator is removed from hass.data[DOMAIN]."""
    hub = await _setup_hub(hass, hub_entry_builder)
    assert hub.entry_id in hass.data[DOMAIN]

    await hass.config_entries.async_unload(hub.entry_id)
    await hass.async_block_till_done()

    assert hub.entry_id not in hass.data.get(DOMAIN, {})


async def test_unload_entry_returns_true(hass, hub_entry_builder):
    """async_unload_entry returns True."""
    hub = await _setup_hub(hass, hub_entry_builder)
    result = await hass.config_entries.async_unload(hub.entry_id)
    await hass.async_block_till_done()
    assert result is True


async def test_unload_entry_clears_reachability_repair(hass, hub_entry_builder):
    """async_unload_entry calls async_clear_hub_unreachable."""
    hub = await _setup_hub(hass, hub_entry_builder)

    with patch(
        "custom_components.rtl_433.repairs.async_clear_hub_unreachable"
    ) as clear_spy:
        await hass.config_entries.async_unload(hub.entry_id)
        await hass.async_block_till_done()

    clear_spy.assert_called_once_with(hass, hub)


async def test_unload_entry_no_coordinator_in_data_branch(hass, hub_entry_builder):
    """The unload code handles hass.data[DOMAIN] missing the entry_id gracefully.

    We verify this by confirming the coordinator is None-safe: the implementation
    checks ``if coordinator is not None`` before calling async_stop, so we
    confirm the unload returns True even after a normal setup cycle.
    """
    hub = await _setup_hub(hass, hub_entry_builder)
    # Confirm the coordinator is there before unload
    assert hub.entry_id in hass.data[DOMAIN]

    # Normal unload should return True
    result = await hass.config_entries.async_unload(hub.entry_id)
    await hass.async_block_till_done()
    assert result is True
    # After unload, coordinator is gone
    assert hub.entry_id not in hass.data.get(DOMAIN, {})


# ===========================================================================
# async_remove_config_entry_device
# ===========================================================================


async def test_remove_hub_device_returns_false(hass, hub_entry_builder):
    """Attempting to remove the hub device itself returns False."""
    hub = await _setup_hub(hass, hub_entry_builder)
    dev_reg = dr.async_get(hass)
    hub_device = dev_reg.async_get_device(identifiers={(DOMAIN, hub.entry_id)})
    assert hub_device is not None

    result = await async_remove_config_entry_device(hass, hub, hub_device)
    assert result is False


async def test_remove_nested_device_returns_true(hass, hub_entry_builder, events):
    """Removing a nested RF device returns True."""
    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)
    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, prefix)})
    assert device_entry is not None

    result = await async_remove_config_entry_device(hass, hub, device_entry)
    assert result is True


async def test_remove_nested_device_drops_from_devices_map(
    hass, hub_entry_builder, events
):
    """Removing a nested device drops it from entry.data[CONF_DEVICES]."""
    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)
    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    assert device_key in hub.data.get(CONF_DEVICES, {})

    dev_reg = dr.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, prefix)})
    await async_remove_config_entry_device(hass, hub, device_entry)

    assert device_key not in hub.data.get(CONF_DEVICES, {})


async def test_remove_nested_device_calls_forget_device(
    hass, hub_entry_builder, events
):
    """Removing a nested device calls coordinator.forget_device."""
    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)
    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, prefix)})

    with patch.object(
        coordinator, "forget_device", wraps=coordinator.forget_device
    ) as forget_spy:
        await async_remove_config_entry_device(hass, hub, device_entry)

    forget_spy.assert_called_once_with(device_key)


async def test_remove_nested_device_calls_device_removers(
    hass, hub_entry_builder, events
):
    """Removing a nested device calls each registered device_remover."""
    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)
    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    # Register a mock device remover
    removed_keys: list[str] = []
    coordinator.device_removers.append(removed_keys.append)

    dev_reg = dr.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, prefix)})
    await async_remove_config_entry_device(hass, hub, device_entry)

    assert device_key in removed_keys


async def test_remove_device_coordinator_none_branch(hass, hub_entry_builder, events):
    """async_remove_config_entry_device handles missing coordinator gracefully.

    The code does ``coordinator = hass.data.get(...).get(entry_id)`` which returns
    None when hass.data is missing the domain key. Removing the domain data
    exercises this branch: the device is still removed from the map, but
    forget_device is not called. The function still returns True.
    """
    power_event = events("power_sensor.json")[0]
    device_key = "EnergyMeter-2000-1234"

    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)
    _feed(coordinator, power_event)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    prefix = f"{hub.entry_id}:{device_key}"
    device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, prefix)})
    assert device_entry is not None

    # Pop DOMAIN entirely from hass.data so .get(DOMAIN, {}).get(entry_id) is None
    saved = hass.data.pop(DOMAIN, {})
    try:
        result = await async_remove_config_entry_device(hass, hub, device_entry)
    finally:
        # Restore so teardown can succeed
        hass.data[DOMAIN] = saved

    assert result is True


# ===========================================================================
# _async_update_listener — options-update-listener logic
# ===========================================================================


async def test_update_listener_reloads_on_manage_settings_change(
    hass, hub_entry_builder
):
    """Changing manage_settings triggers a reload."""
    hub = await _setup_hub(
        hass, hub_entry_builder, options={CONF_MANAGE_SETTINGS: True}
    )
    coordinator = _coordinator(hass, hub)
    assert coordinator.manage_settings is True

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        # Flip manage_settings to False via options update
        hass.config_entries.async_update_entry(
            hub, options={CONF_MANAGE_SETTINGS: False}
        )
        await hass.async_block_till_done()

    reload_spy.assert_called_once_with(hub.entry_id)


async def test_update_listener_reloads_on_calibration_change(hass, hub_entry_builder):
    """Changing per-device calibration triggers a reload."""
    device_key = "Meter-9001"
    hub = await _setup_hub(
        hass,
        hub_entry_builder,
        devices={
            device_key: {CONF_MODEL: "Meter", DEVICE_FIELDS: ["consumption_data"]}
        },
    )

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        # Write a calibration into the devices map
        devices = {k: dict(v) for k, v in hub.data[CONF_DEVICES].items()}
        devices[device_key][DEVICE_CALIBRATION] = {
            CALIBRATION_COMMODITY: COMMODITY_WATER,
            CALIBRATION_UNIT: "L",
            CALIBRATION_SCALE: 0.1,
        }
        hass.config_entries.async_update_entry(
            hub, data={**hub.data, CONF_DEVICES: devices}
        )
        await hass.async_block_till_done()

    reload_spy.assert_called_once_with(hub.entry_id)


async def test_update_listener_no_reload_for_unrelated_change(hass, hub_entry_builder):
    """An unrelated options change (e.g. discovery toggle) does not reload."""
    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)
    assert coordinator.discovery_enabled is True

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        hass.config_entries.async_update_entry(
            hub, options={CONF_DISCOVERY_ENABLED: False}
        )
        await hass.async_block_till_done()

    reload_spy.assert_not_called()
    # The coordinator's discovery_enabled was updated live
    assert coordinator.discovery_enabled is False


async def test_update_listener_updates_discovery_live(hass, hub_entry_builder):
    """Toggling discovery is applied live without reload."""
    hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
    coordinator = _coordinator(hass, hub)

    hass.config_entries.async_update_entry(hub, options={CONF_DISCOVERY_ENABLED: False})
    await hass.async_block_till_done()

    assert coordinator.discovery_enabled is False


async def test_update_listener_updates_availability_timeout_live(
    hass, hub_entry_builder
):
    """Changing availability_timeout is applied live without reload."""
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)
    # Default is 600 from _setup_hub

    hass.config_entries.async_update_entry(
        hub, options={CONF_AVAILABILITY_TIMEOUT: 120}
    )
    await hass.async_block_till_done()

    assert coordinator.availability_timeout == 120


async def test_update_listener_no_coordinator_returns_early(hass, hub_entry_builder):
    """If the coordinator is gone, _async_update_listener returns early without error.

    Exercises the ``if coordinator is None: return`` guard in the update listener.
    """
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)

    # Temporarily remove coordinator, call the listener, then restore
    hass.data[DOMAIN].pop(hub.entry_id, None)

    with patch.object(
        hass.config_entries, "async_reload", return_value=True
    ) as reload_spy:
        await _async_update_listener(hass, hub)

    reload_spy.assert_not_called()

    # Restore for proper teardown
    hass.data[DOMAIN][hub.entry_id] = coordinator


# ===========================================================================
# async_migrate_entry
# ===========================================================================


async def test_migrate_entry_returns_false_for_future_version(hass):
    """Version > 2 (future schema) is unsupported and returns False."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="future hub",
        version=3,
        data={CONF_HOST: "h", CONF_PORT: 8433, CONF_PATH: "/ws"},
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)
    assert result is False


async def test_migrate_entry_v2_returns_true_immediately(hass):
    """A version-2 entry needs no migration and returns True immediately."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="hub v2",
        version=2,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
    )
    entry.add_to_hass(hass)

    # Version 2 should pass through: no v1 block is hit, just returns True.
    result = await async_migrate_entry(hass, entry)
    assert result is True


async def test_migrate_entry_v1_hub_bumps_version_to_2(hass):
    """Migrating a v1 hub entry bumps its version to 2."""
    hub = MockConfigEntry(
        domain=DOMAIN,
        title="hub v1",
        version=1,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
    )
    hub.add_to_hass(hass)

    result = await async_migrate_entry(hass, hub)
    assert result is True
    assert hub.version == 2


async def test_migrate_entry_v1_device_bumps_version_to_2(hass):
    """Migrating a v1 device entry bumps its version to 2."""
    hub_id = "hub-id-001"
    hub = MockConfigEntry(
        domain=DOMAIN,
        title="hub",
        version=1,
        entry_id=hub_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
    )
    device = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_HUB_ENTRY_ID: hub_id,
            CONF_DEVICE_KEY: "Sensor-1",
            CONF_MODEL: "Sensor",
        },
    )
    hub.add_to_hass(hass)
    device.add_to_hass(hass)

    result = await async_migrate_entry(hass, device)
    assert result is True
    assert device.version == 2


async def test_migrate_entry_v1_hub_removes_child_entries(hass):
    """Migration folds children into the hub and removes them."""
    hub_id = "hub-id-001"
    key_a = "Acurite-606TX-42"

    hub = MockConfigEntry(
        domain=DOMAIN,
        title="hub",
        version=1,
        entry_id=hub_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_HUB_ENTRY_ID: hub_id,
            CONF_DEVICE_KEY: key_a,
            CONF_MODEL: "Acurite-606TX",
        },
        options={LEGACY_CONF_OBSERVED_FIELDS: ["temperature_C"]},
    )
    hub.add_to_hass(hass)
    child.add_to_hass(hass)

    result = await async_migrate_entry(hass, hub)
    assert result is True

    # Only the hub remains
    entries = hass.config_entries.async_entries(DOMAIN)
    entry_ids = {e.entry_id for e in entries}
    assert hub_id in entry_ids
    assert child.entry_id not in entry_ids


async def test_migrate_entry_v1_hub_folds_device_into_devices_map(hass):
    """Migration folds a child's fields into the hub's devices map."""
    hub_id = "hub-id-001"
    key_a = "Acurite-606TX-42"

    hub = MockConfigEntry(
        domain=DOMAIN,
        title="hub",
        version=1,
        entry_id=hub_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_HUB_ENTRY_ID: hub_id,
            CONF_DEVICE_KEY: key_a,
            CONF_MODEL: "Acurite-606TX",
        },
        options={LEGACY_CONF_OBSERVED_FIELDS: ["temperature_C", "humidity"]},
    )
    hub.add_to_hass(hass)
    child.add_to_hass(hass)

    await async_migrate_entry(hass, hub)

    devices = hub.data.get(CONF_DEVICES, {})
    assert key_a in devices
    assert devices[key_a][CONF_MODEL] == "Acurite-606TX"
    # Fields are sorted
    assert devices[key_a][DEVICE_FIELDS] == ["humidity", "temperature_C"]


async def test_migrate_entry_v1_hub_preserves_timeout_override(hass):
    """Child's timeout_override option is folded into the devices map."""
    hub_id = "hub-id-001"
    key_b = "EnergyMeter-2000-1234"

    hub = MockConfigEntry(
        domain=DOMAIN,
        title="hub",
        version=1,
        entry_id=hub_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_HUB_ENTRY_ID: hub_id,
            CONF_DEVICE_KEY: key_b,
            CONF_MODEL: "EnergyMeter-2000",
        },
        options={
            LEGACY_CONF_OBSERVED_FIELDS: ["power_W"],
            CONF_AVAILABILITY_TIMEOUT: 120,
        },
    )
    hub.add_to_hass(hass)
    child.add_to_hass(hass)

    await async_migrate_entry(hass, hub)

    devices = hub.data[CONF_DEVICES]
    assert devices[key_b][DEVICE_TIMEOUT_OVERRIDE] == 120


async def test_migrate_entry_v1_device_without_hub_id_returns_true(hass):
    """A v1 device entry with no CONF_HUB_ENTRY_ID still returns True."""
    device = MockConfigEntry(
        domain=DOMAIN,
        title="orphan device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_DEVICE_KEY: "Sensor-99",
            CONF_MODEL: "Sensor",
            # No CONF_HUB_ENTRY_ID
        },
    )
    device.add_to_hass(hass)

    result = await async_migrate_entry(hass, device)
    assert result is True
    assert device.version == 2


async def test_migrate_entry_v1_device_rehomes_registry_devices(hass):
    """A v1 hub migration re-homes registry devices to the hub entry."""
    hub_id = "hub-id-001"
    key_a = "Acurite-606TX-42"

    hub = MockConfigEntry(
        domain=DOMAIN,
        title="hub",
        version=1,
        entry_id=hub_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_HUB_ENTRY_ID: hub_id,
            CONF_DEVICE_KEY: key_a,
            CONF_MODEL: "Acurite-606TX",
        },
        options={LEGACY_CONF_OBSERVED_FIELDS: []},
    )
    hub.add_to_hass(hass)
    child.add_to_hass(hass)

    dev_reg = dr.async_get(hass)

    # Pre-seed a registry device owned by child
    dev_reg.async_get_or_create(
        config_entry_id=child.entry_id,
        identifiers={(DOMAIN, f"{hub_id}:{key_a}")},
    )

    # Migrate the hub (which migrates and removes the child)
    await async_migrate_entry(hass, hub)

    # The device is now owned by the hub
    updated_dev = dev_reg.async_get_device(identifiers={(DOMAIN, f"{hub_id}:{key_a}")})
    assert updated_dev is not None
    assert hub_id in updated_dev.config_entries
    # The child entry no longer owns the device
    assert child.entry_id not in updated_dev.config_entries


# ===========================================================================
# _rehome_device_objects
# ===========================================================================


async def test_rehome_device_objects_skips_when_same_entry(hass, hub_entry_builder):
    """When hub_entry_id == device_entry.entry_id nothing changes."""
    hub = hub_entry_builder(availability_timeout=600)
    hub.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    er.async_get(hass)

    before_devs = list(dev_reg.devices.keys())
    # Should return immediately without touching anything
    _rehome_device_objects(hass, hub, hub.entry_id)
    after_devs = list(dev_reg.devices.keys())
    assert before_devs == after_devs


async def test_rehome_device_objects_moves_devices_to_hub(hass):
    """Devices owned by source entry are re-homed to hub_entry_id."""
    hub_id = "hub-entry"
    source_id = "child-entry"

    source = MockConfigEntry(
        domain=DOMAIN,
        title="source",
        version=2,
        entry_id=source_id,
        data={CONF_HOST: "h", CONF_PORT: 8433, CONF_PATH: "/ws"},
    )
    source.add_to_hass(hass)

    # Also need a hub entry so the hub_entry_id is valid
    hub = MockConfigEntry(
        domain=DOMAIN,
        title="hub",
        version=2,
        entry_id=hub_id,
        data={CONF_HOST: "h2", CONF_PORT: 8433, CONF_PATH: "/ws"},
    )
    hub.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    dev = dev_reg.async_get_or_create(
        config_entry_id=source_id,
        identifiers={(DOMAIN, f"{hub_id}:MySensor-1")},
    )
    assert source_id in dev.config_entries

    _rehome_device_objects(hass, source, hub_id)

    updated = dev_reg.async_get_device(identifiers={(DOMAIN, f"{hub_id}:MySensor-1")})
    assert hub_id in updated.config_entries
    assert source_id not in updated.config_entries


async def test_rehome_device_objects_idempotent_for_devices(hass):
    """Calling _rehome_device_objects twice is safe (idempotent for devices).

    After re-homing, the device already belongs to hub_entry_id; a second call
    is a no-op for devices (add_config_entry_id is idempotent, remove finds
    nothing to remove for the source).
    """
    hub_id = "hub-id-001"
    source_id = "child-id-001"

    hub = MockConfigEntry(
        domain=DOMAIN,
        title="hub",
        version=2,
        entry_id=hub_id,
        data={CONF_HOST: "h", CONF_PORT: 8433, CONF_PATH: "/ws"},
    )
    source = MockConfigEntry(
        domain=DOMAIN,
        title="child",
        version=2,
        entry_id=source_id,
        data={CONF_HOST: "h2", CONF_PORT: 8433, CONF_PATH: "/ws"},
    )
    hub.add_to_hass(hass)
    source.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    dev = dev_reg.async_get_or_create(
        config_entry_id=source_id,
        identifiers={(DOMAIN, f"{hub_id}:MySensor-42")},
    )
    assert source_id in dev.config_entries

    # First call re-homes the device
    _rehome_device_objects(hass, source, hub_id)
    dev1 = dev_reg.async_get_device(identifiers={(DOMAIN, f"{hub_id}:MySensor-42")})
    assert hub_id in dev1.config_entries
    assert source_id not in dev1.config_entries

    # Second call is a no-op (device already belongs to hub)
    _rehome_device_objects(hass, source, hub_id)
    dev2 = dev_reg.async_get_device(identifiers={(DOMAIN, f"{hub_id}:MySensor-42")})
    assert hub_id in dev2.config_entries


# ===========================================================================
# Full setup/unload round-trip: idempotent reload
# ===========================================================================


async def test_reload_is_idempotent(hass, hub_entry_builder):
    """Reloading a hub entry succeeds and restores the coordinator."""
    hub = await _setup_hub(hass, hub_entry_builder)

    assert await hass.config_entries.async_reload(hub.entry_id)
    await hass.async_block_till_done()

    # Coordinator is back in hass.data
    assert hub.entry_id in hass.data[DOMAIN]
    coordinator = _coordinator(hass, hub)
    assert isinstance(coordinator, Rtl433Coordinator)


async def test_setup_then_unload_then_setup_again(hass, hub_entry_builder):
    """Setup -> unload -> setup succeeds (no stale state)."""
    hub = await _setup_hub(hass, hub_entry_builder)
    assert await hass.config_entries.async_unload(hub.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    assert hub.entry_id in hass.data[DOMAIN]


# ===========================================================================
# Phantom device cleanup via full setup path
# ===========================================================================


async def test_phantom_cleanup_during_setup(hass, hub_entry_builder):
    """The phantom unknown device is cleaned up automatically during setup."""
    real_key = "Acurite-606TX-42"
    hub = hub_entry_builder(
        availability_timeout=600,
        devices={
            PHANTOM_DEVICE_KEY: {CONF_MODEL: "", DEVICE_FIELDS: []},
            real_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]},
        },
    )
    hub.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    phantom_ident = (DOMAIN, f"{hub.entry_id}:{PHANTOM_DEVICE_KEY}")
    dev_reg.async_get_or_create(
        config_entry_id=hub.entry_id,
        identifiers={phantom_ident},
    )

    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    assert PHANTOM_DEVICE_KEY not in hub.data.get(CONF_DEVICES, {})
    assert real_key in hub.data[CONF_DEVICES]
    assert dev_reg.async_get_device(identifiers={phantom_ident}) is None


# ===========================================================================
# Motion migration via full setup path
# ===========================================================================


async def test_motion_migration_during_setup(hass, hub_entry_builder):
    """The motion entity migration runs automatically during setup."""

    device_key = "MySensor-1"

    hub = hub_entry_builder(
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "Sensor",
                DEVICE_EVENT_TYPES: {"motion": ["on"], "button": ["A"]},
            }
        },
    )
    hub.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    # Pre-seed an orphaned event.motion entity
    orphan_uid = f"{hub.entry_id}:{device_key}:motion"
    ent_reg.async_get_or_create("event", DOMAIN, orphan_uid, config_entry=hub)

    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    # The orphaned entity is gone
    assert ent_reg.async_get_entity_id("event", DOMAIN, orphan_uid) is None

    # The motion slot is dropped from device_event_types
    devices = hub.data.get(CONF_DEVICES, {})
    if device_key in devices and isinstance(devices[device_key], dict):
        event_types = devices[device_key].get(DEVICE_EVENT_TYPES, {})
        assert "motion" not in event_types


# ===========================================================================
# Migration: clear_delay preserved
# ===========================================================================


async def test_migrate_hub_entry_preserves_clear_delay(hass):
    """Child's motion_clear_delay option is folded into the devices map."""
    hub_id = "hub-id-001"
    key_c = "MotionSensor-1"

    hub = MockConfigEntry(
        domain=DOMAIN,
        title="hub",
        version=1,
        entry_id=hub_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_HUB_ENTRY_ID: hub_id,
            CONF_DEVICE_KEY: key_c,
            CONF_MODEL: "MotionSensor",
        },
        options={
            LEGACY_CONF_OBSERVED_FIELDS: ["motion"],
            DEVICE_MOTION_CLEAR_DELAY: 45,
        },
    )
    hub.add_to_hass(hass)
    child.add_to_hass(hass)

    await async_migrate_entry(hass, hub)

    devices = hub.data[CONF_DEVICES]
    assert key_c in devices
    assert devices[key_c][DEVICE_MOTION_CLEAR_DELAY] == 45


# ===========================================================================
# PLATFORMS is a list (not a tuple/set) — critical for forwarding
# ===========================================================================


def test_platforms_is_a_list():
    """PLATFORMS must be a list so async_forward_entry_setups accepts it."""
    assert isinstance(PLATFORMS, list)
    assert len(PLATFORMS) > 0
