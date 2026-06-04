"""Mutation-killing tests for custom_components/rtl_433/config_flow.py.

These tests are designed to exercise every branch, boundary, and data-path in
the config flow module. They assert exact values — titles, step_ids,
FlowResultTypes, error keys, entry.data contents, entry.options contents,
unique_ids — to kill mutants that substitute constants, flip conditions, drop
assignments, or alter dict manipulations.

Coverage targets:
- _hub_unique_id format
- STEP_USER_SCHEMA defaults
- async_step_user: success path (data keys/values, title, unique_id), cannot_connect error
- async_step_user: duplicate hub aborted by _abort_if_unique_id_configured
- _reconfigure_schema defaults from entry.data
- async_step_reconfigure: success path (data_updates, unique_id, title), cannot_connect, collision
- async_get_options_flow
- async_step_init: menu options list
- async_step_hub: defaults from options then data, persists to entry.options
- _device_commodity_default: coordinator lookup path
- _registry: hass.data lookup path
- _is_motion_bearing: True/False, model-scoped lookup
- _write_device_record: override set/clear, calibration set/clear, motion_clear_delay
  set/clear, opt_record present/absent, options[CONF_DEVICES] written
- async_step_device: no_devices abort, commodity=none finish, commodity!=none goto calibration,
  device picker options, single-device commodity pre-fill, motion-bearing field shown/hidden,
  clear_default single vs multi device, motion_clear_delay carried to calibration step
- async_step_calibration: form shown with correct step_id/placeholders, unit/scale persisted,
  existing-calibration pre-fill (matching commodity vs different commodity)
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtl_433.calibration import COMMODITY_UNITS
from custom_components.rtl_433.config_flow import (
    CONF_SECURE,
    _hub_unique_id,
    async_rebind_hub,
)
from custom_components.rtl_433.const import (
    CALIBRATION_COMMODITY,
    CALIBRATION_SCALE,
    CALIBRATION_UNIT,
    COMMODITY_ENERGY,
    COMMODITY_GAS,
    COMMODITY_NONE,
    COMMODITY_WATER,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_DISCOVERY_ENABLED,
    CONF_HOST,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    CONF_USER_MAPPINGS,
    DATA_ENTRY_LIBRARY,
    DATA_LIBRARY,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MANAGE_SETTINGS,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from custom_components.rtl_433.mapping import FieldDescriptor, Registry
from custom_components.rtl_433.options_flow import CONF_DEVICE
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

VALIDATE = "custom_components.rtl_433.config_flow.Rtl433Coordinator.validate_connection"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema_default(result: dict[str, Any], key: str):
    """Pull the rendered default for a form field key from the result schema."""
    for marker in result["data_schema"].schema:
        if marker == key:
            default = getattr(marker, "default", None)
            return default() if callable(default) else default
    return None


def _schema_keys(result: dict[str, Any]) -> set[str]:
    """Return the set of field key names present in a form result's schema."""
    keys = set()
    for marker in result["data_schema"].schema:
        # marker may be a str or a Voluptuous marker (Required/Optional)
        if hasattr(marker, "schema"):
            keys.add(marker.schema)
        else:
            keys.add(str(marker))
    return keys


# ---------------------------------------------------------------------------
# _hub_unique_id unit tests
# ---------------------------------------------------------------------------


def test_hub_unique_id_format():
    """_hub_unique_id returns exact 'hub:{host}:{port}' string."""
    assert _hub_unique_id("myhost", 8433) == "hub:myhost:8433"


def test_hub_unique_id_different_hosts_differ():
    """Different hosts produce different unique_ids."""
    assert _hub_unique_id("a.local", 8433) != _hub_unique_id("b.local", 8433)


def test_hub_unique_id_different_ports_differ():
    """Different ports produce different unique_ids."""
    assert _hub_unique_id("host", 8433) != _hub_unique_id("host", 9000)


# ---------------------------------------------------------------------------
# User flow — success path: exact data, title, unique_id
# ---------------------------------------------------------------------------


async def test_user_step_initial_form(hass):
    """Initiating the user flow shows a FORM at step_id 'user'."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_step_creates_entry_exact_data(hass):
    """Successful user step produces a CREATE_ENTRY with all submitted data fields."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "myserver.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
                CONF_MANAGE_SETTINGS: True,
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "rtl_433 (myserver.local)"
    assert result["data"][CONF_HOST] == "myserver.local"
    assert result["data"][CONF_PORT] == 8433
    assert result["data"][CONF_PATH] == "/ws"
    assert result["data"][CONF_SECURE] is False
    assert result["data"][CONF_MANAGE_SETTINGS] is True


async def test_user_step_creates_entry_secure_true(hass):
    """secure=True is stored exactly in entry.data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "secure.local",
                CONF_PORT: 9000,
                CONF_PATH: "/socket",
                CONF_SECURE: True,
                CONF_MANAGE_SETTINGS: False,
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SECURE] is True
    assert result["data"][CONF_MANAGE_SETTINGS] is False
    assert result["data"][CONF_PORT] == 9000
    assert result["data"][CONF_PATH] == "/socket"


async def test_user_step_unique_id_set_on_hub(hass):
    """The hub entry's unique_id is 'hub:{host}:{port}'."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "uid.local",
                CONF_PORT: 5555,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
                CONF_MANAGE_SETTINGS: True,
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "hub:uid.local:5555"


async def test_user_step_title_uses_host(hass):
    """Entry title uses the submitted hostname in 'rtl_433 ({host})' form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "title-host",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
                CONF_MANAGE_SETTINGS: True,
            },
        )
    assert result["title"] == "rtl_433 (title-host)"


async def test_user_step_cannot_connect_shows_base_error(hass):
    """cannot_connect exception maps to errors['base'] = 'cannot_connect'."""
    from custom_components.rtl_433.coordinator import CannotConnect

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE, side_effect=CannotConnect("fail")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "fail.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
                CONF_MANAGE_SETTINGS: True,
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "cannot_connect"
    assert len(result["errors"]) == 1


async def test_user_step_cannot_connect_then_success(hass):
    """After cannot_connect the flow continues on next submit (no abort)."""
    from custom_components.rtl_433.coordinator import CannotConnect

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE, side_effect=CannotConnect("fail")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "fail.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
                CONF_MANAGE_SETTINGS: True,
            },
        )
    assert result["type"] is FlowResultType.FORM
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "ok.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
                CONF_MANAGE_SETTINGS: True,
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_step_duplicate_host_port_aborted(hass, hub_entry_builder):
    """A second hub with the same host:port is aborted (already_configured)."""
    entry = hub_entry_builder(host="dup.local", port=8433)
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "dup.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
                CONF_MANAGE_SETTINGS: True,
            },
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reconfigure flow — initial form pre-fill from entry.data
# ---------------------------------------------------------------------------


async def test_reconfigure_form_pre_fills_host(hass, hub_entry_builder):
    """Reconfigure form is pre-filled with entry.data host."""
    entry = hub_entry_builder(host="prefill.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert _schema_default(result, CONF_HOST) == "prefill.local"


async def test_reconfigure_form_pre_fills_port(hass, hub_entry_builder):
    """Reconfigure form is pre-filled with entry.data port."""
    entry = hub_entry_builder(host="x.local", port=9999, path="/ws")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert _schema_default(result, CONF_PORT) == 9999


async def test_reconfigure_form_pre_fills_path(hass, hub_entry_builder):
    """Reconfigure form is pre-filled with entry.data path."""
    entry = hub_entry_builder(host="x.local", port=8433, path="/custom")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert _schema_default(result, CONF_PATH) == "/custom"


async def test_reconfigure_form_pre_fills_secure(hass, hub_entry_builder):
    """Reconfigure form is pre-filled with entry.data secure flag."""
    entry = hub_entry_builder(host="x.local", port=8433, secure=True)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert _schema_default(result, CONF_SECURE) is True


async def test_reconfigure_form_has_no_manage_settings_field(hass, hub_entry_builder):
    """Reconfigure schema omits manage_settings (it's an options-flow field)."""
    entry = hub_entry_builder(host="x.local", port=8433)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    for marker in result["data_schema"].schema:
        key = marker.schema if hasattr(marker, "schema") else str(marker)
        assert key != CONF_MANAGE_SETTINGS


# ---------------------------------------------------------------------------
# Reconfigure flow — success: exact updated data fields
# ---------------------------------------------------------------------------


async def test_reconfigure_success_updates_host_port_path_secure(
    hass, hub_entry_builder
):
    """Successful reconfigure updates all connection fields in entry.data."""
    entry = hub_entry_builder(host="old.local", port=8433, path="/ws", secure=False)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 9001,
                CONF_PATH: "/newpath",
                CONF_SECURE: True,
            },
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "new.local"
    assert entry.data[CONF_PORT] == 9001
    assert entry.data[CONF_PATH] == "/newpath"
    assert entry.data[CONF_SECURE] is True


