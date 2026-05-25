"""The rtl_433 integration.

Skeleton entry points. Real lifecycle wiring (hub vs device branching, the
WebSocket coordinator, platform forwarding, dispatcher fan-out, and cascade
removal of child device entries) lands in Task 9. The stubs below exist only
so the package imports cleanly and other Phase 1+ tasks have non-conflicting
files to extend.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ENTRY_TYPE, ENTRY_TYPE_HUB, LOGGER


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up rtl_433 from a config entry.

    TODO Task 9: Branch on ``entry.data[CONF_ENTRY_TYPE]`` (hub vs device):
      - hub entries create/start the WebSocket coordinator and store runtime
        state; an options listener and the discovery flow are wired here.
      - device entries register the HA device and forward the SENSOR /
        BINARY_SENSOR platforms, subscribing to the parent hub's dispatcher
        signals.
    This stub only logs and returns True so the package is import-safe.
    """
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_HUB)
    LOGGER.debug(
        "async_setup_entry stub for %s entry %s (real wiring in Task 9)",
        entry_type,
        entry.entry_id,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    TODO Task 9: For hub entries, stop the coordinator and cascade-unload child
    device entries; for device entries, unload the forwarded platforms and
    tear down dispatcher subscriptions. This stub returns True.
    """
    LOGGER.debug(
        "async_unload_entry stub for entry %s (real wiring in Task 9)",
        entry.entry_id,
    )
    return True
