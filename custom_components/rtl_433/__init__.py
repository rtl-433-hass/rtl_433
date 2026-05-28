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

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import repairs
from .calibration import normalize_calibration
from .const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DEVICES,
    CONF_DISCOVERY_ENABLED,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_HUB_ENTRY_ID,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    DATA_LIBRARY,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_MANAGE_SETTINGS,
    DEFAULT_MOTION_CLEAR_DELAY,
    DEVICE_CALIBRATION,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    LOGGER,
    PLATFORMS,
    signal_new_device,
)
from .coordinator import Rtl433Coordinator
from .mapping import Registry, load_library, load_user_overrides

# The 0.1.0 per-device config entries stored the set of observed mapped field
# keys under this literal options key. It is intentionally *not* exported from
# const.py (the v2 model uses ``DEVICE_FIELDS`` inside the hub devices map); it
# lives here because it is only ever read by the migration.
LEGACY_CONF_OBSERVED_FIELDS = "observed_fields"

# Pre-fix versions could persist a phantom device under this key (and a matching
# registry device ``(DOMAIN, f"{entry_id}:unknown")``) when a frame could not be
# classified. The frame-routing fix prevents recreation, so the cleanup below
# converges to a clean state after one run.
PHANTOM_DEVICE_KEY = "unknown"


def _cleanup_phantom_unknown_device(
    hass: HomeAssistant, entry: ConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    """Remove a pre-fix phantom ``unknown`` device from the map and registry.

    Idempotent: drops the ``unknown`` key from ``entry.data[CONF_DEVICES]`` (only
    persisting when it changed) and removes the stale registry device
    ``(DOMAIN, f"{entry_id}:unknown")`` if present. Never touches the hub device
    or real nested devices. Safe to run on every setup.
    """
    devices = entry.data.get(CONF_DEVICES, {})
    if PHANTOM_DEVICE_KEY in devices:
        cleaned = {k: v for k, v in devices.items() if k != PHANTOM_DEVICE_KEY}
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICES: cleaned}
        )

    phantom = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}:{PHANTOM_DEVICE_KEY}")}
    )
    if phantom is not None:
        device_registry.async_remove_device(phantom.id)


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


def _hub_manage_settings(entry: ConfigEntry) -> bool:
    """Resolve the hub's manage-settings toggle (options > data > default)."""
    return bool(
        entry.options.get(
            CONF_MANAGE_SETTINGS,
            entry.data.get(CONF_MANAGE_SETTINGS, DEFAULT_MANAGE_SETTINGS),
        )
    )


def _calibration_map(entry: ConfigEntry) -> dict[str, dict]:
    """Build the per-device calibration map from the hub's devices map.

    Returns ``{device_key: {commodity, unit, scale}}`` for every device that
    carries a *valid* calibration (via :func:`normalize_calibration`, which drops
    a ``none``/unknown commodity or an out-of-range unit). Used both to capture
    the coordinator's setup snapshot and to detect a change in the update
    listener; comparing the normalized maps means only a real calibration change
    (never a routine devices-map upsert) is treated as a change.
    """
    result: dict[str, dict] = {}
    for device_key, record in entry.data.get(CONF_DEVICES, {}).items():
        if not isinstance(record, dict):
            continue
        calibration = normalize_calibration(record.get(DEVICE_CALIBRATION))
        if calibration is not None:
            result[device_key] = calibration
    return result


async def _async_load_library(
    hass: HomeAssistant,
) -> tuple[Registry, set[str]]:
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

    def new_device_callback(device_key: str, model: str) -> None:
        """Dispatch the hub-level new-device signal for a newly observed device.

        The coordinator only invokes this when discovery is enabled and the
        device is genuinely new (its ``is_new`` check dedupes), so the platform
        listeners can add the nested device + its entities directly.

        In addition to wiring up the device, a genuinely-new device (one absent
        from the persisted ``entry.data[CONF_DEVICES]`` map) raises an in-app
        persistent notification. ``entry.data[CONF_DEVICES]`` is the restart-safe
        "ever-adopted" record, so a device already in it is a known device
        re-observed after a restart/reload (``coordinator.devices`` starts empty)
        — NOT genuinely new — and is not re-notified. This boolean is captured
        *before* the dispatch, which schedules the deferred ``async_upsert_device``
        that adds the key to the map.
        """
        is_new_device = device_key not in entry.data.get(CONF_DEVICES, {})
        async_dispatcher_send(
            hass, signal_new_device(entry.entry_id), device_key, model
        )
        if is_new_device:
            name = model or device_key
            persistent_notification.async_create(
                hass,
                f"A new device '{name}' (key {device_key}) was added under hub "
                f"'{entry.title}'.",
                title="rtl_433: new device discovered",
                notification_id=(f"{DOMAIN}_new_device_{entry.entry_id}_{device_key}"),
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
    _cleanup_phantom_unknown_device(hass, entry, device_registry)

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
        skip_keys=skip_keys,
    )
    coordinator.new_device_callback = new_device_callback
    coordinator.effective_timeout_resolver = effective_timeout_resolver
    coordinator.effective_clear_delay_resolver = effective_clear_delay_resolver
    # Snapshot the per-device calibration so the update listener can detect a
    # real calibration change (and reload) while ignoring routine devices-map
    # upserts — the same change-vs-snapshot pattern as ``manage_settings``.
    coordinator.calibration_snapshot = _calibration_map(entry)

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
            # Drop the entity platforms' per-device dedup cache and field
            # listeners so the device re-appears cleanly if it transmits again
            # while discovery is on (Clarification #4).
            for remover in list(coordinator.device_removers):
                remover(device_key)

    return True