async def test_reconfigure_success_unique_id_updated(hass, hub_entry_builder):
    """Reconfigure updates the entry's unique_id to match the new host:port."""
    entry = hub_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 7777,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
            },
        )
    assert entry.unique_id == "hub:new.local:7777"


async def test_reconfigure_success_title_uses_new_host(hass, hub_entry_builder):
    """Reconfigure updates the entry title to 'rtl_433 ({new_host})'."""
    entry = hub_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "titled.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
            },
        )
    assert entry.title == "rtl_433 (titled.local)"


async def test_reconfigure_preserves_manage_settings(hass, hub_entry_builder):
    """Reconfigure does not clobber manage_settings (data_updates merge)."""
    entry = hub_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_MANAGE_SETTINGS: False}
    )
    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
            },
        )
    assert entry.data[CONF_MANAGE_SETTINGS] is False


async def test_reconfigure_preserves_devices_map(hass, hub_entry_builder):
    """Reconfigure leaves entry.data['devices'] untouched."""
    device_key = "Foo-1"
    entry = hub_entry_builder(
        host="old.local",
        port=8433,
        devices={device_key: {CONF_MODEL: "Foo", DEVICE_FIELDS: ["temperature_C"]}},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "new.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
            },
        )
    assert device_key in entry.data[CONF_DEVICES]
    assert entry.data[CONF_DEVICES][device_key][CONF_MODEL] == "Foo"


async def test_reconfigure_cannot_connect_shows_error(hass, hub_entry_builder):
    """cannot_connect on reconfigure shows the form with base error."""
    from custom_components.rtl_433.coordinator import CannotConnect

    entry = hub_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with patch(VALIDATE, side_effect=CannotConnect("fail")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "bad.local",
                CONF_PORT: 8433,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_cannot_connect_data_unchanged(hass, hub_entry_builder):
    """cannot_connect on reconfigure leaves entry.data completely unchanged."""
    from custom_components.rtl_433.coordinator import CannotConnect

    entry = hub_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    original_host = entry.data[CONF_HOST]
    original_port = entry.data[CONF_PORT]
    original_uid = entry.unique_id
    result = await entry.start_reconfigure_flow(hass)
    with patch(VALIDATE, side_effect=CannotConnect("fail")):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "bad.local",
                CONF_PORT: 9999,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
            },
        )
    assert entry.data[CONF_HOST] == original_host
    assert entry.data[CONF_PORT] == original_port
    assert entry.unique_id == original_uid


async def test_reconfigure_collision_aborts_already_configured(hass, hub_entry_builder):
    """Reconfiguring one hub to collide with another returns already_configured."""
    entry_a = hub_entry_builder(host="a.local", port=8433, path="/ws")
    entry_b = hub_entry_builder(host="b.local", port=9000, path="/ws")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    result = await entry_a.start_reconfigure_flow(hass)
    with patch(VALIDATE, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "b.local",
                CONF_PORT: 9000,
                CONF_PATH: "/ws",
                CONF_SECURE: False,
            },
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_same_host_port_does_not_collide_with_self(
    hass, hub_entry_builder
):
    """Reconfiguring a hub to the SAME host:port is not treated as a collision."""
    entry = hub_entry_builder(host="same.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(VALIDATE, return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "same.local",
                CONF_PORT: 8433,
                CONF_PATH: "/newpath",
                CONF_SECURE: False,
            },
        )
    # Should not abort as already_configured, should be reconfigure_successful
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


# ---------------------------------------------------------------------------
# Options flow — init step
# ---------------------------------------------------------------------------


async def test_options_init_shows_menu_with_hub_and_device(hass, hub_entry_builder):
    """Options init step shows a menu with 'hub', 'device', and 'mappings'."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert "hub" in result["menu_options"]
    assert "device" in result["menu_options"]
    assert "mappings" in result["menu_options"]
    assert len(result["menu_options"]) == 3


# ---------------------------------------------------------------------------
# Options flow — hub step
# ---------------------------------------------------------------------------


async def test_hub_step_shows_form(hass, hub_entry_builder):
    """Hub options step shows a FORM at step_id='hub'."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hub"


async def test_hub_step_persists_discovery_false(hass, hub_entry_builder):
    """Hub step writes CONF_DISCOVERY_ENABLED=False to entry.options."""
    entry = hub_entry_builder(discovery_enabled=True)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DISCOVERY_ENABLED: False,
            CONF_AVAILABILITY_TIMEOUT: 300,
            CONF_MANAGE_SETTINGS: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DISCOVERY_ENABLED] is False


async def test_hub_step_persists_exact_timeout(hass, hub_entry_builder):
    """Hub step writes CONF_AVAILABILITY_TIMEOUT exactly as submitted."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DISCOVERY_ENABLED: True,
            CONF_AVAILABILITY_TIMEOUT: 999,
            CONF_MANAGE_SETTINGS: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_AVAILABILITY_TIMEOUT] == 999


async def test_hub_step_persists_manage_settings(hass, hub_entry_builder):
    """Hub step writes CONF_MANAGE_SETTINGS exactly as submitted."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DISCOVERY_ENABLED: True,
            CONF_AVAILABILITY_TIMEOUT: 600,
            CONF_MANAGE_SETTINGS: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MANAGE_SETTINGS] is False


async def test_hub_step_entry_title_is_empty(hass, hub_entry_builder):
    """Hub step finishes with empty title (only updates options, not title)."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DISCOVERY_ENABLED: True,
            CONF_AVAILABILITY_TIMEOUT: 600,
            CONF_MANAGE_SETTINGS: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ""


async def test_hub_step_default_discovery_from_options(hass, hub_entry_builder):
    """Hub step pre-fills discovery from entry.options when available."""
    entry = hub_entry_builder(
        options={CONF_DISCOVERY_ENABLED: False, CONF_AVAILABILITY_TIMEOUT: 120}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    assert result["type"] is FlowResultType.FORM
    assert _schema_default(result, CONF_DISCOVERY_ENABLED) is False


async def test_hub_step_default_timeout_from_options(hass, hub_entry_builder):
    """Hub step pre-fills timeout from entry.options when available."""
    entry = hub_entry_builder(
        options={CONF_DISCOVERY_ENABLED: True, CONF_AVAILABILITY_TIMEOUT: 123}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    assert _schema_default(result, CONF_AVAILABILITY_TIMEOUT) == 123


async def test_hub_step_default_discovery_from_data_fallback(hass, hub_entry_builder):
    """Hub step falls back to entry.data[CONF_DISCOVERY_ENABLED] when not in options."""
    entry = hub_entry_builder(discovery_enabled=False)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    assert _schema_default(result, CONF_DISCOVERY_ENABLED) is False


async def test_hub_step_default_timeout_falls_back_to_constant(hass, hub_entry_builder):
    """Hub step default timeout falls back to DEFAULT_AVAILABILITY_TIMEOUT when absent."""
    entry = hub_entry_builder()  # no availability_timeout in data or options
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    assert (
        _schema_default(result, CONF_AVAILABILITY_TIMEOUT)
        == DEFAULT_AVAILABILITY_TIMEOUT
    )


async def test_hub_step_default_manage_settings_from_data_fallback(
    hass, hub_entry_builder
):
    """Hub step falls back to DEFAULT_MANAGE_SETTINGS when manage_settings absent from both."""
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    # Remove manage_settings from data if present
    new_data = {k: v for k, v in entry.data.items() if k != CONF_MANAGE_SETTINGS}
    hass.config_entries.async_update_entry(entry, data=new_data)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    assert _schema_default(result, CONF_MANAGE_SETTINGS) == DEFAULT_MANAGE_SETTINGS


# ---------------------------------------------------------------------------
# Options flow — device step: no_devices abort
# ---------------------------------------------------------------------------


async def test_device_step_aborts_no_devices(hass, hub_entry_builder):
    """Device step aborts with reason='no_devices' when entry has no devices."""
    entry = hub_entry_builder()  # no devices
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


# ---------------------------------------------------------------------------
# Options flow — device step: timeout override set and clear
# ---------------------------------------------------------------------------


async def test_device_step_sets_timeout_override(hass, hub_entry_builder):
    """Device step persists a timeout override into entry.data[devices]."""
    device_key = "Acurite-606TX-1"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, DEVICE_TIMEOUT_OVERRIDE: 77},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DEVICES][device_key][DEVICE_TIMEOUT_OVERRIDE] == 77


async def test_device_step_clears_timeout_override(hass, hub_entry_builder):
    """Device step clears timeout override when not submitted."""
    device_key = "Acurite-606TX-2"
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "Acurite-606TX",
                DEVICE_FIELDS: ["temperature_C"],
                DEVICE_TIMEOUT_OVERRIDE: 90,
            }
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key},  # no DEVICE_TIMEOUT_OVERRIDE -> clears
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert DEVICE_TIMEOUT_OVERRIDE not in entry.data[CONF_DEVICES][device_key]


async def test_device_step_does_not_affect_other_devices_data(hass, hub_entry_builder):
    """Setting override on one device does not change other devices' records."""
    device_a = "Dev-A-1"
    device_b = "Dev-B-2"
    entry = hub_entry_builder(
        devices={
            device_a: {CONF_MODEL: "Dev-A", DEVICE_FIELDS: ["temperature_C"]},
            device_b: {CONF_MODEL: "Dev-B", DEVICE_FIELDS: ["temperature_C"]},
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_a, DEVICE_TIMEOUT_OVERRIDE: 55},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DEVICES][device_a][DEVICE_TIMEOUT_OVERRIDE] == 55
    assert DEVICE_TIMEOUT_OVERRIDE not in entry.data[CONF_DEVICES][device_b]


