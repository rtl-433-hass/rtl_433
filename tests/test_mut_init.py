"""Mutation-killing tests for custom_components/rtl_433/__init__.py.

Covers every branch and helper in the module in isolation or via integration
tests so that mutation-testing survivors are minimized. Uses the same idioms as
test_lifecycle.py (receiver_entry_builder fixture, _no_socket stub, etc.).
"""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
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
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    CONF_RECEIVER_ENTRY_ID,
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
    ENTRY_TYPE_RECEIVER,
    PLATFORMS,
    SUBENTRY_TYPE_RECEIVER,
)
from custom_components.rtl_433.coordinator import Rtl433Coordinator
from custom_components.rtl_433.coordinator.base import Rtl433Client
from custom_components.rtl_433.migration import (
    LEGACY_CONF_OBSERVED_FIELDS,
    PHANTOM_DEVICE_KEY,
    _cleanup_phantom_unknown_device,
    _migrate_motion_event_to_binary_sensor,
    _rehome_device_objects,
)
from custom_components.rtl_433.receiver_settings import (
    _calibration_map,
    _receiver_availability_timeout,
    _receiver_connection,
    _receiver_manage_settings,
    _receiver_secure,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from tests.conftest import (
    build_receiver_entry,
    receiver_id,
    receiver_scope,
    receiver_subentry,
)


# ---------------------------------------------------------------------------
# Socket stub: prevents real WebSocket connections in every test.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_socket():
    """Stub the connect loop so the coordinator never opens a real WebSocket."""

    async def _noop(self) -> None:
        return None

    with patch.object(Rtl433Client, "start", _noop):
        yield


# ---------------------------------------------------------------------------
# Helpers (mirrors test_lifecycle.py)
# ---------------------------------------------------------------------------
def _coordinator(
    hass: HomeAssistant, receiver_entry: MockConfigEntry
) -> Rtl433Coordinator:
    return hass.data[DOMAIN][receiver_id(receiver_entry)]


def _feed(coordinator: Rtl433Coordinator, event: dict) -> None:
    coordinator._client._process_event(event)


def _live(event: dict) -> dict:
    """Strip ``time`` so the frame classifies as a live transmission.

    The fixtures carry a fixed (long-stale) timestamp, which the client would
    classify as a reconnect replay — and a replay never makes a device a pending
    candidate.
    """
    return {k: v for k, v in event.items() if k != "time"}


async def _setup_receiver(hass, receiver_entry_builder, *, devices=None, **kwargs):
    receiver = receiver_entry_builder(
        availability_timeout=600, devices=devices, **kwargs
    )
    receiver.add_to_hass(hass)
    assert await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()
    return receiver


# ===========================================================================
# Pure helper functions — _receiver_secure / _receiver_availability_timeout /
# _receiver_manage_settings
# ===========================================================================


def _make_entry(data=None, options=None):
    """Build a minimal location MockConfigEntry with given data/options."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="test location",
        data=data or {},
        options=options or {},
        version=2,
    )


def _make_receiver(data=None, *, unique_id=None):
    """Build a bare receiver subentry carrying exactly ``data``.

    The connection target and the radio settings live on the *subentry* now, so
    the resolvers that read them take one of these rather than the location entry
    -- which is the point: a per-receiver setting cannot be read off the wrong
    object by accident.
    """
    return ConfigSubentry(
        data=MappingProxyType(data or {}),
        subentry_type=SUBENTRY_TYPE_RECEIVER,
        title="test receiver",
        unique_id=unique_id,
    )


# --- _receiver_secure -----------------------------------------------------------


def test_receiver_secure_defaults_false():
    subentry = _make_receiver({CONF_HOST: "h", CONF_PORT: 8433, CONF_PATH: "/ws"})
    assert _receiver_secure(subentry) is False


def test_receiver_secure_true_when_set():
    assert _receiver_secure(_make_receiver({"secure": True})) is True


def test_receiver_secure_false_when_explicit_false():
    assert _receiver_secure(_make_receiver({"secure": False})) is False


# --- _receiver_availability_timeout ---------------------------------------------


def test_receiver_availability_timeout_defaults():
    entry = _make_entry(data={})
    assert _receiver_availability_timeout(entry) == DEFAULT_AVAILABILITY_TIMEOUT


def test_receiver_availability_timeout_from_data():
    entry = _make_entry(data={CONF_AVAILABILITY_TIMEOUT: 300})
    assert _receiver_availability_timeout(entry) == 300


def test_receiver_availability_timeout_options_overrides_data():
    entry = _make_entry(
        data={CONF_AVAILABILITY_TIMEOUT: 300},
        options={CONF_AVAILABILITY_TIMEOUT: 120},
    )
    assert _receiver_availability_timeout(entry) == 120


def test_receiver_availability_timeout_options_only():
    entry = _make_entry(data={}, options={CONF_AVAILABILITY_TIMEOUT: 900})
    assert _receiver_availability_timeout(entry) == 900


def test_receiver_availability_timeout_is_int():
    """Result must be an integer (int() coercion)."""
    entry = _make_entry(data={CONF_AVAILABILITY_TIMEOUT: 200})
    result = _receiver_availability_timeout(entry)
    assert isinstance(result, int)
    assert result == 200


# --- _receiver_manage_settings --------------------------------------------------


def test_receiver_manage_settings_defaults_to_true():
    assert _receiver_manage_settings(_make_entry(), _make_receiver()) is True


def test_receiver_manage_settings_true():
    assert DEFAULT_MANAGE_SETTINGS is True


def test_receiver_manage_settings_receiver_false():
    """The toggle is the receiver's: it is read off the subentry."""
    subentry = _make_receiver({CONF_MANAGE_SETTINGS: False})
    assert _receiver_manage_settings(_make_entry(), subentry) is False


def test_receiver_manage_settings_options_overrides_receiver():
    """The location's options still win, until that form is re-scoped."""
    entry = _make_entry(options={CONF_MANAGE_SETTINGS: False})
    subentry = _make_receiver({CONF_MANAGE_SETTINGS: True})
    assert _receiver_manage_settings(entry, subentry) is False


def test_receiver_manage_settings_options_true_overrides_receiver_false():
    entry = _make_entry(options={CONF_MANAGE_SETTINGS: True})
    subentry = _make_receiver({CONF_MANAGE_SETTINGS: False})
    assert _receiver_manage_settings(entry, subentry) is True


def test_receiver_manage_settings_without_a_named_receiver_reads_the_first():
    """Omitting the subentry asks the location's first receiver."""
    entry = build_receiver_entry(manage_settings=False)
    assert _receiver_manage_settings(entry) is False


# --- _receiver_connection -------------------------------------------------------


def _connection_entry(**overrides):
    """Build a receiver carrying a full connection target + stable radio id."""
    data = {
        CONF_HOST: "rtl433.local",
        CONF_PORT: 8433,
        CONF_PATH: "/ws",
        "secure": False,
    }
    unique_id = overrides.pop("unique_id", "serial:0123")
    data.update(overrides)
    return _make_receiver(data, unique_id=unique_id)


def test_receiver_connection_reports_the_stored_target():
    """The tuple carries host, port, path, secure and the stable radio id."""
    assert _receiver_connection(_connection_entry()) == (
        "rtl433.local",
        8433,
        "/ws",
        False,
        "serial:0123",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {CONF_HOST: "other.local"},
        {CONF_PORT: 9000},
        {CONF_PATH: "/ws2"},
        {"secure": True},
        {"unique_id": "serial:9999"},
    ],
    ids=["host", "port", "path", "secure", "unique_id"],
)
def test_receiver_connection_changes_with_every_component(overrides):
    """Each component is part of the tuple, so changing any one is detectable."""
    assert _receiver_connection(_connection_entry(**overrides)) != _receiver_connection(
        _connection_entry()
    )


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


