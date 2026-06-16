"""The rtl_433 integration.

This module wires the integration's config-entry lifecycle. There is one kind of
config entry: a **hub** entry that owns one rtl_433 server's WebSocket
connection. Setting one up loads the shipped mapping library (cached once on
``hass.data[DOMAIN][DATA_LIBRARY]``), merges this hub's stored
``entry.data[CONF_USER_MAPPINGS]`` over it and caches the per-entry merged
``(registry, skip_keys)`` on ``hass.data[DOMAIN][DATA_ENTRY_LIBRARY][entry_id]``
so the entity platforms reuse it, instantiates the push
:class:`~custom_components.rtl_433.coordinator.Rtl433Coordinator`, injects the
skip-keys, the effective-timeout resolver, and the new-device callback, registers
the hub device, starts the coordinator, registers an options-update listener so
toggling discovery / the timeout takes effect live, and forwards the
``sensor`` / ``binary_sensor`` platforms once on the hub entry.

RF devices are represented as **device-registry devices nested under the hub
entry** (rfxtrx-style), not as their own config entries. They are recreated on
startup from ``entry.data[CONF_DEVICES]`` and added at runtime via the
new-device dispatcher signal (gated by the discovery toggle). A single nested
device can be removed from its device page via
:func:`async_remove_config_entry_device`; deleting the hub entry removes all
nested devices and entities automatically.

The library loading lives in :mod:`.library`, the hub-setting resolvers in
:mod:`.hub_settings`, and the config-entry migration / one-time legacy cleanups
in :mod:`.migration`; this module keeps only the steady-state lifecycle.
"""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import repairs
from .const import (
    CONF_DEVICES,
    CONF_HOST,
    CONF_INITIAL_FREQUENCY,
    CONF_PATH,
    CONF_PORT,
    CONF_USER_MAPPINGS,
    DATA_ENTRY_LIBRARY,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    LOGGER,
    MANUFACTURER,
    PLATFORMS,
    signal_new_device,
)
from .coordinator import Rtl433Coordinator
from .hub_settings import (
    _calibration_map,
    _explicit_hub_timeout,
    _hub_availability_timeout,
    _hub_discovery_enabled,
    _hub_manage_settings,
    _hub_secure,
)
from .library import _async_load_library, _merge_entry_library
from .mapping import event_driven_field_keys
from .migration import (
    _cleanup_phantom_unknown_device,
    _migrate_motion_event_to_binary_sensor,
    async_migrate_entry,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an rtl_433 hub config entry.

    Loads the library, registers the hub device, builds and starts the
    coordinator, wires the reachability watcher and options-update listener, and
    forwards the entity platforms once on the hub entry.
    """
    hass.data.setdefault(DOMAIN, {})

    shipped_registry, shipped_skip_keys = await _async_load_library(hass)
    entry_registry, entry_skip_keys = _merge_entry_library(
        hass, entry, shipped_registry, shipped_skip_keys
    )
    hass.data[DOMAIN].setdefault(DATA_ENTRY_LIBRARY, {})[entry.entry_id] = (
        entry_registry,
        entry_skip_keys,
    )
    # Field keys whose presence marks a device as event-driven (never-expire
    # availability). Derived from this entry's merged library so the
    # classification follows the shipped library plus any user mappings; a reload
    # after an options/user-mapping change re-runs setup and refreshes the set.
    entry_event_driven_keys = event_driven_field_keys(entry_registry)

    def effective_timeout_resolver(device_key: str) -> int | None:
        """Resolve a device's *explicit* effective timeout, or ``None``.

        Resolution order for the two explicit tiers handled here:
        per-device ``timeout_override`` (``entry.data[CONF_DEVICES][device_key]``)
        → explicit hub default (only when ``CONF_AVAILABILITY_TIMEOUT`` is actually
        present in the entry's options/data). Returns ``None`` when neither is set,
        signalling the coordinator to apply the device-class default from the
        device's latest payload. An explicit ``0`` at either tier means
        never-expire and is returned as ``0`` (never falls through).
        """
        override = (
            entry.data.get(CONF_DEVICES, {})
            .get(device_key, {})
            .get(DEVICE_TIMEOUT_OVERRIDE)
        )
        if override is not None:
            return int(override)
        return _explicit_hub_timeout(entry)

    def effective_clear_delay_resolver(device_key: str) -> int:
        """Resolve a device's effective motion clear-delay (override > default).

        Reads the per-device ``motion_clear_delay`` from the hub's devices map
        (``entry.data[CONF_DEVICES][device_key]``); falls back to
        ``DEFAULT_MOTION_CLEAR_DELAY`` when none is set.
        """
        override = (
            entry.data.get(CONF_DEVICES, {})
            .get(device_key, {})
            .get(DEVICE_MOTION_CLEAR_DELAY)
        )
        if override is not None:
            return int(override)
        return DEFAULT_MOTION_CLEAR_DELAY

    def new_device_callback(device_key: str, model: str, is_replay: bool) -> None:
        """Dispatch the hub-level new-device signal for a newly observed device.

        The coordinator only invokes this when discovery is enabled and the
        device is new to its in-memory set (``is_new``), so the platform
        listeners can add the nested device + its entities directly. The signal
        is dispatched for replay-discovered devices too, so a device first seen
        via a reconnect replay still gets its entities wired up and seeded.

        The in-app persistent notification, however, is raised only for a
        *genuine* first-time live discovery — a device that is both absent from
        the persisted ``entry.data[CONF_DEVICES]`` map AND seen on a live (non
        ``is_replay``) frame. The map is the restart-safe "ever-adopted" record,
        so a device already in it is a known device re-observed after a
        restart/reload (``coordinator.devices`` starts empty) and is not
        re-notified. The ``is_replay`` gate additionally suppresses notifications
        for the rtl_433 server's reconnect event-buffer replay: those frames are
        re-broadcasts of already-transmitted events, never a new device's first
        live transmission, so they must not raise a "new device" alert even if
        the device has not yet landed in the persisted map. ``is_new_device`` is
        captured *before* the dispatch, which schedules the deferred
        ``async_upsert_device`` that adds the key to the map.
        """
        is_new_device = device_key not in entry.data.get(CONF_DEVICES, {})
        async_dispatcher_send(
            hass, signal_new_device(entry.entry_id), device_key, model
        )
        if is_new_device and not is_replay:
            name = model or device_key
            persistent_notification.async_create(
                hass,
                f"A new device '{name}' was discovered on hub '{entry.title}'.",
                title="rtl_433: New device discovered",
                notification_id=(f"{DOMAIN}_new_device_{entry.entry_id}_{device_key}"),
            )

    # Register the hub device so nested devices can link to it via ``via_device``.
    # The manufacturer/model start generic and are refined to the real SDR's
    # vendor/product/serial once the coordinator connects (``hub_info_callback``).
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=MANUFACTURER,
        name=entry.title,
        model="rtl_433 server",
    )
    _cleanup_phantom_unknown_device(hass, entry, device_registry)
    _migrate_motion_event_to_binary_sensor(hass, entry, er.async_get(hass))

    coordinator = Rtl433Coordinator(
        hass,
        entry,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        path=entry.data[CONF_PATH],
        secure=_hub_secure(entry),
        discovery_enabled=_hub_discovery_enabled(entry),
        manage_settings=_hub_manage_settings(entry),
        availability_timeout=_hub_availability_timeout(entry),
        initial_center_frequency=entry.data.get(CONF_INITIAL_FREQUENCY),
        skip_keys=entry_skip_keys,
        event_driven_keys=entry_event_driven_keys,
    )

    @callback
    def hub_info_callback() -> None:
        """Refresh the hub device's identity from the SDR's ``dev_info``.

        ``coordinator.dev_info`` is the librtlsdr USB label
        (``{"vendor", "product", "serial"}``); map it onto the hub device so the
        device page shows which physical dongle this hub is, instead of the
        generic ``rtl_433`` / ``rtl_433 server`` placeholders. Absent fields (e.g.
        ``-D manual`` with no SDR open) leave the existing values untouched.
        """
        info = coordinator.dev_info
        updates: dict[str, str] = {}
        if info.get("vendor"):
            updates["manufacturer"] = info["vendor"]
        if info.get("product"):
            updates["model"] = info["product"]
        if info.get("serial"):
            updates["serial_number"] = info["serial"]
        if not updates:
            return
        device = device_registry.async_get_device(
            identifiers={(DOMAIN, entry.entry_id)}
        )
        if device is not None:
            device_registry.async_update_device(device.id, **updates)

    coordinator.new_device_callback = new_device_callback
    coordinator.hub_info_callback = hub_info_callback
    coordinator.effective_timeout_resolver = effective_timeout_resolver
    coordinator.effective_clear_delay_resolver = effective_clear_delay_resolver
    # Global descriptor keys from the merged library, so the coordinator can flag
    # observed fields with no mapping at DEBUG (matches the diagnostics
    # ``unmatched_field_keys`` semantics, which resolve against the flat table).
    coordinator.known_field_keys = frozenset(entry_registry.flat)
    # Snapshot the per-device calibration so the update listener can detect a
    # real calibration change (and reload) while ignoring routine devices-map
    # upserts — the same change-vs-snapshot pattern as ``manage_settings``.
    coordinator.calibration_snapshot = _calibration_map(entry)
    # Snapshot the stored user mappings so the update listener can detect a real
    # mappings change (and reload to rebuild the merged library + entities) while
    # ignoring routine devices-map upserts.
    coordinator.user_mappings_snapshot = entry.data.get(CONF_USER_MAPPINGS) or {}

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await coordinator.async_start()

    # Watch reachability and surface / clear a repair issue accordingly.
    entry.async_on_unload(
        repairs.async_track_hub_reachability(hass, entry, coordinator)
    )
    # Advise when a single high-band frequency is left at the default sample rate.
    entry.async_on_unload(repairs.async_track_sample_rate(hass, entry, coordinator))

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Push changed hub options into the running coordinator.

    The manage-settings toggle changes the entity set (the SDR control entities
    appear / disappear) and the coordinator's adoption/enforcement behaviour, so
    a change there requires a full reload to rebuild everything. The running
    coordinator holds the *previous* effective value as
    ``coordinator.manage_settings``; comparing it against the new effective value
    detects the change without persisting extra bookkeeping.

    A per-device calibration change is detected the same way: the options device
    step writes the calibration into ``entry.data[CONF_DEVICES]`` (firing this
    listener), and a consumption sensor's ``device_class`` / unit / ``state_class``
    are construction-time, so the affected entity must be rebuilt by reloading the
    hub. The new calibration map is compared against ``coordinator.calibration_
    snapshot`` (captured at setup) so the *frequent* idempotent devices-map upserts
    (``async_upsert_device`` / ``async_upsert_event_types``), which leave the
    calibration sub-record untouched, never trigger a reload.

    Discovery-toggle and availability-timeout changes are applied live instead
    (the coordinator reads ``discovery_enabled`` / ``availability_timeout`` on
    every event and watchdog tick), so no reload is required for those and we
    avoid the disruption of tearing the socket down.
    """
    coordinator: Rtl433Coordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is None:
        return

    new_manage = _hub_manage_settings(entry)
    if new_manage != coordinator.manage_settings:
        # The entity set changes (SDR controls appear / disappear) and the
        # coordinator's adoption/enforcement flips, so reload to rebuild.
        await hass.config_entries.async_reload(entry.entry_id)
        return

    if _calibration_map(entry) != coordinator.calibration_snapshot:
        # A consumption sensor's device_class / unit / state_class are
        # construction-time, so rebuild the affected entity by reloading the hub.
        await hass.config_entries.async_reload(entry.entry_id)
        return

    if (entry.data.get(CONF_USER_MAPPINGS) or {}) != coordinator.user_mappings_snapshot:
        # The user mappings drive the merged library (descriptors + skip_keys),
        # which is consumed at construction time, so reload to rebuild the merged
        # library and the affected entities.
        await hass.config_entries.async_reload(entry.entry_id)
        return

    coordinator.discovery_enabled = _hub_discovery_enabled(entry)
    coordinator.availability_timeout = _hub_availability_timeout(entry)
    LOGGER.debug(
        "rtl_433 hub %s options updated (discovery=%s, timeout=%ss)",
        entry.title,
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
    hass.data.get(DOMAIN, {}).get(DATA_ENTRY_LIBRARY, {}).pop(entry.entry_id, None)
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
    on).
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
            # Drop the entity platforms' per-device dedup cache and field
            # listeners so the device re-appears cleanly if it transmits again
            # while discovery is on.
            for remover in list(coordinator.device_removers):
                remover(device_key)

    return True


__all__: list[str] = [
    "async_migrate_entry",
    "async_remove_config_entry_device",
    "async_setup_entry",
    "async_unload_entry",
]