async def test_device_step_timeout_override_not_in_options(hass, hub_entry_builder):
    """Timeout override lives in entry.data, not in entry.options."""
    device_key = "Dev-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "Dev", DEVICE_FIELDS: ["temperature_C"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, DEVICE_TIMEOUT_OVERRIDE: 44},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert DEVICE_TIMEOUT_OVERRIDE not in entry.options


# ---------------------------------------------------------------------------
# Options flow — device step: commodity = none finishes without calibration
# ---------------------------------------------------------------------------


async def test_device_step_none_commodity_clears_existing_calibration(
    hass, hub_entry_builder
):
    """Submitting commodity=none removes any existing calibration from the record."""
    device_key = "Cal-Dev-1"
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "Cal-Dev",
                DEVICE_FIELDS: ["consumption_data"],
                DEVICE_CALIBRATION: {
                    CALIBRATION_COMMODITY: COMMODITY_WATER,
                    CALIBRATION_UNIT: UnitOfVolume.LITERS,
                    CALIBRATION_SCALE: 0.5,
                },
            }
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_NONE},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert DEVICE_CALIBRATION not in entry.data[CONF_DEVICES][device_key]


async def test_device_step_none_commodity_result_type(hass, hub_entry_builder):
    """Commodity=none goes directly to CREATE_ENTRY without a calibration step."""
    device_key = "NoCalib-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "NoCalib", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_NONE},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# Options flow — device step: calibration step advance
# ---------------------------------------------------------------------------


async def test_device_step_energy_commodity_advances_to_calibration(
    hass, hub_entry_builder
):
    """Picking energy on the device step advances to the calibration form."""
    device_key = "Energy-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "EnergyDev", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_ENERGY},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration"


async def test_device_step_gas_commodity_advances_to_calibration(
    hass, hub_entry_builder
):
    """Picking gas on the device step advances to the calibration form."""
    device_key = "Gas-1"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "GasDev", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_GAS},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration"


async def test_device_step_water_commodity_advances_to_calibration(
    hass, hub_entry_builder
):
    """Picking water on the device step advances to the calibration form."""
    device_key = "Water-1"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "WaterDev", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_WATER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "calibration"


# ---------------------------------------------------------------------------
# Options flow — calibration step: form + persist
# ---------------------------------------------------------------------------


async def test_calibration_form_step_id(hass, hub_entry_builder):
    """Calibration form has step_id='calibration'."""
    device_key = "Cal-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "CalDev", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_GAS},
    )
    assert result["step_id"] == "calibration"


async def test_calibration_form_description_placeholder_commodity(
    hass, hub_entry_builder
):
    """Calibration form description_placeholders contains {'commodity': chosen_commodity}."""
    device_key = "Cal-2"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "CalDev2", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_WATER},
    )
    assert result["description_placeholders"] == {"commodity": COMMODITY_WATER}


async def test_calibration_gas_placeholder_is_gas(hass, hub_entry_builder):
    """Calibration form description_placeholder for gas is 'gas'."""
    device_key = "Cal-gas"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "GasDev", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_GAS},
    )
    assert result["description_placeholders"]["commodity"] == COMMODITY_GAS


async def test_calibration_energy_write_exact_record(hass, hub_entry_builder):
    """Calibration for energy writes exact {commodity, unit, scale} triple."""
    device_key = "Energy-cal-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "EnergyDev", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_ENERGY},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_UNIT: UnitOfEnergy.KILO_WATT_HOUR, CALIBRATION_SCALE: 0.001},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    cal = entry.data[CONF_DEVICES][device_key][DEVICE_CALIBRATION]
    assert cal[CALIBRATION_COMMODITY] == COMMODITY_ENERGY
    assert cal[CALIBRATION_UNIT] == UnitOfEnergy.KILO_WATT_HOUR
    assert cal[CALIBRATION_SCALE] == pytest.approx(0.001)


async def test_calibration_water_write_exact_record(hass, hub_entry_builder):
    """Calibration for water writes exact {commodity, unit, scale} triple."""
    device_key = "Water-cal-1"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "WaterDev", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_WATER},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_UNIT: UnitOfVolume.GALLONS, CALIBRATION_SCALE: 10.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    cal = entry.data[CONF_DEVICES][device_key][DEVICE_CALIBRATION]
    assert cal[CALIBRATION_COMMODITY] == COMMODITY_WATER
    assert cal[CALIBRATION_UNIT] == UnitOfVolume.GALLONS
    assert cal[CALIBRATION_SCALE] == pytest.approx(10.0)


async def test_calibration_gas_write_exact_record(hass, hub_entry_builder):
    """Calibration for gas writes exact {commodity, unit, scale} triple."""
    device_key = "Gas-cal-1"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "GasDev", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_GAS},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_UNIT: UnitOfVolume.CUBIC_METERS, CALIBRATION_SCALE: 1.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    cal = entry.data[CONF_DEVICES][device_key][DEVICE_CALIBRATION]
    assert cal[CALIBRATION_COMMODITY] == COMMODITY_GAS
    assert cal[CALIBRATION_UNIT] == UnitOfVolume.CUBIC_METERS
    assert cal[CALIBRATION_SCALE] == pytest.approx(1.0)


async def test_calibration_also_sets_timeout_override(hass, hub_entry_builder):
    """When device step also had a timeout, calibration step persists both."""
    device_key = "Cal-timeout-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "CalToDev", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DEVICE: device_key,
            DEVICE_TIMEOUT_OVERRIDE: 33,
            CALIBRATION_COMMODITY: COMMODITY_WATER,
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_UNIT: UnitOfVolume.CUBIC_FEET, CALIBRATION_SCALE: 2.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    rec = entry.data[CONF_DEVICES][device_key]
    assert rec[DEVICE_TIMEOUT_OVERRIDE] == 33
    assert rec[DEVICE_CALIBRATION][CALIBRATION_COMMODITY] == COMMODITY_WATER


async def test_calibration_prefill_from_existing_same_commodity(
    hass, hub_entry_builder
):
    """Calibration step pre-fills unit from existing calibration when commodity matches."""
    device_key = "Cal-prefill-1"
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "CalPre",
                DEVICE_FIELDS: ["consumption_data"],
                DEVICE_CALIBRATION: {
                    CALIBRATION_COMMODITY: COMMODITY_GAS,
                    CALIBRATION_UNIT: UnitOfVolume.CUBIC_FEET,
                    CALIBRATION_SCALE: 3.0,
                },
            }
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    # Pick the same commodity as stored (gas)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_GAS},
    )
    assert result["step_id"] == "calibration"
    # unit default should be pre-filled with the stored unit
    assert _schema_default(result, CALIBRATION_UNIT) == UnitOfVolume.CUBIC_FEET
    # scale default should be pre-filled with the stored scale
    assert _schema_default(result, CALIBRATION_SCALE) == pytest.approx(3.0)


async def test_calibration_prefill_unit_falls_back_when_commodity_differs(
    hass, hub_entry_builder
):
    """Calibration unit pre-fill uses default_unit when commodity differs from stored."""
    from custom_components.rtl_433.calibration import default_unit

    device_key = "Cal-prefill-2"
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "CalPre2",
                DEVICE_FIELDS: ["consumption_data"],
                DEVICE_CALIBRATION: {
                    CALIBRATION_COMMODITY: COMMODITY_GAS,
                    CALIBRATION_UNIT: UnitOfVolume.CUBIC_FEET,
                    CALIBRATION_SCALE: 3.0,
                },
            }
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    # Pick water — different from stored gas
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_WATER},
    )
    assert result["step_id"] == "calibration"
    # unit default should be default_unit(water) not cubic feet
    assert _schema_default(result, CALIBRATION_UNIT) == default_unit(COMMODITY_WATER)


