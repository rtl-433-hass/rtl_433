"""Tests for the rtl_433 config and options flows (single-hub model).

The connectivity check is patched throughout (no sockets are opened). Coverage:
the hub user step (success + ``cannot_connect``), the hub options step
(discovery toggle + availability timeout persisted to ``entry.options``), the
device options step (set/clear a per-device ``timeout_override`` in
``entry.data["devices"]``, plus the ``no_devices`` abort), and a direct unit
test of ``async_remove_config_entry_device`` (False for the hub device, True +
map/coordinator eviction for a nested device).
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtl_433 import async_remove_config_entry_device
from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_GAS,
    COMMODITY_NONE,
    COMMODITY_WATER,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_DISCOVERY_ENABLED,
    CONF_HOST,
    CONF_INITIAL_FREQUENCY,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    DEFAULT_MANAGE_SETTINGS,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.const import UnitOfVolume
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

VALIDATE = "custom_components.rtl_433.config_flow.Rtl433Coordinator.validate_connection"


def _schema_default(result, key: str):
    """Return the rendered default for a form field key, or ``None``.

    Voluptuous stores a field's default as a zero-arg callable on the marker; this
    pulls the option flow form's commodity pre-fill out of the shown schema so a
    test can assert the rendered default without re-implementing the flow.
    """
    for marker in result["data_schema"].schema:
        if marker == key:
            default = getattr(marker, "default", None)
            return default() if callable(default) else default
    return None


async def test_user_step_success_creates_hub(hass):
    """A reachable server produces a hub entry with the connection data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "rtl433.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "rtl_433 (rtl433.local)"
    assert result["data"][CONF_HOST] == "rtl433.local"
    assert result["data"][CONF_PORT] == 8433
    # No per-device entry_type discriminator in the single-hub model.
    assert "entry_type" not in result["data"]


async def test_user_step_cannot_connect_shows_error(hass):
    """An unreachable server keeps the form open with a cannot_connect error."""
    from custom_components.rtl_433.coordinator import CannotConnect

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(VALIDATE, side_effect=CannotConnect("nope")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "unreachable",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


# --------------------------------------------------------------------------- #
# Options flow — hub step.                                                     #
# --------------------------------------------------------------------------- #
async def test_hub_options_step_persists_discovery_and_timeout(hass, hub_entry_builder):
    """The hub options step persists the discovery toggle + timeout to options."""
    entry = hub_entry_builder(discovery_enabled=True)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"

    # Pick the hub step from the menu.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hub"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DISCOVERY_ENABLED: False, CONF_AVAILABILITY_TIMEOUT: 120},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DISCOVERY_ENABLED] is False
    assert entry.options[CONF_AVAILABILITY_TIMEOUT] == 120


# --------------------------------------------------------------------------- #
# Options flow — device step.                                                  #
# --------------------------------------------------------------------------- #
async def test_device_options_step_sets_and_clears_timeout_override(
    hass, hub_entry_builder
):
    """The device step writes, then clears, a per-device timeout override."""
    device_key = "Acurite-606TX-42"
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
            }
        }
    )
    entry.add_to_hass(hass)

    # Menu -> device step.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "device"

    # Set an override; it lands in entry.data["devices"], not entry.options.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"device": device_key, DEVICE_TIMEOUT_OVERRIDE: 90},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DEVICES][device_key][DEVICE_TIMEOUT_OVERRIDE] == 90
    assert CONF_AVAILABILITY_TIMEOUT not in entry.options

    # Re-enter and submit with the override blank -> it is cleared.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": device_key}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert DEVICE_TIMEOUT_OVERRIDE not in entry.data[CONF_DEVICES][device_key]


async def test_device_options_step_aborts_when_no_devices(hass, hub_entry_builder):
    """With an empty devices map the device step aborts with no_devices."""
    entry = hub_entry_builder()  # no devices seeded
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