def _rehome_device_objects(
    hass: HomeAssistant, device_entry: ConfigEntry, hub_entry_id: str
) -> None:
    """Re-home a legacy device entry's registry objects onto the hub entry.

    The 0.1.0 registry devices and entities are owned by a per-device config
    entry. Before that entry can be removed, its device-registry device and all
    of its entities must be re-associated with the hub config entry, otherwise
    removing the legacy entry would delete them (and their history). The device
    identifiers and the entity unique_ids/entity_ids are never touched — only
    *which config entry owns them* changes — so history is preserved.

    For each device-registry device linked to the legacy entry the hub
    ``config_entry_id`` is **added first**, then the legacy one removed, so the
    device is never momentarily orphaned. Then every entity belonging to the
    legacy entry has its ``config_entry_id`` repointed to the hub. The function
    is idempotent: if a device/entity has already been re-homed it simply finds
    nothing left to move.
    """
    if hub_entry_id == device_entry.entry_id:
        return

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    for device in list(dev_reg.devices.values()):
        if device_entry.entry_id in device.config_entries:
            dev_reg.async_update_device(device.id, add_config_entry_id=hub_entry_id)
            dev_reg.async_update_device(
                device.id, remove_config_entry_id=device_entry.entry_id
            )

    for entity in er.async_entries_for_config_entry(ent_reg, device_entry.entry_id):
        ent_reg.async_update_entity(entity.entity_id, config_entry_id=hub_entry_id)


async def _migrate_hub_entry(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    """Consolidate every legacy child device entry into the hub entry.

    The hub entry is the migration anchor. All legacy per-device config entries
    that recorded this hub as their parent (``CONF_HUB_ENTRY_ID``) are folded
    into the hub's ``entry.data[CONF_DEVICES]`` map, their registry objects are
    re-homed onto the hub **before** removal, and the now-obsolete device config
    entries are removed. The end state: only the hub entry remains, its devices
    map carries every device's model/fields/optional timeout override, and the
    re-homed registry devices/entities are owned by the hub.

    Idempotent: re-running finds no remaining children (they were removed) and
    leaves the already-folded map untouched.
    """
    children = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_HUB_ENTRY_ID) == hub_entry.entry_id
        and e.entry_id != hub_entry.entry_id
    ]

    devices = dict(hub_entry.data.get(CONF_DEVICES, {}))
    for child in children:
        device_key = child.data[CONF_DEVICE_KEY]
        model = child.data.get(CONF_MODEL, "")
        fields = sorted(child.options.get(LEGACY_CONF_OBSERVED_FIELDS, []))
        record: dict = {CONF_MODEL: model, DEVICE_FIELDS: fields}
        timeout_override = child.options.get(CONF_AVAILABILITY_TIMEOUT)
        if timeout_override is not None:
            record[DEVICE_TIMEOUT_OVERRIDE] = int(timeout_override)
        clear_delay = child.options.get(DEVICE_MOTION_CLEAR_DELAY)
        if clear_delay is not None:
            record[DEVICE_MOTION_CLEAR_DELAY] = int(clear_delay)
        devices[device_key] = record

        # Re-home registry objects BEFORE the child entry is removed.
        _rehome_device_objects(hass, child, hub_entry.entry_id)

    hass.config_entries.async_update_entry(
        hub_entry, data={**hub_entry.data, CONF_DEVICES: devices}
    )

    for child in children:
        await hass.config_entries.async_remove(child.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry from the 0.1.0 per-device model to the hub model.

    Version 1 (0.1.0) stored each RF device as its own config entry carrying a
    ``CONF_HUB_ENTRY_ID`` back-reference. Version 2 nests all devices under the
    hub entry's ``entry.data[CONF_DEVICES]`` map. This migration consolidates the
    legacy entries in place with entity_ids and history preserved.

    The hub entry is the authoritative anchor: when it migrates it folds every
    legacy child into its devices map, re-homes the children's registry objects
    onto itself, and removes the children. A legacy *device* entry that Home
    Assistant happens to migrate first only re-homes its own registry objects to
    its parent hub (so they survive an early removal) and bumps its version; the
    hub later folds + removes it. Either ordering converges on the same
    invariant, and re-running is safe.
    """
    if entry.version > 2:
        # Downgrade from a future schema is unsupported.
        return False

    if entry.version == 1:
        is_device = entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_DEVICE
        if is_device:
            # A legacy device entry processed on its own: protect its registry
            # objects by re-homing them to the parent hub before anything can
            # remove this entry. The hub migration remains responsible for
            # folding the field/override state and removing this entry.
            hub_id = entry.data.get(CONF_HUB_ENTRY_ID)
            if hub_id:
                _rehome_device_objects(hass, entry, hub_id)
            hass.config_entries.async_update_entry(entry, version=2)
            return True

        # Hub entry: consolidate all children into the devices map.
        await _migrate_hub_entry(hass, entry)
        hass.config_entries.async_update_entry(entry, version=2)

    return True


__all__: list[str] = [
    "async_migrate_entry",
    "async_remove_config_entry_device",
    "async_setup_entry",
    "async_unload_entry",
]