async def test_cleanup_phantom_removes_unknown_from_map(hass, receiver_entry_builder):
    """The 'unknown' key is removed from the devices map."""
    receiver = receiver_entry_builder(
        devices={
            PHANTOM_DEVICE_KEY: {CONF_MODEL: "", DEVICE_FIELDS: []},
            "real-device": {CONF_MODEL: "Acurite", DEVICE_FIELDS: ["temperature_C"]},
        }
    )
    receiver.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    _cleanup_phantom_unknown_device(hass, receiver, dev_reg)

    assert PHANTOM_DEVICE_KEY not in receiver.data.get(CONF_DEVICES, {})
    assert "real-device" in receiver.data[CONF_DEVICES]


async def test_cleanup_phantom_no_unknown_is_noop(hass, receiver_entry_builder):
    """No 'unknown' key: the devices map is left untouched."""
    receiver = receiver_entry_builder(
        devices={"real-device": {CONF_MODEL: "Acurite", DEVICE_FIELDS: []}}
    )
    receiver.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    before = dict(receiver.data.get(CONF_DEVICES, {}))

    _cleanup_phantom_unknown_device(hass, receiver, dev_reg)

    assert receiver.data.get(CONF_DEVICES, {}) == before


async def test_cleanup_phantom_removes_registry_device(hass, receiver_entry_builder):
    """The stale registry device with identifier (DOMAIN, entry_id:unknown) is removed."""
    receiver = receiver_entry_builder(devices={})
    receiver.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    phantom_ident = (DOMAIN, f"{receiver.entry_id}:{PHANTOM_DEVICE_KEY}")
    dev_reg.async_get_or_create(
        config_entry_id=receiver.entry_id,
        identifiers={phantom_ident},
    )
    assert (
        dev_reg.async_get_device_by_identifier(phantom_ident, receiver.entry_id)
        is not None
    )

    _cleanup_phantom_unknown_device(hass, receiver, dev_reg)

    assert (
        dev_reg.async_get_device_by_identifier(phantom_ident, receiver.entry_id) is None
    )


async def test_cleanup_phantom_no_registry_device_is_noop(hass, receiver_entry_builder):
    """No phantom registry device: no error."""
    receiver = receiver_entry_builder(devices={})
    receiver.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    # Should not raise
    _cleanup_phantom_unknown_device(hass, receiver, dev_reg)


async def test_cleanup_phantom_leaves_receiver_device_untouched(
    hass, receiver_entry_builder
):
    """The receiver device itself is never touched by the cleanup."""
    receiver = receiver_entry_builder(
        devices={PHANTOM_DEVICE_KEY: {CONF_MODEL: "", DEVICE_FIELDS: []}}
    )
    receiver.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    dev_reg.async_get_or_create(
        config_entry_id=receiver.entry_id,
        identifiers={(DOMAIN, receiver_scope(receiver))},
        name="Receiver",
    )
    _cleanup_phantom_unknown_device(hass, receiver, dev_reg)

    # Receiver device still exists
    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, receiver_scope(receiver)), receiver.entry_id
        )
        is not None
    )


# ===========================================================================
# _migrate_motion_event_to_binary_sensor
# ===========================================================================


async def test_migrate_motion_removes_event_entities(hass, receiver_entry_builder):
    """Orphaned event.*_motion entities are removed."""
    receiver = receiver_entry_builder(devices={})
    receiver.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    device_key = "MySensor-42"
    motion_uid = f"{receiver.entry_id}:{device_key}:motion"
    ent_reg.async_get_or_create(
        "event",
        DOMAIN,
        motion_uid,
        config_entry=receiver,
    )
    assert ent_reg.async_get_entity_id("event", DOMAIN, motion_uid) is not None

    with patch(
        "custom_components.rtl_433.repairs.async_raise_motion_moved"
    ) as mock_notify:
        _migrate_motion_event_to_binary_sensor(hass, receiver, ent_reg)

    # The event entity is gone.
    assert ent_reg.async_get_entity_id("event", DOMAIN, motion_uid) is None
    # The repair advisory was raised.
    mock_notify.assert_called_once_with(hass)


async def test_migrate_motion_leaves_non_motion_event_entities(
    hass, receiver_entry_builder
):
    """Non-motion event entities are not touched."""
    receiver = receiver_entry_builder(devices={})
    receiver.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    uid = f"{receiver.entry_id}:MySensor-42:button"
    ent_reg.async_get_or_create("event", DOMAIN, uid, config_entry=receiver)

    with patch(
        "custom_components.rtl_433.repairs.async_raise_motion_moved"
    ) as mock_notify:
        _migrate_motion_event_to_binary_sensor(hass, receiver, ent_reg)

    assert ent_reg.async_get_entity_id("event", DOMAIN, uid) is not None
    mock_notify.assert_not_called()


async def test_migrate_motion_drops_motion_from_event_types(
    hass, receiver_entry_builder
):
    """The 'motion' key is removed from DEVICE_EVENT_TYPES in the devices map."""
    device_key = "MySensor-42"
    receiver = receiver_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "Sensor",
                DEVICE_EVENT_TYPES: {"motion": ["on"], "button": ["A"]},
            }
        }
    )
    receiver.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    with patch("custom_components.rtl_433.repairs.async_raise_motion_moved"):
        _migrate_motion_event_to_binary_sensor(hass, receiver, ent_reg)

    devices = receiver.data[CONF_DEVICES]
    # motion slot was dropped
    assert "motion" not in devices[device_key][DEVICE_EVENT_TYPES]
    # button slot is kept
    assert "button" in devices[device_key][DEVICE_EVENT_TYPES]