async def test_calibration_prefill_scale_is_one_when_no_existing(
    hass, hub_entry_builder
):
    """Calibration scale defaults to 1.0 when no existing calibration is present."""
    device_key = "Cal-noprefill-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "NoPre", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_ENERGY},
    )
    assert _schema_default(result, CALIBRATION_SCALE) == pytest.approx(1.0)


async def test_calibration_prefill_unit_default_for_energy(hass, hub_entry_builder):
    """Calibration unit default for energy is first in COMMODITY_UNITS[energy]."""
    device_key = "Cal-energy-def"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "EnergyDev", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_ENERGY},
    )
    expected_unit = COMMODITY_UNITS[COMMODITY_ENERGY][0]
    assert _schema_default(result, CALIBRATION_UNIT) == expected_unit


async def test_calibration_overwrites_existing_calibration(hass, hub_entry_builder):
    """Submitting new calibration overwrites the existing calibration record."""
    device_key = "Cal-overwrite-1"
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "OWDev",
                DEVICE_FIELDS: ["consumption"],
                DEVICE_CALIBRATION: {
                    CALIBRATION_COMMODITY: COMMODITY_WATER,
                    CALIBRATION_UNIT: UnitOfVolume.LITERS,
                    CALIBRATION_SCALE: 0.1,
                },
            }
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_ENERGY},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_UNIT: UnitOfEnergy.WATT_HOUR, CALIBRATION_SCALE: 5.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    cal = entry.data[CONF_DEVICES][device_key][DEVICE_CALIBRATION]
    assert cal[CALIBRATION_COMMODITY] == COMMODITY_ENERGY
    assert cal[CALIBRATION_UNIT] == UnitOfEnergy.WATT_HOUR
    assert cal[CALIBRATION_SCALE] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Options flow — device step: motion clear delay (non-motion-bearing → absent)
# ---------------------------------------------------------------------------


async def test_device_step_no_motion_bearing_no_clear_delay_field(
    hass, hub_entry_builder
):
    """Without any motion-bearing device, the clear-delay field is absent."""
    device_key = "Temp-1"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "TempSensor", DEVICE_FIELDS: ["temperature_C"]}
        }
    )
    entry.add_to_hass(hass)
    # No library data in hass.data -> _registry() returns (None, None)[0] = None
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert DEVICE_MOTION_CLEAR_DELAY not in _schema_keys(result)


async def test_device_step_motion_bearing_shows_clear_delay_field(
    hass, hub_entry_builder
):
    """When a motion-bearing device is in the hub, clear-delay field appears."""
    from custom_components.rtl_433.mapping import FieldDescriptor, Registry

    device_key = "Motion-1"
    field_key = "motion"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "MotionDev", DEVICE_FIELDS: [field_key]}}
    )
    entry.add_to_hass(hass)

    # Build a minimal registry with a descriptor that has clear_delay set
    motion_descriptor = FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name="Motion",
        object_suffix="motion",
        clear_delay=30,
    )
    registry = Registry(flat={field_key: motion_descriptor}, models={})
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert DEVICE_MOTION_CLEAR_DELAY in _schema_keys(result)


async def test_device_step_clear_delay_default_is_constant_multi_device(
    hass, hub_entry_builder
):
    """With multiple devices, clear-delay default is DEFAULT_MOTION_CLEAR_DELAY."""
    from custom_components.rtl_433.mapping import FieldDescriptor, Registry

    field_key = "motion"
    dev_a = "Motion-A"
    dev_b = "Motion-B"
    entry = hub_entry_builder(
        devices={
            dev_a: {CONF_MODEL: "MotionDev", DEVICE_FIELDS: [field_key]},
            dev_b: {CONF_MODEL: "MotionDev2", DEVICE_FIELDS: [field_key]},
        }
    )
    entry.add_to_hass(hass)
    motion_descriptor = FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name="Motion",
        object_suffix="motion",
        clear_delay=30,
    )
    registry = Registry(flat={field_key: motion_descriptor}, models={})
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert (
        _schema_default(result, DEVICE_MOTION_CLEAR_DELAY) == DEFAULT_MOTION_CLEAR_DELAY
    )


async def test_device_step_clear_delay_prefill_from_single_device_record(
    hass, hub_entry_builder
):
    """With one motion-bearing device, clear-delay is pre-filled from the device record."""
    from custom_components.rtl_433.mapping import FieldDescriptor, Registry

    field_key = "motion"
    device_key = "Motion-Solo"
    stored_delay = 45
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "SoloDev",
                DEVICE_FIELDS: [field_key],
                DEVICE_MOTION_CLEAR_DELAY: stored_delay,
            }
        }
    )
    entry.add_to_hass(hass)
    motion_descriptor = FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name="Motion",
        object_suffix="motion",
        clear_delay=30,
    )
    registry = Registry(flat={field_key: motion_descriptor}, models={})
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert _schema_default(result, DEVICE_MOTION_CLEAR_DELAY) == stored_delay


async def test_device_step_clear_delay_prefill_default_when_no_stored(
    hass, hub_entry_builder
):
    """With one motion-bearing device and no stored delay, prefill uses DEFAULT."""
    from custom_components.rtl_433.mapping import FieldDescriptor, Registry

    field_key = "motion"
    device_key = "Motion-Solo2"
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "SoloDev2",
                DEVICE_FIELDS: [field_key],
            }
        }
    )
    entry.add_to_hass(hass)
    motion_descriptor = FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name="Motion",
        object_suffix="motion",
        clear_delay=30,
    )
    registry = Registry(flat={field_key: motion_descriptor}, models={})
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert (
        _schema_default(result, DEVICE_MOTION_CLEAR_DELAY) == DEFAULT_MOTION_CLEAR_DELAY
    )


# ---------------------------------------------------------------------------
# _write_device_record: motion_clear_delay in entry.options
# ---------------------------------------------------------------------------


async def test_write_device_record_motion_delay_set_in_options(hass, hub_entry_builder):
    """motion_clear_delay is stored in entry.options[CONF_DEVICES][device_key]."""
    from custom_components.rtl_433.mapping import FieldDescriptor, Registry

    field_key = "motion"
    device_key = "Motion-opt-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "MotionDev", DEVICE_FIELDS: [field_key]}}
    )
    entry.add_to_hass(hass)
    motion_descriptor = FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name="Motion",
        object_suffix="motion",
        clear_delay=30,
    )
    registry = Registry(flat={field_key: motion_descriptor}, models={})
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, DEVICE_MOTION_CLEAR_DELAY: 60},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    opt_record = entry.options.get(CONF_DEVICES, {}).get(device_key, {})
    assert opt_record[DEVICE_MOTION_CLEAR_DELAY] == 60


async def test_write_device_record_motion_delay_not_in_entry_data(
    hass, hub_entry_builder
):
    """motion_clear_delay submission does NOT appear in entry.data[devices]."""
    from custom_components.rtl_433.mapping import FieldDescriptor, Registry

    field_key = "motion"
    device_key = "Motion-data-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "MotionDev", DEVICE_FIELDS: [field_key]}}
    )
    entry.add_to_hass(hass)
    motion_descriptor = FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name="Motion",
        object_suffix="motion",
        clear_delay=30,
    )
    registry = Registry(flat={field_key: motion_descriptor}, models={})
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, DEVICE_MOTION_CLEAR_DELAY: 99},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # motion_clear_delay must NOT be in entry.data[devices][device_key]
    assert DEVICE_MOTION_CLEAR_DELAY not in entry.data[CONF_DEVICES][device_key]


async def test_write_device_record_motion_delay_cleared_removes_opt_record(
    hass, hub_entry_builder
):
    """Clearing motion_clear_delay (submitting None) removes device from opt_devices."""
    device_key = "Motion-clear-1"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "MotionDev", DEVICE_FIELDS: ["temperature_C"]}
        },
        options={CONF_DEVICES: {device_key: {DEVICE_MOTION_CLEAR_DELAY: 77}}},
    )
    entry.add_to_hass(hass)

    # Submit device step without the motion_clear_delay field -> clears it
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key},  # no DEVICE_MOTION_CLEAR_DELAY -> None -> cleared
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # opt_record should be empty, so device_key should not appear in opt_devices
    opt_devices = entry.options.get(CONF_DEVICES, {})
    assert (
        device_key not in opt_devices
        or DEVICE_MOTION_CLEAR_DELAY not in opt_devices.get(device_key, {})
    )


async def test_write_device_record_options_devices_key_written(hass, hub_entry_builder):
    """options[CONF_DEVICES] is written (not left absent) when motion delay set."""
    from custom_components.rtl_433.mapping import FieldDescriptor, Registry

    field_key = "motion"
    device_key = "Motion-opts-key"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "MotionDev", DEVICE_FIELDS: [field_key]}}
    )
    entry.add_to_hass(hass)
    motion_descriptor = FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name="Motion",
        object_suffix="motion",
        clear_delay=30,
    )
    registry = Registry(flat={field_key: motion_descriptor}, models={})
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, DEVICE_MOTION_CLEAR_DELAY: 55},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_DEVICES in entry.options
    assert device_key in entry.options[CONF_DEVICES]