# --------------------------------------------------------------------------- #
# Options flow — per-device calibration (device step -> calibration step).     #
# --------------------------------------------------------------------------- #
async def test_calibration_round_trip_writes_into_device_record(
    hass, hub_entry_builder
):
    """A water calibration drives device -> calibration step and is persisted.

    Picking a real commodity on the device step advances to the calibration step;
    submitting ``{unit, scale}`` writes the ``{commodity, unit, scale}`` triple
    into ``entry.data[CONF_DEVICES][device_key]["calibration"]``.
    """
    device_key = "ERT-SCM-9001"
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "ERT-SCM",
                DEVICE_FIELDS: ["consumption_data"],
            }
        }
    )
    entry.add_to_hass(hass)

    # Menu -> device step.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["step_id"] == "device"

    # Choose water -> advance to the calibration step (no record written yet).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"device": device_key, CALIBRATION_COMMODITY: COMMODITY_WATER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration"

    # Submit a convertible volume unit + scale; the triple is persisted.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_UNIT: UnitOfVolume.LITERS, CALIBRATION_SCALE: 0.1},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    calibration = entry.data[CONF_DEVICES][device_key][DEVICE_CALIBRATION]
    assert calibration == {
        CALIBRATION_COMMODITY: COMMODITY_WATER,
        CALIBRATION_UNIT: UnitOfVolume.LITERS,
        CALIBRATION_SCALE: 0.1,
    }
    # The timeout override is untouched (not part of this calibration).
    assert DEVICE_TIMEOUT_OVERRIDE not in entry.data[CONF_DEVICES][device_key]


async def test_device_step_none_commodity_finishes_without_calibration(
    hass, hub_entry_builder
):
    """Commodity ``none`` writes the record (no calibration) and finishes."""
    device_key = "ERT-SCM-9001"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "ERT-SCM", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"device": device_key, CALIBRATION_COMMODITY: COMMODITY_NONE},
    )
    # No calibration step; finishes immediately with no calibration sub-record.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert DEVICE_CALIBRATION not in entry.data[CONF_DEVICES][device_key]


# --------------------------------------------------------------------------- #
# Options flow — commodity pre-fill from the device's last decoded event.      #
# --------------------------------------------------------------------------- #
def _seed_coordinator_last_event(hass, entry, device_key, fields):
    """Stand a minimal coordinator with one device's last event into hass.data.

    The device step reads ``coordinator.devices[device_key].fields`` to pre-fill
    the commodity, so a SimpleNamespace coordinator with the right shape is enough
    to exercise the pre-fill path without a full hub setup.
    """
    event = SimpleNamespace(fields=fields)
    coordinator = SimpleNamespace(devices={device_key: event})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator


async def _open_device_step(hass, entry):
    """Open the options device step and return the shown form result."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )


async def test_commodity_prefill_from_meter_type_string(hass, hub_entry_builder):
    """A last event with ``MeterType: "Gas"`` pre-fills the commodity to gas."""
    device_key = "IDM-1234"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "IDM", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    _seed_coordinator_last_event(hass, entry, device_key, {"MeterType": "Gas"})

    result = await _open_device_step(hass, entry)
    assert result["step_id"] == "device"
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_GAS


async def test_commodity_prefill_from_ert_type_low_nibble(hass, hub_entry_builder):
    """An ``ert_type`` whose low nibble denotes gas pre-fills the commodity to gas.

    ``ert_type & 0x0f == 2`` is a gas commodity; 0x12 exercises that the high
    nibble is ignored.
    """
    device_key = "ERT-SCM-9001"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "ERT-SCM", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    _seed_coordinator_last_event(hass, entry, device_key, {"ert_type": 0x12})

    result = await _open_device_step(hass, entry)
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_GAS


async def test_commodity_prefill_defaults_to_none_without_hint(hass, hub_entry_builder):
    """With no MeterType/ert_type hint, the commodity default stays ``none``."""
    device_key = "ERT-SCM-9001"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "ERT-SCM", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    _seed_coordinator_last_event(hass, entry, device_key, {"consumption_data": 42})

    result = await _open_device_step(hass, entry)
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_NONE


# --------------------------------------------------------------------------- #
# async_remove_config_entry_device (direct unit test).                         #
# --------------------------------------------------------------------------- #
async def test_remove_hub_device_is_refused(hass, hub_entry_builder):
    """Removing the hub device itself returns False (cannot be deleted)."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)

    hub_device = SimpleNamespace(identifiers={(DOMAIN, entry.entry_id)})

    assert await async_remove_config_entry_device(hass, entry, hub_device) is False