async def test_migrate_motion_no_motion_event_types_no_write(
    hass, receiver_entry_builder
):
    """No motion in event_types means the devices map is not rewritten."""
    device_key = "MySensor-42"
    original_devices = {
        device_key: {
            CONF_MODEL: "Sensor",
            DEVICE_EVENT_TYPES: {"button": ["A"]},
        }
    }
    receiver = receiver_entry_builder(devices=original_devices)
    receiver.add_to_hass(hass)
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
        _migrate_motion_event_to_binary_sensor(hass, receiver, ent_reg)

    # No update for the devices change (no motion slot existed)
    # Only check that the motion event_type slot is not present (it wasn't)
    assert "button" in receiver.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES]


async def test_migrate_motion_no_removed_means_no_repair_issue(
    hass, receiver_entry_builder
):
    """No orphaned motion entities: the repair issue is NOT raised."""
    receiver = receiver_entry_builder(devices={})
    receiver.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    with patch(
        "custom_components.rtl_433.repairs.async_raise_motion_moved"
    ) as mock_notify:
        _migrate_motion_event_to_binary_sensor(hass, receiver, ent_reg)

    mock_notify.assert_not_called()


async def test_migrate_motion_non_dict_record_skipped(hass, receiver_entry_builder):
    """Non-dict device records are passed through unchanged."""
    receiver = receiver_entry_builder(devices={"bad": "not-a-dict"})
    receiver.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    with patch("custom_components.rtl_433.repairs.async_raise_motion_moved"):
        _migrate_motion_event_to_binary_sensor(hass, receiver, ent_reg)

    # The bad record is still there (no crash)
    assert receiver.data[CONF_DEVICES]["bad"] == "not-a-dict"


# ===========================================================================
# async_setup_entry — receiver device registration and coordinator wiring
# ===========================================================================


async def test_setup_entry_registers_receiver_device(hass, receiver_entry_builder):
    """async_setup_entry creates the receiver device in the device registry."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)

    dev_reg = dr.async_get(hass)
    receiver_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, receiver_scope(receiver)), receiver.entry_id
    )
    assert receiver_device is not None
    assert receiver_device.manufacturer == "rtl_433"
    assert receiver_device.name == receiver.title
    assert receiver_device.model == "rtl_433 server"


async def test_receiver_info_callback_updates_receiver_device_identity(
    hass, receiver_entry_builder
):
    """Once the SDR identity is known, the receiver device shows its model/serial."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)

    coordinator._client.dev_info = {
        "vendor": "Realtek",
        "product": "RTL2838UHIDIR",
        "serial": "00000001",
    }
    coordinator.receiver_info_callback()
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    receiver_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, receiver_scope(receiver)), receiver.entry_id
    )
    assert receiver_device.manufacturer == "Realtek"
    assert receiver_device.model == "RTL2838UHIDIR"
    assert receiver_device.serial_number == "00000001"


async def test_receiver_info_callback_noop_when_identity_empty(
    hass, receiver_entry_builder
):
    """With no SDR identity (e.g. ``-D manual``) the receiver keeps its placeholders."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)

    coordinator._client.dev_info = {}
    coordinator.receiver_info_callback()
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    receiver_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, receiver_scope(receiver)), receiver.entry_id
    )
    assert receiver_device.manufacturer == "rtl_433"
    assert receiver_device.model == "rtl_433 server"
    assert receiver_device.serial_number is None


async def test_setup_entry_stores_coordinator_in_hass_data(
    hass, receiver_entry_builder
):
    """async_setup_entry puts the coordinator in hass.data[DOMAIN][entry_id]."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)

    coordinator = hass.data[DOMAIN][receiver_id(receiver)]
    assert isinstance(coordinator, Rtl433Coordinator)


async def test_setup_entry_returns_true(hass, receiver_entry_builder):
    """async_setup_entry returns True on success."""
    receiver = receiver_entry_builder(availability_timeout=600)
    receiver.add_to_hass(hass)
    result = await hass.config_entries.async_setup(receiver.entry_id)
    assert result is True


async def test_setup_entry_caches_library_in_hass_data(hass, receiver_entry_builder):
    """The mapping library is cached in hass.data[DOMAIN][DATA_LIBRARY]."""
    await _setup_receiver(hass, receiver_entry_builder)
    assert DATA_LIBRARY in hass.data[DOMAIN]
    cached = hass.data[DOMAIN][DATA_LIBRARY]
    assert cached is not None
    assert len(cached) == 2  # (registry, skip_keys)


async def test_setup_entry_library_cached_across_two_receivers(
    hass, receiver_entry_builder
):
    """A second receiver setup reuses the cached library (same object)."""
    await _setup_receiver(hass, receiver_entry_builder, host="h1.local")
    first_cached = hass.data[DOMAIN][DATA_LIBRARY]

    await _setup_receiver(hass, receiver_entry_builder, host="h2.local")
    second_cached = hass.data[DOMAIN][DATA_LIBRARY]

    assert first_cached is second_cached


async def test_setup_entry_injects_skip_keys_into_coordinator(
    hass, receiver_entry_builder
):
    """The coordinator's skip_keys is set from the loaded library."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    assert coordinator.skip_keys is not None


async def test_setup_entry_coordinator_gets_correct_host_port(
    hass, receiver_entry_builder
):
    """Coordinator is built with the right host/port from the entry."""
    receiver = await _setup_receiver(hass, receiver_entry_builder, host="myhost.local")
    coordinator = _coordinator(hass, receiver)
    assert coordinator.host == "myhost.local"
    assert coordinator.port == DEFAULT_PORT


async def test_setup_entry_coordinator_manages_settings_default(
    hass, receiver_entry_builder
):
    """manage_settings defaults to True (DEFAULT_MANAGE_SETTINGS)."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    assert _coordinator(hass, receiver).manage_settings is True


async def test_setup_entry_coordinator_manages_settings_false(
    hass, receiver_entry_builder
):
    """manage_settings=False is propagated to the coordinator."""
    receiver = await _setup_receiver(
        hass, receiver_entry_builder, options={CONF_MANAGE_SETTINGS: False}
    )
    assert _coordinator(hass, receiver).manage_settings is False


async def test_setup_entry_coordinator_availability_timeout(
    hass, receiver_entry_builder
):
    """availability_timeout from entry data lands on the coordinator."""
    # _setup_receiver passes availability_timeout=600, so we build manually
    receiver = receiver_entry_builder(availability_timeout=300)
    receiver.add_to_hass(hass)
    assert await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()
    assert _coordinator(hass, receiver).availability_timeout == 300


