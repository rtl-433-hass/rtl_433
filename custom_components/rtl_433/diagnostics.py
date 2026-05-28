"""Diagnostics export for the rtl_433 integration.

``async_get_config_entry_diagnostics`` returns a redacted snapshot of the hub's
runtime state for support and — crucially — for *contributors* extending the
device library: the ``unmatched_field_keys`` list surfaces exactly which fields
the hub has observed that have no mapping descriptor yet (and are not on the
skip-list), so adding library coverage is a matter of reading the diagnostics
rather than packet-sniffing.

Every config entry is a hub entry that owns the coordinator and its runtime
state; the nested RF devices are device-registry devices, not config entries.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, CONF_PATH, CONF_PORT, DATA_LIBRARY, DOMAIN
from .coordinator import Rtl433Coordinator
from .mapping import Registry, lookup

# Keys redacted from the exported connection params. The host can reveal a
# private network address / hostname, so it is redacted; port/path are benign.
TO_REDACT = {CONF_HOST}


def _resolve_coordinator(
    hass: HomeAssistant, entry: ConfigEntry
) -> Rtl433Coordinator | None:
    """Return the coordinator that owns ``entry``'s runtime state, if loaded.

    Every config entry is a hub entry that is its own coordinator.
    """
    return hass.data.get(DOMAIN, {}).get(entry.entry_id)


def _unmatched_field_keys(
    coordinator: Rtl433Coordinator,
    registry: Registry | None,
    skip_keys: set[str],
) -> list[str]:
    """Compute observed fields that have neither a descriptor nor a skip-entry.

    A field is "matched" if :func:`mapping.lookup` resolves it against the
    merged registry; a field is intentionally dropped if it is in ``skip_keys``.
    Everything else the hub has seen is a candidate for new library coverage.
    The check is model-agnostic (``model=None``): ``seen_fields`` is a flat set
    of field keys across all devices, so a field counts as matched if any
    global descriptor exists for it.
    """
    unmatched = {
        field_key
        for field_key in coordinator.seen_fields
        if field_key not in skip_keys and lookup(field_key, None, registry) is None
    }
    return sorted(unmatched)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    domain_data = hass.data.get(DOMAIN, {})
    registry, skip_keys = domain_data.get(DATA_LIBRARY, (None, set()))

    coordinator = _resolve_coordinator(hass, entry)

    diagnostics: dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
    }

    if coordinator is None:
        # Hub not loaded yet: nothing more to report than the (redacted) static
        # entry data.
        diagnostics["coordinator_loaded"] = False
        return diagnostics

    diagnostics["coordinator_loaded"] = True
    diagnostics["connection"] = async_redact_data(
        {
            CONF_HOST: coordinator.host,
            CONF_PORT: coordinator.port,
            CONF_PATH: coordinator.path,
            "secure": coordinator.secure,
            "ws_url": coordinator.ws_url,
        },
        # Redact the host *and* the assembled URL (which embeds the host).
        TO_REDACT | {"ws_url"},
    )
    diagnostics["connected"] = coordinator.connected
    diagnostics["discovery_enabled"] = coordinator.discovery_enabled
    diagnostics["availability_timeout"] = coordinator.availability_timeout

    diagnostics["devices"] = {
        device_key: {
            "model": normalized.model,
            "identity": normalized.identity,
            "fields": sorted(coordinator.device_fields.get(device_key, set())),
            "available": coordinator.available.get(device_key),
            "last_seen": (
                last_seen.isoformat()
                if (last_seen := coordinator.last_seen.get(device_key)) is not None
                else None
            ),
        }
        for device_key, normalized in coordinator.devices.items()
    }

    diagnostics["seen_field_keys"] = sorted(coordinator.seen_fields)
    diagnostics["unmatched_field_keys"] = _unmatched_field_keys(
        coordinator, registry, set(skip_keys)
    )

    return diagnostics
