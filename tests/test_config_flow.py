"""Tests for the rtl_433 config, discovery, and options flows.

The connectivity check is patched throughout (no sockets are opened). Coverage:
hub user-step success + ``cannot_connect``, integration-discovery creating a
device entry, ignore preventing a re-prompt, and both options flows.
"""

from __future__ import annotations

from unittest.mock import patch

from custom_components.rtl_433.const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DISCOVERY_ENABLED,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_HUB_ENTRY_ID,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_HUB,
)
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_INTEGRATION_DISCOVERY,
    SOURCE_USER,
)
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
    assert result["data"][CONF_ENTRY_TYPE] == ENTRY_TYPE_HUB
    assert result["data"][CONF_HOST] == "rtl433.local"


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


async def test_discovery_creates_device_entry(hass):
    """An integration-discovery flow confirms into a per-device config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={
            CONF_HUB_ENTRY_ID: "hub123",
            CONF_DEVICE_KEY: "Acurite-606TX-42",
            CONF_MODEL: "Acurite-606TX",
        },
    )
    # Discovery lands on the confirm card.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENTRY_TYPE] == ENTRY_TYPE_DEVICE
    assert result["data"][CONF_HUB_ENTRY_ID] == "hub123"
    assert result["data"][CONF_DEVICE_KEY] == "Acurite-606TX-42"
    assert result["data"][CONF_MODEL] == "Acurite-606TX"


async def test_ignored_device_does_not_reprompt(hass):
    """A device whose unique_id is already ignored aborts re-discovery."""
    # Simulate an ignore entry already present for this device.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IGNORE},
        data={"unique_id": "hub123:Acurite-606TX-42", "title": "ignored"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # A subsequent discovery of the same device must abort, not prompt.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={
            CONF_HUB_ENTRY_ID: "hub123",
            CONF_DEVICE_KEY: "Acurite-606TX-42",
            CONF_MODEL: "Acurite-606TX",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_hub_options_flow_updates_discovery_and_timeout(hass, hub_entry_builder):
    """The hub options flow persists the discovery toggle and timeout."""
    entry = hub_entry_builder(discovery_enabled=True)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DISCOVERY_ENABLED: False, CONF_AVAILABILITY_TIMEOUT: 120},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DISCOVERY_ENABLED] is False
    assert entry.options[CONF_AVAILABILITY_TIMEOUT] == 120


async def test_device_options_flow_sets_timeout_override(hass, device_entry_builder):
    """The device options flow persists a per-device availability override."""
    entry = device_entry_builder(
        hub_entry_id="hub123", device_key="Acurite-606TX-42", model="Acurite-606TX"
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_AVAILABILITY_TIMEOUT: 90}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_AVAILABILITY_TIMEOUT] == 90