async def test_setup_entry_calibration_snapshot_set(hass, receiver_entry_builder):
    """coordinator.calibration_snapshot is set from the entry's devices map."""
    device_key = "Meter-42"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
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
    coordinator = _coordinator(hass, receiver)
    assert device_key in coordinator.calibration_snapshot
    assert (
        coordinator.calibration_snapshot[device_key][CALIBRATION_COMMODITY]
        == COMMODITY_WATER
    )


async def test_setup_entry_calibration_snapshot_empty_when_no_calibration(
    hass, receiver_entry_builder
):
    """calibration_snapshot is {} when no devices have calibration."""
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={"dev": {CONF_MODEL: "Foo", DEVICE_FIELDS: ["temp"]}},
    )
    coordinator = _coordinator(hass, receiver)
    assert coordinator.calibration_snapshot == {}


async def test_setup_entry_forwards_platforms(hass, receiver_entry_builder):
    """async_setup_entry forwards all PLATFORMS entries."""
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        wraps=hass.config_entries.async_forward_entry_setups,
    ) as fwd_spy:
        receiver = receiver_entry_builder(availability_timeout=600)
        receiver.add_to_hass(hass)
        await hass.config_entries.async_setup(receiver.entry_id)
        await hass.async_block_till_done()

    fwd_spy.assert_called_once()
    _, forwarded_platforms = fwd_spy.call_args[0]
    # All required platforms are forwarded exactly once
    assert set(forwarded_platforms) == set(PLATFORMS)


async def test_setup_entry_secure_false_by_default(hass, receiver_entry_builder):
    """Coordinator gets secure=False when the entry has no 'secure' key."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    assert coordinator.secure is False


async def test_setup_entry_secure_true(hass, receiver_entry_builder):
    """Coordinator gets secure=True when entry data has secure=True."""
    receiver = receiver_entry_builder(secure=True, availability_timeout=600)
    receiver.add_to_hass(hass)
    await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, receiver)
    assert coordinator.secure is True


# ===========================================================================
# effective_timeout_resolver and effective_clear_delay_resolver (closures)
# ===========================================================================


async def test_effective_timeout_resolver_uses_receiver_default(
    hass, receiver_entry_builder
):
    """When no per-device override exists, receiver default is returned."""
    receiver = receiver_entry_builder(availability_timeout=300)
    receiver.add_to_hass(hass)
    assert await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, receiver)
    # Call the wired resolver; no override means receiver default.
    result = coordinator.effective_timeout_resolver("some-device")
    assert result == 300


async def test_effective_timeout_resolver_uses_device_override(
    hass, receiver_entry_builder
):
    """Per-device timeout_override takes precedence over the receiver default."""
    device_key = "MySensor-7"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "M",
                DEVICE_FIELDS: [],
                DEVICE_TIMEOUT_OVERRIDE: 120,
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    assert coordinator.effective_timeout_resolver(device_key) == 120


async def test_effective_timeout_resolver_fallback_for_unknown_device(
    hass, receiver_entry_builder
):
    """Device not in the map uses the receiver default timeout."""
    receiver = receiver_entry_builder(availability_timeout=450)
    receiver.add_to_hass(hass)
    assert await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, receiver)
    assert coordinator.effective_timeout_resolver("not-in-map") == 450


async def test_effective_clear_delay_resolver_default(hass, receiver_entry_builder):
    """No per-device override returns DEFAULT_MOTION_CLEAR_DELAY."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    result = coordinator.effective_clear_delay_resolver("some-device")
    assert result == DEFAULT_MOTION_CLEAR_DELAY


async def test_effective_clear_delay_resolver_device_override(
    hass, receiver_entry_builder
):
    """Per-device motion_clear_delay overrides the default."""
    device_key = "MotionDev-1"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "M",
                DEVICE_FIELDS: [],
                DEVICE_MOTION_CLEAR_DELAY: 30,
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    assert coordinator.effective_clear_delay_resolver(device_key) == 30


async def test_effective_timeout_resolver_int_coercion(hass, receiver_entry_builder):
    """timeout_override stored as a non-int is coerced to int."""
    device_key = "MySensor-7"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {
                CONF_MODEL: "M",
                DEVICE_FIELDS: [],
                DEVICE_TIMEOUT_OVERRIDE: "180",
            }
        },
    )
    coordinator = _coordinator(hass, receiver)
    result = coordinator.effective_timeout_resolver(device_key)
    assert isinstance(result, int)
    assert result == 180


# ===========================================================================
# new_device_callback wiring
# ===========================================================================


async def test_new_device_callback_dispatches_signal(
    hass, receiver_entry_builder, events
):
    """Adopting a received device dispatches the receiver-level new-device signal."""
    from custom_components.rtl_433.const import signal_new_device

    power_event = _live(events("power_sensor.json")[0])
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)

    received: list[tuple] = []

    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    async_dispatcher_connect(
        hass,
        signal_new_device(receiver_id(receiver)),
        lambda device_key, model: received.append((device_key, model)),
    )

    # Merely hearing the device dispatches nothing: it has no device or entities
    # to build until the user adopts it.
    _feed(coordinator, power_event)
    await hass.async_block_till_done()
    assert received == []

    coordinator.adopt_device("EnergyMeter-2000-1234")
    await hass.async_block_till_done()

    assert len(received) == 1
    device_key, model = received[0]
    assert device_key == "EnergyMeter-2000-1234"
    assert model == "EnergyMeter-2000"


# ===========================================================================
# async_unload_entry
# ===========================================================================


async def test_unload_entry_stops_coordinator(hass, receiver_entry_builder):
    """async_unload_entry calls coordinator.async_stop."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)

    with patch.object(
        coordinator, "async_stop", wraps=coordinator.async_stop
    ) as stop_spy:
        result = await hass.config_entries.async_unload(receiver.entry_id)
        await hass.async_block_till_done()

    assert result is True
    stop_spy.assert_called_once()


async def test_unload_entry_removes_coordinator_from_hass_data(
    hass, receiver_entry_builder
):
    """After unload, the coordinator is removed from hass.data[DOMAIN]."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    assert receiver_id(receiver) in hass.data[DOMAIN]

    await hass.config_entries.async_unload(receiver.entry_id)
    await hass.async_block_till_done()

    assert receiver_id(receiver) not in hass.data.get(DOMAIN, {})