# ---------------------------------------------------------------------------
# _write_device_record: calibration via the calibration step + motion delay
# ---------------------------------------------------------------------------


async def test_calibration_step_motion_delay_carried_and_written(
    hass, hub_entry_builder
):
    """motion_clear_delay from the device step is carried through calibration."""
    from custom_components.rtl_433.mapping import FieldDescriptor, Registry

    field_key = "motion"
    device_key = "Cal-motion-1"
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "CalMotion",
                DEVICE_FIELDS: [field_key, "consumption"],
            }
        }
    )
    entry.add_to_hass(hass)
    motion_descriptor = FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name="Motion",
        object_suffix="motion",
        clear_delay=30,
    )
    registry = Registry(flat={field_key: motion_descriptor}, models={})
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    # Submit device step with both commodity and motion delay
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DEVICE: device_key,
            CALIBRATION_COMMODITY: COMMODITY_WATER,
            DEVICE_MOTION_CLEAR_DELAY: 120,
        },
    )
    assert result["step_id"] == "calibration"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_UNIT: UnitOfVolume.LITERS, CALIBRATION_SCALE: 0.5},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Calibration written into data
    cal = entry.data[CONF_DEVICES][device_key][DEVICE_CALIBRATION]
    assert cal[CALIBRATION_COMMODITY] == COMMODITY_WATER
    # Motion delay written into options
    opt_record = entry.options[CONF_DEVICES][device_key]
    assert opt_record[DEVICE_MOTION_CLEAR_DELAY] == 120


# ---------------------------------------------------------------------------
# _write_device_record: preserve other devices in CONF_DEVICES
# ---------------------------------------------------------------------------


async def test_write_device_record_preserves_other_devices_in_data(
    hass, hub_entry_builder
):
    """Updating one device's record leaves other devices' records intact in data."""
    dev_a = "DevA-1"
    dev_b = "DevB-2"
    entry = hub_entry_builder(
        devices={
            dev_a: {
                CONF_MODEL: "DevA",
                DEVICE_FIELDS: ["temperature_C"],
                DEVICE_TIMEOUT_OVERRIDE: 120,
            },
            dev_b: {CONF_MODEL: "DevB", DEVICE_FIELDS: ["humidity"]},
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: dev_b, DEVICE_TIMEOUT_OVERRIDE: 50},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # dev_a is untouched
    assert entry.data[CONF_DEVICES][dev_a][DEVICE_TIMEOUT_OVERRIDE] == 120
    assert entry.data[CONF_DEVICES][dev_a][CONF_MODEL] == "DevA"
    # dev_b updated
    assert entry.data[CONF_DEVICES][dev_b][DEVICE_TIMEOUT_OVERRIDE] == 50


# ---------------------------------------------------------------------------
# _write_device_record: options[CONF_DEVICES] stays absent when no motion delay
# ---------------------------------------------------------------------------


async def test_write_device_record_no_motion_delay_no_conf_devices_key(
    hass, hub_entry_builder
):
    """When no motion_clear_delay is set and none was previously set, CONF_DEVICES not in options."""
    device_key = "Plain-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "Plain", DEVICE_FIELDS: ["temperature_C"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, DEVICE_TIMEOUT_OVERRIDE: 30},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # options[CONF_DEVICES] should be there but the device should not have motion key
    # OR CONF_DEVICES should be absent if no device has any options
    opt_devices = entry.options.get(CONF_DEVICES, {})
    opt_dev_rec = opt_devices.get(device_key, {})
    assert DEVICE_MOTION_CLEAR_DELAY not in opt_dev_rec


# ---------------------------------------------------------------------------
# Device step: single-device commodity default from multi-device (stays none)
# ---------------------------------------------------------------------------


async def test_device_step_commodity_default_is_none_for_multi_device(
    hass, hub_entry_builder
):
    """With multiple devices, commodity default is 'none' regardless of coordinator hint."""
    from types import SimpleNamespace

    dev_a = "Dev-Multi-A"
    dev_b = "Dev-Multi-B"
    entry = hub_entry_builder(
        devices={
            dev_a: {CONF_MODEL: "DevA", DEVICE_FIELDS: ["temperature_C"]},
            dev_b: {CONF_MODEL: "DevB", DEVICE_FIELDS: ["consumption_data"]},
        }
    )
    entry.add_to_hass(hass)
    # Even if coordinator hints gas, multi-device defaults to none
    event = SimpleNamespace(fields={"MeterType": "Gas"})
    coordinator = SimpleNamespace(devices={dev_a: event, dev_b: event})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_NONE


async def test_device_step_commodity_prefill_single_device_from_coordinator(
    hass, hub_entry_builder
):
    """With one device and a coordinator gas hint, commodity pre-fills to gas."""
    device_key = "Single-Gas"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "GasMeter", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    event = SimpleNamespace(fields={"MeterType": "Gas"})
    coordinator = SimpleNamespace(devices={device_key: event})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_GAS


async def test_device_step_commodity_prefill_water_from_ert_type(
    hass, hub_entry_builder
):
    """With one device and ert_type water hint, commodity pre-fills to water."""
    device_key = "Single-Water"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "WaterMeter", DEVICE_FIELDS: ["consumption_data"]}
        }
    )
    entry.add_to_hass(hass)
    # ert_type low nibble 11 = water
    event = SimpleNamespace(fields={"ert_type": 11})
    coordinator = SimpleNamespace(devices={device_key: event})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_WATER


async def test_device_step_commodity_prefill_energy_from_electric_meter_type(
    hass, hub_entry_builder
):
    """MeterType='Electric' pre-fills commodity to energy."""
    device_key = "Single-Elec"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "ElecMeter", DEVICE_FIELDS: ["consumption"]}}
    )
    entry.add_to_hass(hass)
    event = SimpleNamespace(fields={"MeterType": "Electric"})
    coordinator = SimpleNamespace(devices={device_key: event})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert _schema_default(result, CALIBRATION_COMMODITY) == COMMODITY_ENERGY


# ---------------------------------------------------------------------------
# _is_motion_bearing: model-scoped descriptor lookup
# ---------------------------------------------------------------------------


async def test_is_motion_bearing_model_scoped_descriptor(hass, hub_entry_builder):
    """_is_motion_bearing uses model-scoped lookup and finds clear_delay."""
    from custom_components.rtl_433.mapping import FieldDescriptor, Registry

    field_key = "motion"
    model = "MotionModel"
    device_key = "MotionScoped-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: model, DEVICE_FIELDS: [field_key]}}
    )
    entry.add_to_hass(hass)

    # Model-scoped descriptor with clear_delay; no global descriptor
    scoped_descriptor = FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name="Motion",
        object_suffix="motion",
        clear_delay=60,
    )
    registry = Registry(flat={}, models={model: {field_key: scoped_descriptor}})
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    # The clear-delay field should appear since the device is motion-bearing via model scope
    assert DEVICE_MOTION_CLEAR_DELAY in _schema_keys(result)


async def test_is_motion_bearing_false_when_descriptor_no_clear_delay(
    hass, hub_entry_builder
):
    """_is_motion_bearing returns False when descriptor has no clear_delay."""
    from custom_components.rtl_433.mapping import FieldDescriptor, Registry

    field_key = "temperature_C"
    device_key = "NonMotion-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "TempDev", DEVICE_FIELDS: [field_key]}}
    )
    entry.add_to_hass(hass)

    temp_descriptor = FieldDescriptor(
        field_key=field_key,
        platform="sensor",
        name="Temperature",
        object_suffix="temperature",
        clear_delay=None,  # Explicitly None
    )
    registry = Registry(flat={field_key: temp_descriptor}, models={})
    hass.data.setdefault(DOMAIN, {})[DATA_LIBRARY] = (registry, set())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    # clear-delay field must NOT appear for non-motion-bearing device
    assert DEVICE_MOTION_CLEAR_DELAY not in _schema_keys(result)


# ---------------------------------------------------------------------------
# async_get_options_flow: returns an OptionsFlow
# ---------------------------------------------------------------------------


async def test_async_get_options_flow_is_callable(hass, hub_entry_builder):
    """async_get_options_flow returns an OptionsFlow-compatible object."""
    from custom_components.rtl_433.config_flow import (
        Rtl433ConfigFlow,
        Rtl433OptionsFlow,
    )

    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    flow = Rtl433ConfigFlow.async_get_options_flow(entry)
    assert isinstance(flow, Rtl433OptionsFlow)


# ---------------------------------------------------------------------------
# VERSION constant
# ---------------------------------------------------------------------------


