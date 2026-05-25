"""The rtl_433 integration.

This module wires the integration's config-entry lifecycle. There are two kinds
of config entry, discriminated by ``entry.data[CONF_ENTRY_TYPE]``:

* **Hub** entries own one rtl_433 server's WebSocket connection. Setting one up
  loads the mapping library (shipped + user overrides), instantiates the push
  :class:`~custom_components.rtl_433.coordinator.Rtl433Coordinator`, injects the
  skip-keys, the effective-timeout resolver, and the new-device discovery
  callback, registers the hub device, starts the coordinator, and registers an
  options-update listener so toggling discovery / the timeout takes effect
  live. The loaded library is cached on ``hass.data[DOMAIN]["_library"]`` so the
  entity platforms reuse it.

* **Device** entries own one physical device's entities. Setting one up just
  forwards the ``sensor`` / ``binary_sensor`` platforms once the parent hub's
  coordinator is present (otherwise :class:`ConfigEntryNotReady`).

Deleting a hub cascade-removes its child device entries (and thus their HA
devices and entities), leaving no orphans (Clarification #8).
"""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, discovery_flow

from . import repairs
from .config_flow import is_hub_entry
from .const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DISCOVERY_ENABLED,
    CONF_HOST,
    CONF_HUB_ENTRY_ID,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DOMAIN,
    LOGGER,
    PLATFORMS,
)
from .coordinator import Rtl433Coordinator
from .mapping import FieldDescriptor, load_library, load_user_overrides

# Key under ``hass.data[DOMAIN]`` holding the once-loaded mapping library tuple
# ``(registry, skip_keys)`` shared by the coordinator and entity platforms.
DATA_LIBRARY = "_library"

# Discovery-info keys carried into the integration-discovery flow. Mirrors the
# contract documented in ``config_flow.async_step_integration_discovery``.
DISCOVERY_HUB_ENTRY_ID = "hub_entry_id"
DISCOVERY_DEVICE_KEY = "device_key"
DISCOVERY_MODEL = "model"


def _hub_secure(entry: ConfigEntry) -> bool:
    """Return the hub entry's ``secure`` (wss) flag, defaulting to False."""
    return bool(entry.data.get("secure", False))


def _hub_discovery_enabled(entry: ConfigEntry) -> bool:
    """Resolve the hub's discovery toggle (options override data, default on)."""
    return bool(
        entry.options.get(
            CONF_DISCOVERY_ENABLED,
            entry.data.get(CONF_DISCOVERY_ENABLED, True),
        )
    )


def _hub_availability_timeout(entry: ConfigEntry) -> int:
    """Resolve the hub's default availability timeout (options > data > default)."""
    return int(
        entry.options.get(
            CONF_AVAILABILITY_TIMEOUT,
            entry.data.get(CONF_AVAILABILITY_TIMEOUT, DEFAULT_AVAILABILITY_TIMEOUT),
        )
    )


async def _async_load_library(
    hass: HomeAssistant,
) -> tuple[dict[str, FieldDescriptor], set[str]]:
    """Load (and cache) the shipped library merged with user overrides.

    Both the glob/parse and the override merge touch the filesystem, so they run
    in the executor. The merged ``(registry, skip_keys)`` is cached on
    ``hass.data[DOMAIN][DATA_LIBRARY]`` so the entity platforms and additional
    hubs reuse a single load.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    cached = domain_data.get(DATA_LIBRARY)
    if cached is not None:
        return cached

    registry, skip_keys = await hass.async_add_executor_job(load_library)
    registry, skip_keys = await hass.async_add_executor_job(
        load_user_overrides, hass.config.path(), registry, skip_keys
    )
    domain_data[DATA_LIBRARY] = (registry, skip_keys)
    return registry, skip_keys


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up rtl_433 from a config entry (hub or device)."""
    hass.data.setdefault(DOMAIN, {})

    if is_hub_entry(entry):
        return await _async_setup_hub_entry(hass, entry)
    return await _async_setup_device_entry(hass, entry)