async def test_remove_nested_device_evicts_map_and_coordinator(hass, hub_entry_builder):
    """Removing a nested device returns True and drops it from map + coordinator."""
    device_key = "Acurite-606TX-42"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
        }
    )
    entry.add_to_hass(hass)

    # Stand in a fake coordinator so we can observe forget_device and the
    # per-platform device removers being called (both are the Clarification #4
    # re-add path: coordinator state eviction + platform dedup-cache pruning).
    forgotten: list[str] = []
    removed: list[str] = []
    coordinator = SimpleNamespace(
        forget_device=forgotten.append, device_removers=[removed.append]
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    nested_device = SimpleNamespace(
        identifiers={(DOMAIN, f"{entry.entry_id}:{device_key}")}
    )

    assert await async_remove_config_entry_device(hass, entry, nested_device) is True
    # The device_key is gone from the hub devices map...
    assert device_key not in entry.data.get(CONF_DEVICES, {})
    # ...and the coordinator was told to forget it, and the platform removers ran.
    assert forgotten == [device_key]
    assert removed == [device_key]


# --------------------------------------------------------------------------- #
# Reconfigure flow.                                                            #
# --------------------------------------------------------------------------- #
async def test_reconfigure_updates_data_and_preserves_devices(hass, hub_entry_builder):
    """A changed-and-reachable target updates entry.data in place.

    The same flow exercise proves the headline guarantees: aborts with
    ``reconfigure_successful``, host/port/path/secure are rewritten,
    ``manage_settings`` and the seeded ``data["devices"]`` map survive untouched,
    ``entry_id`` is stable, and the unique_id is reconciled to the new host:port.
    """
    device_key = "Acurite-606TX-42"
    seeded_devices = {
        device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
    }
    entry = hub_entry_builder(
        host="old.local",
        port=8433,
        path="/ws",
        devices=seeded_devices,
    )
    entry.add_to_hass(hass)
    # manage_settings is owned by the options flow; stamp it on so we can assert
    # the reconfigure data_updates merge leaves it (and the devices map) intact.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_MANAGE_SETTINGS: True}
    )

    original_entry_id = entry.entry_id
    devices_snapshot = deepcopy(entry.data[CONF_DEVICES])

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    # Suppress the framework-scheduled reload so it does not try a real socket
    # setup; we only need to confirm the update + abort behaviour here.
    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/socket",
                "secure": True,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    # Connection params updated in place.
    assert entry.data[CONF_HOST] == "new.local"
    assert entry.data[CONF_PORT] == 9000
    assert entry.data[CONF_PATH] == "/socket"
    assert entry.data["secure"] is True

    # Same entry, preserved nested state and manage_settings (data_updates merge).
    assert entry.entry_id == original_entry_id
    assert entry.data[CONF_DEVICES] == devices_snapshot
    assert entry.data[CONF_MANAGE_SETTINGS] is True

    # unique_id reconciled to the new host:port, and the title follows the host.
    assert entry.unique_id == "hub:new.local:9000"
    assert entry.title == "rtl_433 (new.local)"


async def test_reconfigure_cannot_connect_keeps_form_and_data(hass, hub_entry_builder):
    """An unreachable target re-shows the form and leaves entry.data unchanged."""
    from custom_components.rtl_433.coordinator import CannotConnect

    entry = hub_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    data_snapshot = deepcopy(dict(entry.data))
    unique_id_snapshot = entry.unique_id

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch(VALIDATE, side_effect=CannotConnect("nope")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "unreachable",
                CONF_PORT: 9999,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}

    # Nothing persisted.
    assert dict(entry.data) == data_snapshot
    assert entry.unique_id == unique_id_snapshot


async def test_reconfigure_collision_aborts_and_mutates_neither(
    hass, hub_entry_builder
):
    """Reconfiguring one hub onto another's host:port aborts as already_configured."""
    entry_a = hub_entry_builder(host="a.local", port=8433, path="/ws")
    entry_b = hub_entry_builder(host="b.local", port=9000, path="/ws")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    a_data_snapshot = deepcopy(dict(entry_a.data))
    a_unique_id_snapshot = entry_a.unique_id
    b_data_snapshot = deepcopy(dict(entry_b.data))
    b_unique_id_snapshot = entry_b.unique_id

    result = await entry_a.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    # Validation passes, but the new host:port collides with entry_b.
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "b.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    # Neither entry's data changed.
    assert dict(entry_a.data) == a_data_snapshot
    assert entry_a.unique_id == a_unique_id_snapshot
    assert dict(entry_b.data) == b_data_snapshot
    assert entry_b.unique_id == b_unique_id_snapshot