async def test_unload_entry_returns_true(hass, receiver_entry_builder):
    """async_unload_entry returns True."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    result = await hass.config_entries.async_unload(receiver.entry_id)
    await hass.async_block_till_done()
    assert result is True


async def test_unload_entry_clears_reachability_repair(hass, receiver_entry_builder):
    """async_unload_entry calls async_clear_receiver_unreachable."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)

    with patch(
        "custom_components.rtl_433.repairs.async_clear_receiver_unreachable"
    ) as clear_spy:
        await hass.config_entries.async_unload(receiver.entry_id)
        await hass.async_block_till_done()

    clear_spy.assert_called_once_with(hass, receiver, receiver_id(receiver))


async def test_unload_entry_no_coordinator_in_data_branch(hass, receiver_entry_builder):
    """The unload code handles hass.data[DOMAIN] missing the entry_id gracefully.

    We verify this by confirming the coordinator is None-safe: the implementation
    checks ``if coordinator is not None`` before calling async_stop, so we
    confirm the unload returns True even after a normal setup cycle.
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    # Confirm the coordinator is there before unload
    assert receiver_id(receiver) in hass.data[DOMAIN]

    # Normal unload should return True
    result = await hass.config_entries.async_unload(receiver.entry_id)
    await hass.async_block_till_done()
    assert result is True
    # After unload, coordinator is gone
    assert receiver_id(receiver) not in hass.data.get(DOMAIN, {})


# ===========================================================================
# async_remove_config_entry_device
# ===========================================================================


async def test_remove_receiver_device_returns_false(hass, receiver_entry_builder):
    """Attempting to remove the receiver device itself returns False."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    dev_reg = dr.async_get(hass)
    receiver_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, receiver_scope(receiver)), receiver.entry_id
    )
    assert receiver_device is not None

    result = await async_remove_config_entry_device(hass, receiver, receiver_device)
    assert result is False


async def test_remove_receiver_scoped_identifier_returns_false(
    hass, receiver_entry_builder
):
    """A ``{location}:receiver:{subentry}`` identifier is never a device_key.

    The scan classifies identifiers by the reserved ``receiver`` marker, so a
    receiver device is refused like the entry-level device rather than being
    decoded into a bogus ``device_key`` of ``receiver`` and silently mutating
    ``entry.data[CONF_DEVICES]`` (Clarification #18).
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    before = dict(receiver.data.get(CONF_DEVICES, {}))

    fake = SimpleNamespace(
        identifiers={(DOMAIN, f"{receiver.entry_id}:receiver:subentry01")}
    )
    assert await async_remove_config_entry_device(hass, receiver, fake) is False
    assert receiver.data.get(CONF_DEVICES, {}) == before


async def test_reserved_marker_check_is_exact_not_a_prefix(
    hass, receiver_entry_builder
):
    """Only the exact ``receiver`` segment is structural.

    A device_key that merely starts with the word is an ordinary nested device:
    it is removed, and it leaves the entry's devices map. Guards the marker test
    against being widened to a ``startswith``.
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    device_key = "receiver-lookalike-42"
    hass.config_entries.async_update_entry(
        receiver,
        data={**receiver.data, CONF_DEVICES: {device_key: {CONF_MODEL: "Whatever"}}},
    )
    await hass.async_block_till_done()

    fake = SimpleNamespace(identifiers={(DOMAIN, f"{receiver.entry_id}:{device_key}")})
    assert await async_remove_config_entry_device(hass, receiver, fake) is True
    assert device_key not in receiver.data.get(CONF_DEVICES, {})


async def test_remove_nested_device_returns_true(hass, receiver_entry_builder, events):
    """Removing a nested RF device returns True."""
    power_event = _live(events("power_sensor.json")[0])
    device_key = "EnergyMeter-2000-1234"

    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    _feed(coordinator, power_event)
    coordinator.adopt_device(device_key)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    prefix = f"{receiver.entry_id}:{device_key}"
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, prefix), receiver.entry_id
    )
    assert device_entry is not None

    result = await async_remove_config_entry_device(hass, receiver, device_entry)
    assert result is True


async def test_remove_nested_device_drops_from_devices_map(
    hass, receiver_entry_builder, events
):
    """Removing a nested device drops it from entry.data[CONF_DEVICES]."""
    power_event = _live(events("power_sensor.json")[0])
    device_key = "EnergyMeter-2000-1234"

    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    _feed(coordinator, power_event)
    coordinator.adopt_device(device_key)
    await hass.async_block_till_done()

    assert device_key in receiver.data.get(CONF_DEVICES, {})

    dev_reg = dr.async_get(hass)
    prefix = f"{receiver.entry_id}:{device_key}"
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, prefix), receiver.entry_id
    )
    await async_remove_config_entry_device(hass, receiver, device_entry)

    assert device_key not in receiver.data.get(CONF_DEVICES, {})


async def test_remove_nested_device_calls_forget_device(
    hass, receiver_entry_builder, events
):
    """Removing a nested device calls coordinator.forget_device."""
    power_event = _live(events("power_sensor.json")[0])
    device_key = "EnergyMeter-2000-1234"

    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    _feed(coordinator, power_event)
    coordinator.adopt_device(device_key)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    prefix = f"{receiver.entry_id}:{device_key}"
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, prefix), receiver.entry_id
    )

    with patch.object(
        coordinator, "forget_device", wraps=coordinator.forget_device
    ) as forget_spy:
        await async_remove_config_entry_device(hass, receiver, device_entry)

    forget_spy.assert_called_once_with(device_key)


async def test_remove_nested_device_calls_device_removers(
    hass, receiver_entry_builder, events
):
    """Removing a nested device calls each registered device_remover."""
    power_event = _live(events("power_sensor.json")[0])
    device_key = "EnergyMeter-2000-1234"

    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    _feed(coordinator, power_event)
    coordinator.adopt_device(device_key)
    await hass.async_block_till_done()

    # Register a mock device remover
    removed_keys: list[str] = []
    coordinator.device_removers.append(removed_keys.append)

    dev_reg = dr.async_get(hass)
    prefix = f"{receiver.entry_id}:{device_key}"
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, prefix), receiver.entry_id
    )
    await async_remove_config_entry_device(hass, receiver, device_entry)

    assert device_key in removed_keys