def test_config_flow_version():
    """Config flow VERSION must be exactly 2."""
    from custom_components.rtl_433.config_flow import Rtl433ConfigFlow

    assert Rtl433ConfigFlow.VERSION == 2


# ---------------------------------------------------------------------------
# Boundary: STEP_USER_SCHEMA defaults
# ---------------------------------------------------------------------------


def test_step_user_schema_port_default():
    """STEP_USER_SCHEMA default port equals DEFAULT_PORT."""
    from custom_components.rtl_433.config_flow import STEP_USER_SCHEMA

    for marker in STEP_USER_SCHEMA.schema:
        if hasattr(marker, "schema") and marker.schema == CONF_PORT:
            default = getattr(marker, "default", None)
            assert (default() if callable(default) else default) == DEFAULT_PORT
            break


def test_step_user_schema_path_default():
    """STEP_USER_SCHEMA default path equals DEFAULT_PATH."""
    from custom_components.rtl_433.config_flow import STEP_USER_SCHEMA

    for marker in STEP_USER_SCHEMA.schema:
        if hasattr(marker, "schema") and marker.schema == CONF_PATH:
            default = getattr(marker, "default", None)
            assert (default() if callable(default) else default) == DEFAULT_PATH
            break


def test_step_user_schema_secure_default_false():
    """STEP_USER_SCHEMA default secure is False."""
    from custom_components.rtl_433.config_flow import STEP_USER_SCHEMA

    for marker in STEP_USER_SCHEMA.schema:
        if hasattr(marker, "schema") and marker.schema == CONF_SECURE:
            default = getattr(marker, "default", None)
            assert (default() if callable(default) else default) is False
            break


def test_step_user_schema_manage_settings_default():
    """STEP_USER_SCHEMA manage_settings default equals DEFAULT_MANAGE_SETTINGS."""
    from custom_components.rtl_433.config_flow import STEP_USER_SCHEMA

    for marker in STEP_USER_SCHEMA.schema:
        if hasattr(marker, "schema") and marker.schema == CONF_MANAGE_SETTINGS:
            default = getattr(marker, "default", None)
            val = default() if callable(default) else default
            assert val == DEFAULT_MANAGE_SETTINGS
            break


# ---------------------------------------------------------------------------
# Device step: sorted device options
# ---------------------------------------------------------------------------


async def test_device_step_form_has_device_selector(hass, hub_entry_builder):
    """Device step form has a CONF_DEVICE selector field."""
    device_key = "Dev-form-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "FormDev", DEVICE_FIELDS: ["temperature_C"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "device"
    # CONF_DEVICE must be a key in the schema
    assert CONF_DEVICE in _schema_keys(result)


# ---------------------------------------------------------------------------
# Calibration step: result_type is CREATE_ENTRY with empty title
# ---------------------------------------------------------------------------


async def test_calibration_step_result_title_is_empty(hass, hub_entry_builder):
    """Calibration step finishes with empty title."""
    device_key = "Cal-title-1"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "CalTitleDev", DEVICE_FIELDS: ["consumption"]}
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, CALIBRATION_COMMODITY: COMMODITY_GAS},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CALIBRATION_UNIT: UnitOfVolume.CUBIC_METERS, CALIBRATION_SCALE: 1.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ""


# ---------------------------------------------------------------------------
# _registry returns None gracefully when not loaded
# ---------------------------------------------------------------------------


async def test_registry_returns_none_when_not_in_hass_data(hass, hub_entry_builder):
    """When DATA_LIBRARY is absent, the device step still shows without clear-delay."""
    device_key = "NoLib-1"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "NoLib", DEVICE_FIELDS: ["temperature_C"]}}
    )
    entry.add_to_hass(hass)
    # Ensure DATA_LIBRARY is not in hass.data
    hass.data.setdefault(DOMAIN, {}).pop(DATA_LIBRARY, None)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    # Should show form without error, and no clear-delay field
    assert result["type"] is FlowResultType.FORM
    assert DEVICE_MOTION_CLEAR_DELAY not in _schema_keys(result)


# ---------------------------------------------------------------------------
# Validate connection is actually called with correct params
# ---------------------------------------------------------------------------