async def test_reconfigure_reloads_entry_exactly_once(hass, hub_entry_builder):
    """A successful reconfigure schedules exactly one reload (no double teardown)."""
    entry = hub_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)

    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload") as reload_spy,
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    reload_spy.assert_called_once_with(entry.entry_id)


# --------------------------------------------------------------------------- #
# Options flow — mappings step (per-hub user-mapping overrides).               #
# --------------------------------------------------------------------------- #
async def test_mappings_step_invalid_submit_reshows_form_and_stores_nothing(
    hass, hub_entry_builder
):
    """A schema-invalid mappings object re-shows the form and stores nothing."""
    from custom_components.rtl_433.const import CONF_USER_MAPPINGS

    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    data_snapshot = deepcopy(dict(entry.data))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "mappings"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mappings"

    # An entry missing the required ``platform`` is rejected by the validator.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_USER_MAPPINGS: {"bad_field": {"name": "X", "object_suffix": "X"}}},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mappings"
    assert result["errors"]
    # Nothing was persisted: entry.data is unchanged (no CONF_USER_MAPPINGS).
    assert dict(entry.data) == data_snapshot
    assert CONF_USER_MAPPINGS not in entry.data


async def test_mappings_step_valid_submit_writes_data_leaves_options_and_devices(
    hass, hub_entry_builder
):
    """A valid mappings object lands in entry.data, leaving options + devices intact."""
    from custom_components.rtl_433.const import CONF_USER_MAPPINGS

    device_key = "Acurite-606TX-42"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
        },
        options={CONF_DISCOVERY_ENABLED: True},
    )
    entry.add_to_hass(hass)
    options_snapshot = deepcopy(dict(entry.options))
    devices_snapshot = deepcopy(entry.data[CONF_DEVICES])

    # The update listener reloads the hub when CONF_USER_MAPPINGS changes; the
    # entry is not actually set up here, so suppress the scheduled reload.
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "mappings"}
        )
        assert result["step_id"] == "mappings"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_USER_MAPPINGS: {
                    "temperature_C": {
                        "platform": "sensor",
                        "name": "Kelvin Temp",
                        "object_suffix": "K",
                        "unit_of_measurement": "K",
                    }
                }
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The override was written (normalized) into entry.data.
    stored = entry.data[CONF_USER_MAPPINGS]
    assert stored["temperature_C"]["unit_of_measurement"] == "K"
    # Options and the devices map are untouched.
    assert dict(entry.options) == options_snapshot
    assert entry.data[CONF_DEVICES] == devices_snapshot


# --------------------------------------------------------------------------- #
# Supervisor (hassio) discovery flow.                                          #
# --------------------------------------------------------------------------- #
def _disc(host="core-rtl433", port=8433, uid="serial:0123"):
    """Build a Supervisor add-on discovery payload for one radio."""
    return HassioServiceInfo(
        config={
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PATH: "/ws",
            "secure": False,
            "unique_id": uid,
            "addon": "rtl_433",
        },
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )


def _radio_entry(host="core-rtl433", port=8433, uid="serial:0123"):
    """Build a discovered-style hub entry keyed by a stable radio unique_id."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"rtl_433 ({host})",
        unique_id=uid,
        data={
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PATH: "/ws",
            "secure": False,
            CONF_MANAGE_SETTINGS: False,
        },
        version=2,
    )


def _flow_title_placeholders(hass):
    """Return the in-progress flow's context title_placeholders."""
    flow = next(iter(hass.config_entries.flow._progress.values()))
    return flow.context.get("title_placeholders")