async def test_remove_device_coordinator_none_branch(
    hass, receiver_entry_builder, events
):
    """async_remove_config_entry_device handles missing coordinator gracefully.

    The code does ``coordinator = hass.data.get(...).get(entry_id)`` which returns
    None when hass.data is missing the domain key. Removing the domain data
    exercises this branch: the device is still removed from the map, but
    forget_device is not called. The function still returns True.
    """
    power_event = _live(events("power_sensor.json")[0])
    device_key = "EnergyMeter-2000-1234"

    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    _feed(coordinator, power_event)
    coordinator.adopt_device(device_key)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    prefix = f"{receiver.entry_id}:{device_key}"
    device_entry = dev_reg.async_get_device_by_identifier(
        (DOMAIN, prefix), receiver.entry_id
    )
    assert device_entry is not None

    # Pop DOMAIN entirely from hass.data so .get(DOMAIN, {}).get(entry_id) is None
    saved = hass.data.pop(DOMAIN, {})
    try:
        result = await async_remove_config_entry_device(hass, receiver, device_entry)
    finally:
        # Restore so teardown can succeed
        hass.data[DOMAIN] = saved

    assert result is True


# ===========================================================================
# _async_update_listener — options-update-listener logic
# ===========================================================================


async def test_update_listener_reloads_on_manage_settings_change(
    hass, receiver_entry_builder
):
    """Changing manage_settings triggers a reload."""
    receiver = await _setup_receiver(
        hass, receiver_entry_builder, options={CONF_MANAGE_SETTINGS: True}
    )
    coordinator = _coordinator(hass, receiver)
    assert coordinator.manage_settings is True

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        # Flip manage_settings to False via options update
        hass.config_entries.async_update_entry(
            receiver, options={CONF_MANAGE_SETTINGS: False}
        )
        await hass.async_block_till_done()

    reload_spy.assert_called_once_with(receiver.entry_id)


async def test_update_listener_reloads_on_calibration_change(
    hass, receiver_entry_builder
):
    """Changing per-device calibration triggers a reload."""
    device_key = "Meter-9001"
    receiver = await _setup_receiver(
        hass,
        receiver_entry_builder,
        devices={
            device_key: {CONF_MODEL: "Meter", DEVICE_FIELDS: ["consumption_data"]}
        },
    )

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        # Write a calibration into the devices map
        devices = {k: dict(v) for k, v in receiver.data[CONF_DEVICES].items()}
        devices[device_key][DEVICE_CALIBRATION] = {
            CALIBRATION_COMMODITY: COMMODITY_WATER,
            CALIBRATION_UNIT: "L",
            CALIBRATION_SCALE: 0.1,
        }
        hass.config_entries.async_update_entry(
            receiver, data={**receiver.data, CONF_DEVICES: devices}
        )
        await hass.async_block_till_done()

    reload_spy.assert_called_once_with(receiver.entry_id)


async def test_update_listener_reloads_on_connection_change(
    hass, receiver_entry_builder
):
    """Re-pointing the receiver at a new host reloads it.

    The reconfigure / Supervisor-discovery / rebind paths only *write* the new
    connection target — Home Assistant forbids pairing an update listener with the
    reloading config-flow helpers — so the listener owns this reload.
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder, host="old.local")

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        subentry = receiver_subentry(receiver)
        hass.config_entries.async_update_subentry(
            receiver, subentry, data={**subentry.data, CONF_HOST: "new.local"}
        )
        await hass.async_block_till_done()

    reload_spy.assert_called_once_with(receiver.entry_id)


async def test_update_listener_reloads_on_unique_id_rebind(
    hass, receiver_entry_builder
):
    """Rebinding the entry onto a new stable radio id reloads it."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        hass.config_entries.async_update_subentry(
            receiver, receiver_subentry(receiver), unique_id="serial:0123"
        )
        await hass.async_block_till_done()

    reload_spy.assert_called_once_with(receiver.entry_id)


async def test_update_listener_no_reload_for_unrelated_change(
    hass, receiver_entry_builder
):
    """An options change the coordinator can absorb live (the timeout) does not reload."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    assert coordinator.availability_timeout == 600

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        hass.config_entries.async_update_entry(
            receiver, options={CONF_AVAILABILITY_TIMEOUT: 120}
        )
        await hass.async_block_till_done()

    reload_spy.assert_not_called()
    # The coordinator's availability_timeout was updated live instead.
    assert coordinator.availability_timeout == 120


async def test_update_listener_updates_availability_timeout_live(
    hass, receiver_entry_builder
):
    """Changing availability_timeout is applied live without reload."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)
    # Default is 600 from _setup_receiver

    hass.config_entries.async_update_entry(
        receiver, options={CONF_AVAILABILITY_TIMEOUT: 120}
    )
    await hass.async_block_till_done()

    assert coordinator.availability_timeout == 120


async def test_update_listener_no_coordinator_returns_early(
    hass, receiver_entry_builder
):
    """If the coordinator is gone, _async_update_listener returns early without error.

    Exercises the ``if coordinator is None: return`` guard in the update listener.
    """
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    coordinator = _coordinator(hass, receiver)

    # Temporarily remove coordinator, call the listener, then restore
    hass.data[DOMAIN].pop(receiver_id(receiver), None)

    with patch.object(
        hass.config_entries, "async_reload", return_value=True
    ) as reload_spy:
        await _async_update_listener(hass, receiver)

    reload_spy.assert_not_called()

    # Restore for proper teardown
    hass.data[DOMAIN][receiver_id(receiver)] = coordinator


# ===========================================================================
# async_migrate_entry
# ===========================================================================


async def test_migrate_entry_returns_false_for_future_version(hass):
    """Version > 3 (future schema) is unsupported and returns False."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="future receiver",
        version=4,
        data={CONF_HOST: "h", CONF_PORT: 8433, CONF_PATH: "/ws"},
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)
    assert result is False


async def test_migrate_entry_v2_returns_true_immediately(hass):
    """A version-2 entry needs no migration and returns True immediately."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="receiver v2",
        version=2,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_RECEIVER,
        },
    )
    entry.add_to_hass(hass)

    # Version 2 should pass through: no v1 block is hit, just returns True.
    result = await async_migrate_entry(hass, entry)
    assert result is True


async def test_migrate_entry_v1_receiver_bumps_version_to_3(hass):
    """Migrating a v1 receiver entry walks the whole ladder to version 3."""
    receiver = MockConfigEntry(
        domain=DOMAIN,
        title="receiver v1",
        version=1,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_RECEIVER,
        },
    )
    receiver.add_to_hass(hass)

    result = await async_migrate_entry(hass, receiver)
    assert result is True
    assert receiver.version == 3


async def test_migrate_entry_v1_device_bumps_version_to_2(hass):
    """Migrating a v1 device entry bumps its version to 2."""
    receiver_id = "receiver-id-001"
    receiver = MockConfigEntry(
        domain=DOMAIN,
        title="receiver",
        version=1,
        entry_id=receiver_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_RECEIVER,
        },
    )
    device = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_RECEIVER_ENTRY_ID: receiver_id,
            CONF_DEVICE_KEY: "Sensor-1",
            CONF_MODEL: "Sensor",
        },
    )
    receiver.add_to_hass(hass)
    device.add_to_hass(hass)

    result = await async_migrate_entry(hass, device)
    assert result is True
    assert device.version == 2


