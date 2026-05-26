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

from types import SimpleNamespace
from unittest.mock import patch

from custom_components.rtl_433 import async_remove_config_entry_device
from custom_components.rtl_433.const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_DISCOVERY_ENABLED,
    CONF_HOST,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    DEVICE_FIELDS,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

VALIDATE = "custom_components.rtl_433.config_flow.Rtl433Coordinator.validate_connection"


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