async def test_hassio_discovery_happy_path_creates_entry(hass):
    """Discovery -> confirm form -> create entry keyed by the advertised radio id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_disc()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    # The confirm form surfaces exactly which add-on/radio it is.
    assert result["description_placeholders"] == {
        "addon": "rtl_433",
        "host": "core-rtl433",
        "port": "8433",
    }
    # The discovered card title is set from the add-on name and host:port.
    assert _flow_title_placeholders(hass) == {"name": "rtl_433 (core-rtl433:8433)"}

    with patch(VALIDATE, return_value=True) as validate:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    # Connectivity is revalidated against the discovered target before adoption.
    validate.assert_called_once_with(hass, "core-rtl433", 8433, "/ws", secure=False)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == "serial:0123"
    assert entry.title == "rtl_433 (core-rtl433:8433)"
    # Exact data shape: connection params + the default toggles. Submitting the
    # empty confirm form applies the schema defaults (manage-settings default +
    # discovery enabled True, no initial frequency).
    assert entry.data == {
        CONF_HOST: "core-rtl433",
        CONF_PORT: 8433,
        CONF_PATH: "/ws",
        "secure": False,
        CONF_MANAGE_SETTINGS: DEFAULT_MANAGE_SETTINGS,
        CONF_DISCOVERY_ENABLED: True,
    }


async def test_hassio_discovery_non_default_fields_propagate(hass):
    """A discovery carrying non-default path/secure/addon propagates them through."""
    disc = HassioServiceInfo(
        config={
            CONF_HOST: "core-rtl433",
            CONF_PORT: 8500,
            CONF_PATH: "/socket",
            "secure": True,
            "unique_id": "usbpath:1-1.4",
            "addon": "Custom rtl_433",
        },
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=disc
    )
    assert result["description_placeholders"] == {
        "addon": "Custom rtl_433",
        "host": "core-rtl433",
        "port": "8500",
    }

    with patch(VALIDATE, return_value=True) as validate:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    validate.assert_called_once_with(hass, "core-rtl433", 8500, "/socket", secure=True)
    entry = result["result"]
    assert entry.unique_id == "usbpath:1-1.4"
    assert entry.data[CONF_PATH] == "/socket"
    assert entry.data["secure"] is True


async def test_hassio_discovery_missing_optional_fields_use_defaults(hass):
    """A discovery with only host/port/unique_id falls back to path/secure/addon defaults."""
    disc = HassioServiceInfo(
        config={CONF_HOST: "core-rtl433", CONF_PORT: 8433, "unique_id": "serial:0123"},
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=disc
    )
    # addon name falls back to "rtl_433" when the message omits it.
    assert result["description_placeholders"] == {
        "addon": "rtl_433",
        "host": "core-rtl433",
        "port": "8433",
    }

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    entry = result["result"]
    assert entry.data[CONF_PATH] == "/ws"
    assert entry.data["secure"] is False


async def test_hassio_discovery_missing_unique_id_aborts(hass):
    """A discovery message without a unique_id is rejected as malformed."""
    disc = HassioServiceInfo(
        config={CONF_HOST: "core-rtl433", CONF_PORT: 8433},
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=disc
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"


async def test_hassio_discovery_adopts_manual_entry(hass, hub_entry_builder):
    """Discovery of a manually-added host:port re-keys it to the radio id and aborts."""
    entry = hub_entry_builder(host="core-rtl433", port=8433, path="/ws")
    entry.add_to_hass(hass)
    assert entry.unique_id == "hub:core-rtl433:8433"

    # Advertise a different path/secure so we can prove the connection data is
    # refreshed (not just the unique_id re-keyed) during adoption.
    disc = HassioServiceInfo(
        config={
            CONF_HOST: "core-rtl433",
            CONF_PORT: 8433,
            CONF_PATH: "/ws2",
            "secure": True,
            "unique_id": "serial:0123",
            "addon": "rtl_433",
        },
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=disc
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    adopted = entries[0]
    assert adopted.unique_id == "serial:0123"
    # Connection data refreshed from discovery...
    assert adopted.data[CONF_PATH] == "/ws2"
    assert adopted.data["secure"] is True
    # ...while pre-existing keys (the manual entry's discovery toggle) survive.
    assert adopted.data[CONF_DISCOVERY_ENABLED] is True


async def test_hassio_discovery_distinct_port_is_treated_as_new_radio(hass):
    """A different host:port (with a new id) is a new radio, not an adoption."""
    entry = _radio_entry(host="core-rtl433", port=8433, uid="serial:0123")
    entry.add_to_hass(hass)

    # Same host, different port, different stable id -> no host:port match.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=_disc(port=9999, uid="serial:NEW"),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_hassio_discovery_updates_changed_port_in_place(hass):
    """A re-advertised radio on a new port updates the stored connection and aborts."""
    entry = _radio_entry(port=8433)
    entry.add_to_hass(hass)

    # Same stable id, new port + path so we prove every field is updated.
    disc = HassioServiceInfo(
        config={
            CONF_HOST: "core-rtl433",
            CONF_PORT: 8434,
            CONF_PATH: "/ws9",
            "secure": True,
            "unique_id": "serial:0123",
            "addon": "rtl_433",
        },
        name="rtl_433",
        slug="abc123",
        uuid="deadbeef",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=disc
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].data[CONF_PORT] == 8434
    assert entries[0].data[CONF_PATH] == "/ws9"
    assert entries[0].data["secure"] is True


async def test_user_step_dedups_against_discovered_entry(hass):
    """The manual user step aborts when host:port is already owned by a radio entry."""
    entry = _radio_entry(host="core-rtl433", port=8433)
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "core-rtl433",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_preserves_stable_radio_unique_id(hass):
    """Reconfiguring a discovered entry keeps its stable radio id (not hub:...)."""
    entry = _radio_entry(host="core-rtl433", port=8433, uid="serial:0123")
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 9000,
                CONF_PATH: "/socket",
                "secure": True,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # Stable radio id preserved, not rewritten to hub:host:port.
    assert entry.unique_id == "serial:0123"
    assert entry.data[CONF_HOST] == "new.local"
    assert entry.data[CONF_PORT] == 9000
    assert entry.data[CONF_PATH] == "/socket"
    assert entry.data["secure"] is True


async def test_hassio_confirm_cannot_connect_reshows_form(hass):
    """A failed validation on confirm re-shows the form with cannot_connect."""
    from custom_components.rtl_433.coordinator import CannotConnect

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_disc()
    )
    assert result["step_id"] == "hassio_confirm"

    with patch(VALIDATE, side_effect=CannotConnect("nope")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert result["errors"] == {"base": "cannot_connect"}
    # The re-shown form keeps the addon/host/port context.
    assert result["description_placeholders"] == {
        "addon": "rtl_433",
        "host": "core-rtl433",
        "port": "8433",
    }


# --------------------------------------------------------------------------- #
# Setup toggles + initial frequency (manual user step and discovery confirm).  #
# --------------------------------------------------------------------------- #
async def test_user_step_persists_discovery_off_and_initial_frequency(hass):
    """Managed add with discovery off + a frequency persists both into entry.data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "rtl433.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
                CONF_MANAGE_SETTINGS: True,
                CONF_DISCOVERY_ENABLED: False,
                CONF_INITIAL_FREQUENCY: 868.3,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[CONF_DISCOVERY_ENABLED] is False
    # The frequency rides the managed path; persisted as a float (MHz).
    assert entry.data[CONF_INITIAL_FREQUENCY] == 868.3


async def test_user_step_drops_initial_frequency_when_unmanaged(hass):
    """A frequency entered with management off is not persisted (managed-only path)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "rtl433.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                "secure": False,
                CONF_MANAGE_SETTINGS: False,
                CONF_DISCOVERY_ENABLED: True,
                CONF_INITIAL_FREQUENCY: 868.3,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert CONF_INITIAL_FREQUENCY not in entry.data


async def test_hassio_confirm_persists_toggles_and_frequency(hass):
    """The discovery confirm form persists manage/discovery/frequency into entry.data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_disc()
    )
    assert result["step_id"] == "hassio_confirm"

    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_MANAGE_SETTINGS: True,
                CONF_DISCOVERY_ENABLED: False,
                CONF_INITIAL_FREQUENCY: 915.0,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[CONF_MANAGE_SETTINGS] is True
    assert entry.data[CONF_DISCOVERY_ENABLED] is False
    assert entry.data[CONF_INITIAL_FREQUENCY] == 915.0