async def _async_setup_hub_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a hub config entry: library, coordinator, discovery, watchdog."""
    _registry, skip_keys = await _async_load_library(hass)

    def effective_timeout_resolver(device_key: str) -> int:
        """Resolve a device's effective timeout (per-device override > hub default).

        Searches this hub's child *device* config entries for the one matching
        ``device_key`` and reads its ``options[CONF_AVAILABILITY_TIMEOUT]``
        override; falls back to the hub-level default when none is set.
        """
        for child in hass.config_entries.async_entries(DOMAIN):
            if (
                child.data.get(CONF_HUB_ENTRY_ID) == entry.entry_id
                and child.data.get(CONF_DEVICE_KEY) == device_key
            ):
                override = child.options.get(CONF_AVAILABILITY_TIMEOUT)
                if override is not None:
                    return int(override)
                break
        return _hub_availability_timeout(entry)

    def new_device_callback(device_key: str, model: str) -> None:
        """Start a discovery flow for a newly observed, not-yet-known device.

        Skips devices that already have a configured *or* user-ignored config
        entry under unique_id ``{hub_entry_id}:{device_key}`` so repeated
        sightings never spam the discovery list (Battery-Notes dedup).
        """
        unique_id = f"{entry.entry_id}:{device_key}"
        for existing in hass.config_entries.async_entries(DOMAIN):
            if existing.unique_id == unique_id:
                # Configured (any non-ignore source) or explicitly ignored:
                # either way the device is accounted for, so do not re-surface.
                return

        discovery_flow.async_create_flow(
            hass,
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={
                DISCOVERY_HUB_ENTRY_ID: entry.entry_id,
                DISCOVERY_DEVICE_KEY: device_key,
                DISCOVERY_MODEL: model,
            },
        )

    # Register the hub device so child devices can link to it via ``via_device``.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="rtl_433",
        name=entry.title,
        model="rtl_433 server",
    )

    coordinator = Rtl433Coordinator(
        hass,
        entry,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        path=entry.data[CONF_PATH],
        secure=_hub_secure(entry),
        discovery_enabled=_hub_discovery_enabled(entry),
        availability_timeout=_hub_availability_timeout(entry),
        skip_keys=skip_keys,
    )
    coordinator.new_device_callback = new_device_callback
    coordinator.effective_timeout_resolver = effective_timeout_resolver

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await coordinator.async_start()

    # Watch reachability and surface / clear a repair issue accordingly.
    entry.async_on_unload(
        repairs.async_track_hub_reachability(hass, entry, coordinator)
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_setup_device_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a device config entry by forwarding its entity platforms.

    Defers (raises :class:`ConfigEntryNotReady`) when the parent hub coordinator
    is not loaded yet so Home Assistant retries after the hub comes up.
    """
    hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
    if hub_entry_id not in hass.data.get(DOMAIN, {}):
        raise ConfigEntryNotReady(
            f"Parent hub {hub_entry_id} not loaded yet for device "
            f"{entry.data.get(CONF_DEVICE_KEY)}"
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Push changed hub options into the running coordinator.

    Discovery-toggle and availability-timeout changes are applied live (the
    coordinator reads ``discovery_enabled`` / ``availability_timeout`` on every
    event and watchdog tick). No reload is required for these, so we avoid the
    disruption of tearing the socket down.
    """
    coordinator: Rtl433Coordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is None:
        return

    coordinator.discovery_enabled = _hub_discovery_enabled(entry)
    coordinator.availability_timeout = _hub_availability_timeout(entry)
    LOGGER.debug(
        "rtl_433 hub %s options updated (discovery=%s, timeout=%ss)",
        entry.entry_id,
        coordinator.discovery_enabled,
        coordinator.availability_timeout,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry (hub or device)."""
    if not is_hub_entry(entry):
        return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    coordinator: Rtl433Coordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None:
        await coordinator.async_stop()
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    repairs.async_clear_hub_unreachable(hass, entry)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cascade-remove a hub's child device entries when the hub is deleted.

    Removing each child config entry takes its HA device and entities with it,
    so no orphans remain (Clarification #8). Device entries need no special
    removal handling; only hubs own children.
    """
    if not is_hub_entry(entry):
        return

    children = [
        child
        for child in hass.config_entries.async_entries(DOMAIN)
        if child.data.get(CONF_HUB_ENTRY_ID) == entry.entry_id
    ]
    for child in children:
        LOGGER.debug(
            "rtl_433 cascade-removing child device entry %s (%s) of hub %s",
            child.entry_id,
            child.data.get(CONF_MODEL) or child.data.get(CONF_DEVICE_KEY),
            entry.entry_id,
        )
        await hass.config_entries.async_remove(child.entry_id)


__all__: list[str] = [
    "async_remove_entry",
    "async_setup_entry",
    "async_unload_entry",
]