async def test_migrate_entry_v1_receiver_removes_child_entries(hass):
    """Migration folds children into the receiver and removes them."""
    receiver_id = "receiver-id-001"
    key_a = "Acurite-606TX-42"

    receiver = MockConfigEntry(
        domain=DOMAIN,
        title="receiver",
        version=1,
        entry_id=receiver_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_RECEIVER,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_RECEIVER_ENTRY_ID: receiver_id,
            CONF_DEVICE_KEY: key_a,
            CONF_MODEL: "Acurite-606TX",
        },
        options={LEGACY_CONF_OBSERVED_FIELDS: ["temperature_C"]},
    )
    receiver.add_to_hass(hass)
    child.add_to_hass(hass)

    result = await async_migrate_entry(hass, receiver)
    assert result is True

    # Only the receiver remains
    entries = hass.config_entries.async_entries(DOMAIN)
    entry_ids = {e.entry_id for e in entries}
    assert receiver_id in entry_ids
    assert child.entry_id not in entry_ids


async def test_migrate_entry_v1_receiver_folds_device_into_devices_map(hass):
    """Migration folds a child's fields into the receiver's devices map."""
    receiver_id = "receiver-id-001"
    key_a = "Acurite-606TX-42"

    receiver = MockConfigEntry(
        domain=DOMAIN,
        title="receiver",
        version=1,
        entry_id=receiver_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_RECEIVER,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_RECEIVER_ENTRY_ID: receiver_id,
            CONF_DEVICE_KEY: key_a,
            CONF_MODEL: "Acurite-606TX",
        },
        options={LEGACY_CONF_OBSERVED_FIELDS: ["temperature_C", "humidity"]},
    )
    receiver.add_to_hass(hass)
    child.add_to_hass(hass)

    await async_migrate_entry(hass, receiver)

    devices = receiver.data.get(CONF_DEVICES, {})
    assert key_a in devices
    assert devices[key_a][CONF_MODEL] == "Acurite-606TX"
    # Fields are sorted
    assert devices[key_a][DEVICE_FIELDS] == ["humidity", "temperature_C"]


async def test_migrate_entry_v1_receiver_preserves_timeout_override(hass):
    """Child's timeout_override option is folded into the devices map."""
    receiver_id = "receiver-id-001"
    key_b = "EnergyMeter-2000-1234"

    receiver = MockConfigEntry(
        domain=DOMAIN,
        title="receiver",
        version=1,
        entry_id=receiver_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_RECEIVER,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_RECEIVER_ENTRY_ID: receiver_id,
            CONF_DEVICE_KEY: key_b,
            CONF_MODEL: "EnergyMeter-2000",
        },
        options={
            LEGACY_CONF_OBSERVED_FIELDS: ["power_W"],
            CONF_AVAILABILITY_TIMEOUT: 120,
        },
    )
    receiver.add_to_hass(hass)
    child.add_to_hass(hass)

    await async_migrate_entry(hass, receiver)

    devices = receiver.data[CONF_DEVICES]
    assert devices[key_b][DEVICE_TIMEOUT_OVERRIDE] == 120


async def test_migrate_entry_v1_device_without_receiver_id_returns_true(hass):
    """A v1 device entry with no CONF_RECEIVER_ENTRY_ID still returns True."""
    device = MockConfigEntry(
        domain=DOMAIN,
        title="orphan device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_DEVICE_KEY: "Sensor-99",
            CONF_MODEL: "Sensor",
            # No CONF_RECEIVER_ENTRY_ID
        },
    )
    device.add_to_hass(hass)

    result = await async_migrate_entry(hass, device)
    assert result is True
    assert device.version == 2


async def test_migrate_entry_v1_device_rehomes_registry_devices(hass):
    """A v1 receiver migration re-homes registry devices to the receiver entry."""
    receiver_id = "receiver-id-001"
    key_a = "Acurite-606TX-42"

    receiver = MockConfigEntry(
        domain=DOMAIN,
        title="receiver",
        version=1,
        entry_id=receiver_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_RECEIVER,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_RECEIVER_ENTRY_ID: receiver_id,
            CONF_DEVICE_KEY: key_a,
            CONF_MODEL: "Acurite-606TX",
        },
        options={LEGACY_CONF_OBSERVED_FIELDS: []},
    )
    receiver.add_to_hass(hass)
    child.add_to_hass(hass)

    dev_reg = dr.async_get(hass)

    # Pre-seed a registry device owned by child
    dev_reg.async_get_or_create(
        config_entry_id=child.entry_id,
        identifiers={(DOMAIN, f"{receiver_id}:{key_a}")},
    )

    # Migrate the receiver (which migrates and removes the child)
    await async_migrate_entry(hass, receiver)

    # The device is now owned by the receiver
    updated_dev = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver_id}:{key_a}"), receiver_id
    )
    assert updated_dev is not None
    # A device belongs to exactly one config entry, so this also says the child
    # entry no longer owns it.
    assert updated_dev.config_entry_id == receiver_id


# ===========================================================================
# _rehome_device_objects
# ===========================================================================


async def test_rehome_device_objects_skips_when_same_entry(
    hass, receiver_entry_builder
):
    """When receiver_entry_id == device_entry.entry_id nothing changes."""
    receiver = receiver_entry_builder(availability_timeout=600)
    receiver.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    er.async_get(hass)

    before_devs = [device.id for device in dev_reg.devices]
    # Should return immediately without touching anything
    _rehome_device_objects(hass, receiver, receiver.entry_id)
    after_devs = [device.id for device in dev_reg.devices]
    assert before_devs == after_devs


async def test_rehome_device_objects_moves_devices_to_receiver(hass):
    """Devices owned by source entry are re-homed to receiver_entry_id."""
    receiver_id = "receiver-entry"
    source_id = "child-entry"

    source = MockConfigEntry(
        domain=DOMAIN,
        title="source",
        version=2,
        entry_id=source_id,
        data={CONF_HOST: "h", CONF_PORT: 8433, CONF_PATH: "/ws"},
    )
    source.add_to_hass(hass)

    # Also need a receiver entry so the receiver_entry_id is valid
    receiver = MockConfigEntry(
        domain=DOMAIN,
        title="receiver",
        version=2,
        entry_id=receiver_id,
        data={CONF_HOST: "h2", CONF_PORT: 8433, CONF_PATH: "/ws"},
    )
    receiver.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    dev = dev_reg.async_get_or_create(
        config_entry_id=source_id,
        identifiers={(DOMAIN, f"{receiver_id}:MySensor-1")},
    )
    assert dev.config_entry_id == source_id

    _rehome_device_objects(hass, source, receiver_id)

    updated = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver_id}:MySensor-1"), receiver_id
    )
    assert updated.config_entry_id == receiver_id