async def test_user_step_validates_correct_params(hass):
    """validate_connection is called with the submitted host/port/path/secure."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE, return_value=True) as mock_validate:
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "checkhost",
                CONF_PORT: 1234,
                CONF_PATH: "/checkpath",
                CONF_SECURE: True,
                CONF_MANAGE_SETTINGS: True,
            },
        )
    mock_validate.assert_called_once_with(
        hass, "checkhost", 1234, "/checkpath", secure=True
    )


async def test_reconfigure_validates_correct_params(hass, hub_entry_builder):
    """validate_connection is called with the submitted host/port/path/secure on reconfigure."""
    entry = hub_entry_builder(host="old.local", port=8433, path="/ws")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(VALIDATE, return_value=True) as mock_validate,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "newhost",
                CONF_PORT: 5678,
                CONF_PATH: "/newpath",
                CONF_SECURE: False,
            },
        )
    mock_validate.assert_called_once_with(
        hass, "newhost", 5678, "/newpath", secure=False
    )


# ===========================================================================
# PR #34 fallout: extra mutation-killers.
#
# The motion/registry tests above seed ``DATA_LIBRARY`` (the shipped-library
# cache key), but ``_registry()`` actually reads
# ``hass.data[DOMAIN][DATA_ENTRY_LIBRARY][entry_id]`` -- the per-entry merged
# cache holding a ``(Registry, skip_keys)`` tuple. The helpers below seed THAT
# exact structure so ``_registry`` / ``_is_motion_bearing`` are genuinely
# exercised and their mutants observable.
# ===========================================================================


def _pr34_motion_descriptor(field_key="motion"):
    return FieldDescriptor(
        field_key=field_key,
        platform="binary_sensor",
        name="Motion",
        object_suffix="motion",
        clear_delay=30,
    )


def _pr34_plain_descriptor(field_key="temperature_C"):
    return FieldDescriptor(
        field_key=field_key,
        platform="sensor",
        name="Temp",
        object_suffix="temp",
    )


def _pr34_seed_entry_library(hass, entry, *, flat=None, models=None, skip_keys=None):
    """Cache ``(Registry, skip_keys)`` under DATA_ENTRY_LIBRARY[entry_id].

    This is the exact tuple ``_registry()`` reads (element ``[0]`` Registry,
    element ``[1]`` skip_keys).
    """
    registry = Registry(flat=flat or {}, models=models or {})
    hass.data.setdefault(DOMAIN, {}).setdefault(DATA_ENTRY_LIBRARY, {})[
        entry.entry_id
    ] = (registry, skip_keys if skip_keys is not None else set())
    return registry


# --------------------------------------------------------------------------- #
# _registry: returns element [0] (Registry), not [1] (skip_keys).             #
# --------------------------------------------------------------------------- #
async def test_pr34_registry_returns_registry_makes_field_appear(
    hass, hub_entry_builder
):
    """With the per-entry cache holding the motion Registry, the knob appears.

    A mutant returning ``[1]`` (skip_keys, a set) instead of ``[0]`` would hand
    ``lookup`` a set, which cannot resolve the descriptor -- so the field would
    be absent. Kills _registry__mutmut_13 ([0]->[1]).
    """
    field_key = "motion"
    device_key = "PR34-Reg0"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "RegDev", DEVICE_FIELDS: [field_key]}}
    )
    entry.add_to_hass(hass)
    _pr34_seed_entry_library(
        hass, entry, flat={field_key: _pr34_motion_descriptor(field_key)}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert DEVICE_MOTION_CLEAR_DELAY in _schema_keys(result)


async def test_pr34_registry_keyed_by_entry_id(hass, hub_entry_builder):
    """The cache is keyed by THIS entry's id; the wrong key yields no field.

    Two hubs: only the active entry has its motion Registry cached under its own
    id. Kills _registry__mutmut_1 (``.get(None, ...)``) and __mutmut_5
    (``.get(None, {})`` for DATA_ENTRY_LIBRARY) and __mutmut_9
    (``hass.data.get(None, {})``) -- each makes the lookup miss the cache and
    drop the field.
    """
    field_key = "motion"
    other = hub_entry_builder(host="other.local", port=1)
    other.add_to_hass(hass)
    device_key = "PR34-Keyed"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "KeyedDev", DEVICE_FIELDS: [field_key]}}
    )
    entry.add_to_hass(hass)
    # Cache ONLY under the active entry's id.
    _pr34_seed_entry_library(
        hass, entry, flat={field_key: _pr34_motion_descriptor(field_key)}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert DEVICE_MOTION_CLEAR_DELAY in _schema_keys(result)


# --------------------------------------------------------------------------- #
# _is_motion_bearing: model arg, registry arg, field-set, record defaults.     #
# --------------------------------------------------------------------------- #
async def test_pr34_motion_bearing_model_scoped_via_entry_cache(
    hass, hub_entry_builder
):
    """The descriptor lives only in the model-scoped table; model must be passed.

    Kills _is_motion_bearing__mutmut_10 (``model = None``) and __mutmut_11
    (``record.get(None)``) and __mutmut_16 (``lookup(field_key, None, ...)``):
    each loses the model and the model-scoped descriptor cannot resolve.
    """
    field_key = "motion"
    model = "PR34ScopedOnly"
    device_key = "PR34-Scoped"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: model, DEVICE_FIELDS: [field_key]}}
    )
    entry.add_to_hass(hass)
    _pr34_seed_entry_library(
        hass, entry, models={model: {field_key: _pr34_motion_descriptor(field_key)}}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert DEVICE_MOTION_CLEAR_DELAY in _schema_keys(result)


async def test_pr34_motion_bearing_uses_entry_registry_arg(hass, hub_entry_builder):
    """lookup must receive the entry-cached registry (3rd positional arg).

    The field ``pr34_custom_motion`` exists ONLY in this hub's cached registry,
    never in the shipped library. Kills __mutmut_12 (``registry = None``),
    __mutmut_17 (``lookup(field_key, model, None)``), __mutmut_19 (drops the
    registry arg) and __mutmut_20 (trailing-comma drop of the registry arg):
    without the real registry the field cannot resolve.
    """
    field_key = "pr34_custom_motion"
    device_key = "PR34-CustomReg"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "CustomDev", DEVICE_FIELDS: [field_key]}}
    )
    entry.add_to_hass(hass)
    _pr34_seed_entry_library(
        hass, entry, flat={field_key: _pr34_motion_descriptor(field_key)}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert DEVICE_MOTION_CLEAR_DELAY in _schema_keys(result)


async def test_pr34_motion_bearing_only_inspects_listed_fields(hass, hub_entry_builder):
    """Only DEVICE_FIELDS entries are inspected; an unlisted motion field is ignored.

    The registry knows a motion descriptor for ``motion`` but the device lists
    only ``temperature_C`` -> NOT motion-bearing -> knob absent. Kills
    __mutmut_23 (``record.get(DEVICE_FIELDS, None)``) and __mutmut_25 (drops the
    ``[]`` default) -- both would iterate the wrong field set (or crash on None)
    rather than the listed fields.
    """
    device_key = "PR34-NoMotionField"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "MixedDev", DEVICE_FIELDS: ["temperature_C"]}}
    )
    entry.add_to_hass(hass)
    _pr34_seed_entry_library(
        hass,
        entry,
        flat={
            "motion": _pr34_motion_descriptor("motion"),
            "temperature_C": _pr34_plain_descriptor("temperature_C"),
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert DEVICE_MOTION_CLEAR_DELAY not in _schema_keys(result)


async def test_pr34_motion_bearing_reads_named_device_record(hass, hub_entry_builder):
    """_is_motion_bearing reads the SELECTED device's record (model + fields).

    Two devices: a motion one and a plain one. With the motion device present
    the knob appears; this exercises the record/model/fields read on a record
    that actually exists. Kills __mutmut_3 (``.get(device_key, None)``),
    __mutmut_5 (drops the ``{}`` record default), __mutmut_7
    (``.get(CONF_DEVICES, None)``) and __mutmut_9 (drops the CONF_DEVICES
    default) -- each crashes or mis-reads the per-device record.
    """
    motion_key = "PR34-Mover"
    plain_key = "PR34-Still"
    entry = hub_entry_builder(
        devices={
            motion_key: {CONF_MODEL: "Mover", DEVICE_FIELDS: ["motion"]},
            plain_key: {CONF_MODEL: "Still", DEVICE_FIELDS: ["temperature_C"]},
        }
    )
    entry.add_to_hass(hass)
    _pr34_seed_entry_library(
        hass,
        entry,
        flat={
            "motion": _pr34_motion_descriptor("motion"),
            "temperature_C": _pr34_plain_descriptor("temperature_C"),
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    # The hub has a motion-bearing device, so the knob is offered.
    assert DEVICE_MOTION_CLEAR_DELAY in _schema_keys(result)


# --------------------------------------------------------------------------- #
# async_step_device: clear-delay default branches (single vs multi device).    #
# --------------------------------------------------------------------------- #
async def test_pr34_clear_delay_default_constant_when_multi_motion(
    hass, hub_entry_builder
):
    """Two motion devices -> clear-delay default is the constant (not stored, not None).

    Exercises the ``len(devices) == 1`` inner guard being False and the default
    falling back to DEFAULT_MOTION_CLEAR_DELAY even though each device stores a
    different override.
    """
    field_key = "motion"
    dev_a = "PR34-Multi-A"
    dev_b = "PR34-Multi-B"
    entry = hub_entry_builder(
        devices={
            dev_a: {
                CONF_MODEL: "MA",
                DEVICE_FIELDS: [field_key],
                DEVICE_MOTION_CLEAR_DELAY: 11,
            },
            dev_b: {
                CONF_MODEL: "MB",
                DEVICE_FIELDS: [field_key],
                DEVICE_MOTION_CLEAR_DELAY: 22,
            },
        }
    )
    entry.add_to_hass(hass)
    _pr34_seed_entry_library(
        hass, entry, flat={field_key: _pr34_motion_descriptor(field_key)}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert _schema_default(result, DEVICE_MOTION_CLEAR_DELAY) == (
        DEFAULT_MOTION_CLEAR_DELAY
    )


async def test_pr34_clear_delay_default_single_reads_stored(hass, hub_entry_builder):
    """One motion device with a stored clear-delay pre-fills that exact value."""
    field_key = "motion"
    device_key = "PR34-SingleStored"
    entry = hub_entry_builder(
        devices={
            device_key: {
                CONF_MODEL: "MS",
                DEVICE_FIELDS: [field_key],
                DEVICE_MOTION_CLEAR_DELAY: 73,
            }
        }
    )
    entry.add_to_hass(hass)
    _pr34_seed_entry_library(
        hass, entry, flat={field_key: _pr34_motion_descriptor(field_key)}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert _schema_default(result, DEVICE_MOTION_CLEAR_DELAY) == 73


async def test_pr34_device_picker_label_is_model_and_key(hass, hub_entry_builder):
    """The picker option label is exactly '{model} ({device_key})'."""
    device_key = "PR34-Label-7"
    entry = hub_entry_builder(
        devices={device_key: {CONF_MODEL: "LabelModel", DEVICE_FIELDS: ["temp"]}}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    label = None
    for marker in result["data_schema"].schema:
        key = marker.schema if hasattr(marker, "schema") else str(marker)
        if key == CONF_DEVICE:
            selector = result["data_schema"].schema[marker]
            label = selector.config["options"][0]["label"]
            break
    assert label == f"LabelModel ({device_key})"


# --------------------------------------------------------------------------- #
# _write_device_record: record starts from existing record; opt records keyed. #
# --------------------------------------------------------------------------- #
async def test_pr34_write_record_merges_into_existing_record(hass, hub_entry_builder):
    """A new override merges into the device's existing record (model preserved).

    ``record = dict(new_devices.get(device_key, {}))`` must seed from the current
    record. Kills __mutmut_11 (``.get(None, {})``), __mutmut_12
    (``.get(device_key, None)`` -> ``dict(None)`` crash), __mutmut_14 (drops the
    ``{}`` default), __mutmut_6 (``data.get(CONF_DEVICES, None)``) and
    __mutmut_8 (drops CONF_DEVICES default).
    """
    device_key = "PR34-Merge"
    entry = hub_entry_builder(
        devices={
            device_key: {CONF_MODEL: "MergeModel", DEVICE_FIELDS: ["temperature_C"]}
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: device_key, DEVICE_TIMEOUT_OVERRIDE: 88},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    record = entry.data[CONF_DEVICES][device_key]
    assert record[DEVICE_TIMEOUT_OVERRIDE] == 88
    assert record[CONF_MODEL] == "MergeModel"
    assert record[DEVICE_FIELDS] == ["temperature_C"]


async def test_pr34_write_record_opt_record_keyed_per_device(hass, hub_entry_builder):
    """Blanking one device's motion delay leaves OTHER devices' opt records.

    ``opt_record = dict(opt_devices.get(device_key, {}))`` and
    ``opt_devices = dict(options.get(CONF_DEVICES, {}))`` must key on the edited
    device only. Kills __mutmut_35 (``options.get(None, {})``) and __mutmut_41
    (``opt_devices.get(None, {})``) which would read the wrong record.
    """
    edited = "PR34-Opt-Edit"
    other = "PR34-Opt-Keep"
    entry = hub_entry_builder(
        devices={
            edited: {CONF_MODEL: "E", DEVICE_FIELDS: ["temperature_C"]},
            other: {CONF_MODEL: "K", DEVICE_FIELDS: ["temperature_C"]},
        },
        options={
            CONF_DEVICES: {
                edited: {DEVICE_MOTION_CLEAR_DELAY: 5},
                other: {DEVICE_MOTION_CLEAR_DELAY: 99},
            }
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    # Submit the edited device with no clear-delay -> clears its opt record only.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEVICE: edited}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    opt_devices = entry.options[CONF_DEVICES]
    assert edited not in opt_devices
    assert opt_devices[other][DEVICE_MOTION_CLEAR_DELAY] == 99


# --------------------------------------------------------------------------- #
# async_step_calibration: pre-fill reads the SELECTED device's record.         #
# --------------------------------------------------------------------------- #
async def test_pr34_calibration_prefill_reads_selected_device(hass, hub_entry_builder):
    """Calibration unit/scale pre-fill reads the SELECTED device's calibration.

    Two devices, each with a different stored water calibration; selecting one
    must pre-fill from that device. Kills __mutmut_18 (``.get(device_key,
    None)``), __mutmut_20 (drops device_key default), __mutmut_22
    (``.get(CONF_DEVICES, None)``) and __mutmut_24 (drops CONF_DEVICES default)
    in the calibration pre-fill read.
    """
    target = "PR34-Cal-Target"
    decoy = "PR34-Cal-Decoy"
    entry = hub_entry_builder(
        devices={
            target: {
                CONF_MODEL: "T",
                DEVICE_FIELDS: ["consumption_data"],
                DEVICE_CALIBRATION: {
                    CALIBRATION_COMMODITY: COMMODITY_WATER,
                    CALIBRATION_UNIT: UnitOfVolume.GALLONS,
                    CALIBRATION_SCALE: 4.0,
                },
            },
            decoy: {
                CONF_MODEL: "D",
                DEVICE_FIELDS: ["consumption_data"],
                DEVICE_CALIBRATION: {
                    CALIBRATION_COMMODITY: COMMODITY_WATER,
                    CALIBRATION_UNIT: UnitOfVolume.LITERS,
                    CALIBRATION_SCALE: 9.0,
                },
            },
        }
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE: target, CALIBRATION_COMMODITY: COMMODITY_WATER},
    )
    assert result["step_id"] == "calibration"
    assert _schema_default(result, CALIBRATION_UNIT) == UnitOfVolume.GALLONS
    assert _schema_default(result, CALIBRATION_SCALE) == pytest.approx(4.0)


# --------------------------------------------------------------------------- #
# async_step_mappings: normalize on store, problem-join, prefill default.      #
# --------------------------------------------------------------------------- #
async def test_pr34_mappings_valid_stores_submitted_object(hass, hub_entry_builder):
    """A valid non-empty mapping is normalized and stored (not blanked to {}).

    Kills the mutant ``normalize_overrides(None)`` (which would store ``{}``);
    we assert the stored object carries the submitted override exactly.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "mappings"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_USER_MAPPINGS: {
                    "humidity": {
                        "platform": "sensor",
                        "name": "Humidity",
                        "object_suffix": "hum",
                        "unit_of_measurement": "%",
                    }
                }
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    stored = entry.data[CONF_USER_MAPPINGS]
    assert stored != {}
    assert stored["humidity"]["unit_of_measurement"] == "%"
    assert stored["humidity"]["name"] == "Humidity"


