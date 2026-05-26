"""The rtl_433 integration.

This module wires the integration's config-entry lifecycle. There is one kind of
config entry: a **hub** entry that owns one rtl_433 server's WebSocket
connection. Setting one up loads the mapping library (shipped + user overrides),
instantiates the push
:class:`~custom_components.rtl_433.coordinator.Rtl433Coordinator`, injects the
skip-keys, the effective-timeout resolver, and the new-device callback, registers
the hub device, starts the coordinator, registers an options-update listener so
toggling discovery / the timeout takes effect live, and forwards the
``sensor`` / ``binary_sensor`` platforms once on the hub entry. The loaded
library is cached on ``hass.data[DOMAIN][DATA_LIBRARY]`` so the entity platforms
reuse it.

RF devices are represented as **device-registry devices nested under the hub
entry** (rfxtrx-style), not as their own config entries. They are recreated on
startup from ``entry.data[CONF_DEVICES]`` and added at runtime via the
new-device dispatcher signal (gated by the discovery toggle). A single nested
device can be removed from its device page via
:func:`async_remove_config_entry_device`; deleting the hub entry removes all
nested devices and entities automatically.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import repairs
from .const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICES,
    CONF_DISCOVERY_ENABLED,
    CONF_HOST,
    CONF_PATH,
    CONF_PORT,
    DATA_LIBRARY,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    LOGGER,
    PLATFORMS,
    signal_new_device,
)
from .coordinator import Rtl433Coordinator
from .mapping import FieldDescriptor, load_library, load_user_overrides


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
    """Set up an rtl_433 hub config entry.

    Loads the library, registers the hub device, builds and starts the
    coordinator, wires the reachability watcher and options-update listener, and
    forwards the entity platforms once on the hub entry.
    """
    hass.data.setdefault(DOMAIN, {})

    _registry, skip_keys = await _async_load_library(hass)

    def effective_timeout_resolver(device_key: str) -> int:
        """Resolve a device's effective timeout (per-device override > hub default).

        Reads the per-device ``timeout_override`` from the hub's devices map
        (``entry.data[CONF_DEVICES][device_key]``); falls back to the hub-level
        default when none is set.
        """
        override = (
            entry.data.get(CONF_DEVICES, {})
            .get(device_key, {})
            .get(DEVICE_TIMEOUT_OVERRIDE)
        )
        if override is not None:
            return int(override)
        return _hub_availability_timeout(entry)

    def new_device_callback(device_key: str, model: str) -> None:
        """Dispatch the hub-level new-device signal for a newly observed device.

        The coordinator only invokes this when discovery is enabled and the
        device is genuinely new (its ``is_new`` check dedupes), so the platform
        listeners can add the nested device + its entities directly.
        """
        async_dispatcher_send(
            hass, signal_new_device(entry.entry_id), device_key, model
        )

    # Register the hub device so nested devices can link to it via ``via_device``.
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
    """Unload the hub config entry.

    Stops the coordinator, drops its runtime state, clears any reachability
    repair issue, and unloads the forwarded entity platforms.
    """
    coordinator: Rtl433Coordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None:
        await coordinator.async_stop()
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    repairs.async_clear_hub_unreachable(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow removing a single nested RF device from its device page.

    Refuses to remove the hub device itself (identifier
    ``(DOMAIN, entry.entry_id)``) so the hub cannot be deleted out from under its
    config entry. For a nested device, drops it from the hub's devices map and
    evicts its ``device_key`` from the coordinator's runtime state (so a
    re-transmitting device is treated as new and can re-appear while discovery is
    on, Clarification #4).
    """
    if (DOMAIN, config_entry.entry_id) in device_entry.identifiers:
        return False

    # Find this device's device_key from its identifier
    # ``(DOMAIN, f"{entry_id}:{device_key}")``.
    device_key: str | None = None
    for domain, ident in device_entry.identifiers:
        if domain == DOMAIN and ident.startswith(f"{config_entry.entry_id}:"):
            device_key = ident.split(":", 1)[1]
            break

    if device_key is not None:
        devices = {
            k: v
            for k, v in config_entry.data.get(CONF_DEVICES, {}).items()
            if k != device_key
        }
        hass.config_entries.async_update_entry(
            config_entry, data={**config_entry.data, CONF_DEVICES: devices}
        )
        coordinator: Rtl433Coordinator | None = hass.data.get(DOMAIN, {}).get(
            config_entry.entry_id
        )
        if coordinator is not None:
            coordinator.forget_device(device_key)

    return True


__all__: list[str] = [
    "async_remove_config_entry_device",
    "async_setup_entry",
    "async_unload_entry",
]