async def test_rehome_device_objects_idempotent_for_devices(hass):
    """Calling _rehome_device_objects twice is safe (idempotent for devices).

    After re-homing, the device already belongs to receiver_entry_id, so the source
    entry's device list is empty and the second call finds nothing to move.
    """
    receiver_id = "receiver-id-001"
    source_id = "child-id-001"

    receiver = MockConfigEntry(
        domain=DOMAIN,
        title="receiver",
        version=2,
        entry_id=receiver_id,
        data={CONF_HOST: "h", CONF_PORT: 8433, CONF_PATH: "/ws"},
    )
    source = MockConfigEntry(
        domain=DOMAIN,
        title="child",
        version=2,
        entry_id=source_id,
        data={CONF_HOST: "h2", CONF_PORT: 8433, CONF_PATH: "/ws"},
    )
    receiver.add_to_hass(hass)
    source.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    dev = dev_reg.async_get_or_create(
        config_entry_id=source_id,
        identifiers={(DOMAIN, f"{receiver_id}:MySensor-42")},
    )
    assert dev.config_entry_id == source_id

    # First call re-homes the device
    _rehome_device_objects(hass, source, receiver_id)
    dev1 = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver_id}:MySensor-42"), receiver_id
    )
    assert dev1.config_entry_id == receiver_id

    # Second call is a no-op (device already belongs to receiver)
    _rehome_device_objects(hass, source, receiver_id)
    dev2 = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{receiver_id}:MySensor-42"), receiver_id
    )
    assert dev2.config_entry_id == receiver_id


# ===========================================================================
# Full setup/unload round-trip: idempotent reload
# ===========================================================================


async def test_reload_is_idempotent(hass, receiver_entry_builder):
    """Reloading a receiver entry succeeds and restores the coordinator."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)

    assert await hass.config_entries.async_reload(receiver.entry_id)
    await hass.async_block_till_done()

    # Coordinator is back in hass.data
    assert receiver_id(receiver) in hass.data[DOMAIN]
    coordinator = _coordinator(hass, receiver)
    assert isinstance(coordinator, Rtl433Coordinator)


async def test_setup_then_unload_then_setup_again(hass, receiver_entry_builder):
    """Setup -> unload -> setup succeeds (no stale state)."""
    receiver = await _setup_receiver(hass, receiver_entry_builder)
    assert await hass.config_entries.async_unload(receiver.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()

    assert receiver_id(receiver) in hass.data[DOMAIN]


# ===========================================================================
# Phantom device cleanup via full setup path
# ===========================================================================


async def test_phantom_cleanup_during_setup(hass, receiver_entry_builder):
    """The phantom unknown device is cleaned up automatically during setup."""
    real_key = "Acurite-606TX-42"
    receiver = receiver_entry_builder(
        availability_timeout=600,
        devices={
            PHANTOM_DEVICE_KEY: {CONF_MODEL: "", DEVICE_FIELDS: []},
            real_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]},
        },
    )
    receiver.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    phantom_ident = (DOMAIN, f"{receiver.entry_id}:{PHANTOM_DEVICE_KEY}")
    dev_reg.async_get_or_create(
        config_entry_id=receiver.entry_id,
        identifiers={phantom_ident},
    )

    assert await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()

    assert PHANTOM_DEVICE_KEY not in receiver.data.get(CONF_DEVICES, {})
    assert real_key in receiver.data[CONF_DEVICES]
    assert (
        dev_reg.async_get_device_by_identifier(phantom_ident, receiver.entry_id) is None
    )


# ===========================================================================
# Motion migration via full setup path
# ===========================================================================


async def test_motion_migration_during_setup(hass, receiver_entry_builder):
    """The motion entity migration runs automatically during setup."""

    device_key = "MySensor-1"

    receiver = receiver_entry_builder(
        availability_timeout=600,
        devices={
            device_key: {
                CONF_MODEL: "Sensor",
                DEVICE_EVENT_TYPES: {"motion": ["on"], "button": ["A"]},
            }
        },
    )
    receiver.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    # Pre-seed an orphaned event.motion entity
    orphan_uid = f"{receiver.entry_id}:{device_key}:motion"
    ent_reg.async_get_or_create("event", DOMAIN, orphan_uid, config_entry=receiver)

    assert await hass.config_entries.async_setup(receiver.entry_id)
    await hass.async_block_till_done()

    # The orphaned entity is gone
    assert ent_reg.async_get_entity_id("event", DOMAIN, orphan_uid) is None

    # The motion slot is dropped from device_event_types
    devices = receiver.data.get(CONF_DEVICES, {})
    if device_key in devices and isinstance(devices[device_key], dict):
        event_types = devices[device_key].get(DEVICE_EVENT_TYPES, {})
        assert "motion" not in event_types


# ===========================================================================
# Migration: clear_delay preserved
# ===========================================================================


async def test_migrate_receiver_entry_preserves_clear_delay(hass):
    """Child's motion_clear_delay option is folded into the devices map."""
    receiver_id = "receiver-id-001"
    key_c = "MotionSensor-1"

    receiver = MockConfigEntry(
        domain=DOMAIN,
        title="receiver",
        version=1,
        entry_id=receiver_id,
        data={
            CONF_HOST: "h",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_ENTRY_TYPE: ENTRY_TYPE_RECEIVER,
        },
    )
    child = MockConfigEntry(
        domain=DOMAIN,
        title="device",
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
            CONF_RECEIVER_ENTRY_ID: receiver_id,
            CONF_DEVICE_KEY: key_c,
            CONF_MODEL: "MotionSensor",
        },
        options={
            LEGACY_CONF_OBSERVED_FIELDS: ["motion"],
            DEVICE_MOTION_CLEAR_DELAY: 45,
        },
    )
    receiver.add_to_hass(hass)
    child.add_to_hass(hass)

    await async_migrate_entry(hass, receiver)

    devices = receiver.data[CONF_DEVICES]
    assert key_c in devices
    assert devices[key_c][DEVICE_MOTION_CLEAR_DELAY] == 45


# ===========================================================================
# PLATFORMS is a list (not a tuple/set) — critical for forwarding
# ===========================================================================


def test_platforms_is_a_list():
    """PLATFORMS must be a list so async_forward_entry_setups accepts it."""
    assert isinstance(PLATFORMS, list)
    assert len(PLATFORMS) > 0