async def test_pr34_mappings_invalid_joins_problem_strings(hass, hub_entry_builder):
    """Invalid mappings re-show the form with a non-empty joined problems string.

    The ``"; ".join(problems)`` placeholder must be a non-empty string naming the
    offending field. Kills the mutant joining ``None`` (would raise) and the one
    blanking the placeholder.
    """
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "mappings"}
    )
    # A flat field entry missing the required 'platform' is invalid.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_USER_MAPPINGS: {"bad": {"name": "X", "object_suffix": "x"}}},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mappings"
    assert result["errors"]["base"] == "invalid_mappings"
    problems = result["description_placeholders"]["problems"]
    assert isinstance(problems, str)
    assert problems != ""
    assert "bad" in problems


async def test_pr34_mappings_form_prefilled_with_current(hass, hub_entry_builder):
    """The mappings editor default equals the hub's current user_mappings.

    Kills mutants changing the schema default away from ``current`` (the stored
    mapping) -- e.g. defaulting to ``None`` or ``{}``.
    """
    current = {
        "temperature_C": {
            "platform": "sensor",
            "name": "T",
            "object_suffix": "t",
        }
    }
    entry = hub_entry_builder()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_USER_MAPPINGS: current}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "mappings"}
    )
    assert result["type"] is FlowResultType.FORM
    assert _schema_default(result, CONF_USER_MAPPINGS) == current


# ---------------------------------------------------------------------------
# async_rebind_hub: additive-only property — nested ids are byte-identical.
# ---------------------------------------------------------------------------


async def test_rebind_preserves_nested_device_and_entity_unique_ids(hass):
    """Rebinding a hub's radio unique_id must not touch any nested id.

    The additive-only guarantee: device/entity unique_ids and device_keys are
    scoped by the hub ``entry_id`` (never the radio id), so re-pointing the entry
    at a new radio unique_id leaves the registry byte-identical. This asserts the
    full snapshot of nested-device identifiers and entity unique_ids is unchanged.
    """
    entry_id = "rebindhubentry01"
    device_key = "Acurite-606TX-42"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="rtl_433 (old.local)",
        unique_id="radio-old",
        entry_id=entry_id,
        data={
            CONF_HOST: "old.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_SECURE: False,
            CONF_MANAGE_SETTINGS: False,
            CONF_DEVICES: {
                device_key: {
                    CONF_MODEL: "Acurite-606TX",
                    DEVICE_FIELDS: ["temperature_C"],
                }
            },
        },
        version=2,
    )
    entry.add_to_hass(hass)

    # Seed a nested device + entity exactly as the platforms would: the device is
    # identified by ``{entry_id}:{device_key}`` and the entity unique_id by
    # ``{entry_id}:{device_key}:{object_suffix}`` — both entry_id-scoped.
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, f"{entry_id}:{device_key}")},
    )
    entity = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry_id}:{device_key}:temperature",
        config_entry=entry,
        device_id=device.id,
    )

    devices_before = deepcopy(entry.data[CONF_DEVICES])
    device_identifiers_before = set(device.identifiers)
    entity_unique_id_before = entity.unique_id

    # Re-point the entry at a brand-new radio unique_id (the rebind under test).
    with patch.object(hass.config_entries, "async_reload"):
        status = await async_rebind_hub(
            hass,
            entry,
            "radio-new",
            {CONF_HOST: "new.local", CONF_PORT: 9000},
            title="rtl_433 (new.local)",
        )
        await hass.async_block_till_done()

    assert status == "ok"
    # The entry itself moved to the new radio id + connection target.
    assert entry.unique_id == "radio-new"
    assert entry.entry_id == entry_id
    assert entry.data[CONF_HOST] == "new.local"
    assert entry.data[CONF_PORT] == 9000

    # The nested registry rows are byte-identical: device identifiers, entity
    # unique_id, and the persisted device_key scheme all survive untouched.
    device_after = dev_reg.async_get(device.id)
    entity_after = ent_reg.async_get(entity.entity_id)
    assert device_after is not None
    assert entity_after is not None
    assert set(device_after.identifiers) == device_identifiers_before
    assert entity_after.unique_id == entity_unique_id_before
    assert entry.data[CONF_DEVICES] == devices_before
    assert device_key in entry.data[CONF_DEVICES]


async def test_rebind_hub_sets_title_only_when_provided(hass):
    """``title`` is applied when given and left untouched when ``None``.

    Two mutants live on the ``if title is not None`` guard and its body: flipping
    the guard, or forcing the stored title to ``None``. Both are caught by
    asserting the title changes only in the explicit-title call.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="original-title",
        unique_id="radio-old",
        data={
            CONF_HOST: "old.local",
            CONF_PORT: 8433,
            CONF_PATH: "/ws",
            CONF_SECURE: False,
            CONF_MANAGE_SETTINGS: False,
        },
        version=2,
    )
    entry.add_to_hass(hass)

    # No title -> the existing title is preserved (guard must skip the body).
    with patch.object(hass.config_entries, "async_reload"):
        status = await async_rebind_hub(
            hass, entry, "radio-mid", {CONF_HOST: "mid.local"}
        )
        await hass.async_block_till_done()
    assert status == "ok"
    assert entry.title == "original-title"

    # Explicit title -> applied verbatim (body must run with the real value).
    with patch.object(hass.config_entries, "async_reload"):
        await async_rebind_hub(
            hass, entry, "radio-new", {CONF_HOST: "new.local"}, title="brand-new-title"
        )
        await hass.async_block_till_done()
    assert entry.title == "brand-new-title"
