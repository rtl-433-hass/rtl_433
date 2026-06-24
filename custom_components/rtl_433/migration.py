"""Config-entry migration and one-time legacy cleanup for the rtl_433 integration.

This module owns everything that exists only to upgrade an install from an older
shape to the current one — it is deliberately separate from the steady-state
lifecycle in ``__init__.py``:

* :func:`async_migrate_entry` — the config-entry ``VERSION`` 1 → 2 migration (the
  0.1.0 per-device-entry model → the hub model) plus the minor-version bumps that
  seed user mappings, disable legacy "Last seen" sensors, and drop the legacy
  global availability timeout.
* :func:`_migrate_hub_entry` / :func:`_rehome_device_objects` — fold legacy child
  device entries into the hub and re-home their registry objects first.
* :func:`_cleanup_phantom_unknown_device` /
  :func:`_migrate_motion_event_to_binary_sensor` — idempotent cleanups driven from
  ``async_setup_entry`` on every startup (a pre-fix phantom ``unknown`` device and
  the pre-fix ``event.*_motion`` entity, respectively).
* :func:`_disable_existing_last_seen_sensors` / :func:`_read_legacy_overrides` —
  one-shot helpers used by the minor-version migration steps.
"""

from __future__ import annotations

import yaml

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import repairs
from .const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DEVICES,
    CONF_ENTRY_TYPE,
    CONF_HUB_ENTRY_ID,
    CONF_MODEL,
    CONF_USER_MAPPINGS,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    LEGACY_DEFAULT_AVAILABILITY_TIMEOUT,
    LOGGER,
)
from .library import _async_load_library, _merge_entry_library
from .mapping import (
    USER_OVERRIDE_FILENAME,
    event_driven_field_keys,
    normalize_overrides,
)

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

# The ``object_suffix`` (and unique-id tail) of the pre-fix ``event.*_motion``
# entity that has since moved to a ``binary_sensor.*_motion``. Used by the
# migration sweep below to find the orphaned event entities and to drop the
# matching ``DEVICE_EVENT_TYPES`` slot so the event platform never recreates it.
_MOTION_OBJECT_SUFFIX = "motion"

# The ``object_suffix`` (and unique-id tail) of the per-device "Last seen"
# timestamp sensor. It now ships disabled-by-default; the one-time migration
# sweep below disables any already-created instances on existing installs.
_LAST_SEEN_OBJECT_SUFFIX = "last_seen"

# The doorbell ``event`` entity (Honeywell ActivLink ``secret_knock`` field) used
# to fire the stringified raw value (``"0"``/``"1"``) as its ``event_type`` and
# auto-populated ``event_types`` from those raw values. It now fires the
# standardized Home Assistant doorbell types — ``"ring"`` for a regular press and
# ``"secret_knock"`` for a secret knock. The persisted ``DEVICE_EVENT_TYPES`` dict
# is keyed by field_key, so the doorbell slot is found under this key, and any
# already-persisted raw values are rewritten with the map below.
_DOORBELL_FIELD_KEY = "secret_knock"
_DOORBELL_EVENT_MAP = {"0": "ring", "1": "secret_knock"}


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


def _migrate_motion_event_to_binary_sensor(
    hass: HomeAssistant, entry: ConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Remove the orphaned ``event.*_motion`` entity and announce the move.

    Pre-fix versions exposed motion as an ``event.*_motion`` entity; it is now a
    ``binary_sensor.*_motion``. This sweep finds this hub's ``event``-domain
    registry entries whose unique-id tail is ``:motion`` (unique-id shape
    ``f"{hub_entry_id}:{device_key}:{object_suffix}"``), removes them, and drops
    the ``motion`` slot from any persisted ``DEVICE_EVENT_TYPES`` so the event
    platform never recreates them. Only when at least one orphaned entity was
    removed is a single, integration-wide repairs issue raised announcing the
    move (so automations referencing the old entity get updated).

    Idempotent and safe on every startup: re-removing an already-removed entity
    finds nothing, the devices-map write only persists when it changes, and the
    issue id is stable so it is never duplicated across hubs or restarts.
    """
    removed_any = False
    removed_device_keys: set[str] = set()
    for ent in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if ent.domain != "event" or not ent.unique_id.endswith(
            f":{_MOTION_OBJECT_SUFFIX}"
        ):
            continue
        # unique_id is ``{hub_entry_id}:{device_key}:motion``; the middle part is
        # the device_key (device_keys may themselves contain ``:``).
        parts = ent.unique_id.split(":")
        if len(parts) >= 3:
            removed_device_keys.add(":".join(parts[1:-1]))
        entity_registry.async_remove(ent.entity_id)
        removed_any = True

    # Drop the ``motion`` event-type slot from the persisted devices map so the
    # event platform does not recreate the entity on the next build.
    devices = entry.data.get(CONF_DEVICES, {})
    new_devices: dict = {}
    changed = False
    for device_key, record in devices.items():
        if not isinstance(record, dict) or _MOTION_OBJECT_SUFFIX not in record.get(
            DEVICE_EVENT_TYPES, {}
        ):
            new_devices[device_key] = record
            continue
        new_record = dict(record)
        new_event_types = {
            k: v
            for k, v in record[DEVICE_EVENT_TYPES].items()
            if k != _MOTION_OBJECT_SUFFIX
        }
        new_record[DEVICE_EVENT_TYPES] = new_event_types
        new_devices[device_key] = new_record
        changed = True

    if changed:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICES: new_devices}
        )

    if removed_any:
        repairs.async_raise_motion_moved(hass)


def _migrate_doorbell_event_types(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rewrite persisted doorbell ``event_types`` from raw values to the mapped types.

    The doorbell ``event`` entity historically fired (and persisted) the
    stringified raw value of its field — ``"0"`` for a regular press and ``"1"``
    for a secret knock. It now fires the standardized Home Assistant doorbell
    types ``"ring"`` and ``"secret_knock"``, so any already-persisted raw
    ``DEVICE_EVENT_TYPES`` entries for the doorbell field must be rewritten to
    match, otherwise device-trigger subtypes would still reference the stale
    numeric values.

    For every device record carrying the doorbell field (``_DOORBELL_FIELD_KEY``)
    in its persisted ``DEVICE_EVENT_TYPES`` dict, the stored list is rewritten as
    ``sorted({_DOORBELL_EVENT_MAP.get(v, v) for v in old})``: known raw values are
    mapped, anything else (including values already equal to ``"ring"`` /
    ``"secret_knock"``) passes through unchanged, and the result is sorted to
    match ``async_upsert_event_types``' stored-sorted convention. The record and
    its inner event-types dict are deep-copied before mutation so the original
    ``entry.data`` is never mutated in place.

    Unlike :func:`_migrate_motion_event_to_binary_sensor`, this migration removes
    **no** entity and raises **no** repairs issue: the doorbell entity's
    ``unique_id`` / ``object_suffix`` are unchanged — only the persisted
    ``event_type`` strings change — so the entity is preserved as-is.

    Idempotent: the map only rewrites the recognized raw values ``"0"``/``"1"``
    and leaves already-mapped or unknown values untouched, so a second run
    produces ``new == old`` for every record and writes nothing. The devices-map
    write only occurs when at least one record actually changed.
    """
    devices = entry.data.get(CONF_DEVICES, {})
    new_devices: dict = {}
    changed = False
    for device_key, record in devices.items():
        if not isinstance(record, dict) or _DOORBELL_FIELD_KEY not in record.get(
            DEVICE_EVENT_TYPES, {}
        ):
            new_devices[device_key] = record
            continue
        old = record[DEVICE_EVENT_TYPES][_DOORBELL_FIELD_KEY]
        new = sorted({_DOORBELL_EVENT_MAP.get(v, v) for v in old})
        if new == old:
            new_devices[device_key] = record
            continue
        new_record = dict(record)
        new_event_types = dict(record[DEVICE_EVENT_TYPES])
        new_event_types[_DOORBELL_FIELD_KEY] = new
        new_record[DEVICE_EVENT_TYPES] = new_event_types
        new_devices[device_key] = new_record
        changed = True

    if changed:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICES: new_devices}
        )


def _disable_existing_last_seen_sensors(
    hass: HomeAssistant, entry: ConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Disable already-created per-device "Last seen" sensors.

    The "Last seen" sensor now ships disabled-by-default, but
    ``entity_registry_enabled_default`` only takes effect when an entity is first
    *created*, so existing installs keep their already-enabled instances. This
    one-time sweep finds this hub's ``sensor``-domain registry entries whose
    unique-id tail is ``:last_seen`` (unique-id shape
    ``f"{hub_entry_id}:{device_key}:{object_suffix}"``) and disables any the user
    has not already disabled, marking them ``RegistryEntryDisabler.INTEGRATION``.

    Driven once from :func:`async_migrate_entry` behind the minor-version 3 bump
    so a sensor the user later re-enables is never re-disabled on restart.
    """
    for ent in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if (
            ent.domain != "sensor"
            or not ent.unique_id.endswith(f":{_LAST_SEEN_OBJECT_SUFFIX}")
            or ent.disabled_by is not None
        ):
            continue
        entity_registry.async_update_entity(
            ent.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
        )


async def _enable_last_seen_for_event_driven_devices(
    hass: HomeAssistant, entry: ConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Re-enable already-created "Last seen" sensors for event-driven devices.

    Event-driven devices (open/close/motion/button/doorbell) now never expire,
    so their availability no longer signals freshness and the "Last seen"
    timestamp becomes their only such signal — it now ships enabled-by-default
    for them. ``entity_registry_enabled_default`` only affects entities at
    *creation*, so this one-time sweep re-enables the already-created instances
    the integration previously disabled (minor 3) for the devices the merged
    library classifies event-driven. Sensors a user disabled
    (``disabled_by != INTEGRATION``) are left untouched.

    Resolves the event-driven field keys from this hub's merged library (shipped
    descriptors plus user mappings) and matches each device's adopted
    ``DEVICE_FIELDS`` against them — the same classification setup uses, but
    without a coordinator (migration runs first).
    """
    devices = entry.data.get(CONF_DEVICES, {})
    if not devices:
        return
    shipped_registry, shipped_skip_keys = await _async_load_library(hass)
    registry, _ = _merge_entry_library(hass, entry, shipped_registry, shipped_skip_keys)
    event_driven_keys = event_driven_field_keys(registry)
    if not event_driven_keys:
        return

    for device_key, device_cfg in devices.items():
        fields = set(device_cfg.get(DEVICE_FIELDS, []) or [])
        if event_driven_keys.isdisjoint(fields):
            continue
        unique_id = f"{entry.entry_id}:{device_key}:{_LAST_SEEN_OBJECT_SUFFIX}"
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            continue
        ent = entity_registry.async_get(entity_id)
        if ent is not None and ent.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            entity_registry.async_update_entity(entity_id, disabled_by=None)


def _read_legacy_overrides(path: str) -> dict:
    """Read + normalize the legacy ``rtl_433_mappings.yaml`` file (sync, executor).

    Used only by the one-time minor-version migration to seed each hub's
    ``entry.data[CONF_USER_MAPPINGS]`` from any pre-existing file. Returns an
    empty dict (never raises) when the file is missing, unreadable, malformed,
    empty, or not a mapping, so a bad/absent file simply migrates to ``{}``. The
    file is only ever read here; it is never modified or deleted.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            parsed = yaml.safe_load(handle)
    except FileNotFoundError:
        return {}
    except OSError, yaml.YAMLError:
        LOGGER.warning(
            "Could not read legacy mappings file %s; migrating to empty mappings",
            path,
            exc_info=True,
        )
        return {}

    if parsed is None or not isinstance(parsed, dict):
        return {}
    return normalize_overrides(parsed)


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

    Version 2 minor 2 additionally seeds the hub's
    ``entry.data[CONF_USER_MAPPINGS]`` from any pre-existing
    ``<config>/rtl_433_mappings.yaml`` (read once, in the executor, never
    modified or deleted). Version 2 minor 3 disables any already-created
    "Last seen" sensors, which now ship disabled-by-default. Version 2 minor 4
    drops a hub availability timeout still pinned to the legacy global default
    (600s) so the new device-class defaults apply. Version 2 minor 5 rewrites any
    already-persisted doorbell ``event_types`` from the raw ``"0"``/``"1"`` strings
    to the standardized ``"ring"``/``"secret_knock"`` types. Entries created at the
    current minor version skip these steps; new hubs added after the upgrade start
    with no mappings and their "Last seen" sensors already disabled. Version 2
    minor 6 re-enables the "Last seen" sensor for event-driven devices (which now
    never expire, making it their only freshness signal) — only instances the
    integration disabled, not ones the user disabled. Version 2 minor 7 repeats the
    minor-4 cleanup: it drops a hub availability timeout still pinned to the legacy
    global default (600s) that the options flow re-persisted on save, which masked
    the device-class defaults again (expiring event-driven devices); the options
    flow no longer writes that sentinel, so this heal is final.
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
            hass.config_entries.async_update_entry(entry, version=2, minor_version=2)
            return True

        # Hub entry: consolidate all children into the devices map.
        await _migrate_hub_entry(hass, entry)

    if entry.version < 2 or (entry.minor_version or 1) < 2:
        # Seed this hub's stored user mappings from the legacy file (read only
        # during migration). Each entry migrates independently, so every
        # existing hub gets its own copy of the file contents.
        overrides = await hass.async_add_executor_job(
            _read_legacy_overrides, hass.config.path(USER_OVERRIDE_FILENAME)
        )
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_USER_MAPPINGS: overrides},
            version=2,
            minor_version=2,
        )

    if (entry.minor_version or 1) < 3:
        # The "Last seen" sensor now ships disabled-by-default; disable any
        # already-created instances so existing installs match. Gated by the
        # minor-version bump so a user who later re-enables one keeps it.
        _disable_existing_last_seen_sensors(hass, entry, er.async_get(hass))
        hass.config_entries.async_update_entry(entry, version=2, minor_version=3)

    if (entry.minor_version or 1) < 4:
        # The availability timeout grew device-class-aware defaults (never-expire
        # for event-driven door/motion/button sensors, the periodic default for
        # the rest). Entries that persisted the old global default (600s) as an
        # explicit hub option would mask those per-class defaults, so drop that
        # exact value and let the class default apply. A hub timeout the user
        # deliberately set to anything else is preserved.
        new_options = dict(entry.options)
        if (
            new_options.get(CONF_AVAILABILITY_TIMEOUT)
            == LEGACY_DEFAULT_AVAILABILITY_TIMEOUT
        ):
            del new_options[CONF_AVAILABILITY_TIMEOUT]
            LOGGER.info(
                "Removed the old %ss availability timeout from hub %s; "
                "per-device-type defaults now apply",
                LEGACY_DEFAULT_AVAILABILITY_TIMEOUT,
                entry.title,
            )
        hass.config_entries.async_update_entry(
            entry, options=new_options, version=2, minor_version=4
        )

    if (entry.minor_version or 1) < 5:
        # The doorbell event entity now fires the standardized Home Assistant
        # doorbell types instead of the raw ``"0"``/``"1"`` strings. Rewrite any
        # already-persisted raw doorbell ``event_types`` to the mapped values so
        # stored device-trigger subtypes stay consistent. Idempotent and removes
        # no entity (the doorbell unique_id/object_suffix are unchanged).
        _migrate_doorbell_event_types(hass, entry)
        hass.config_entries.async_update_entry(entry, version=2, minor_version=5)

    if (entry.minor_version or 1) < 6:
        # Event-driven devices now never expire, so their "Last seen" sensor
        # ships enabled-by-default (their only freshness signal). Re-enable the
        # already-created instances the integration disabled at minor 3 for those
        # devices, leaving user-disabled ones alone.
        await _enable_last_seen_for_event_driven_devices(
            hass, entry, er.async_get(hass)
        )
        hass.config_entries.async_update_entry(entry, version=2, minor_version=6)

    if (entry.minor_version or 1) < 7:
        # The options flow used to re-persist the plain default availability
        # timeout into ``entry.options`` on every save, which re-masked the
        # device-class defaults that minor 4 had cleared — so event-driven devices
        # (doorbells/motion/contacts) wrongly expired at the periodic timeout
        # again, taking their battery/RSSI/SNR/noise sensors unavailable. Re-strip
        # that exact sentinel (identical to the minor-4 cleanup) so the class
        # defaults apply again; a hub timeout the user deliberately set to anything
        # else is preserved. The options flow no longer writes the sentinel, so the
        # entry cannot re-acquire it after this one-time heal.
        new_options = dict(entry.options)
        if (
            new_options.get(CONF_AVAILABILITY_TIMEOUT)
            == LEGACY_DEFAULT_AVAILABILITY_TIMEOUT
        ):
            del new_options[CONF_AVAILABILITY_TIMEOUT]
            LOGGER.info(
                "Removed the default %ss availability timeout re-saved into the "
                "options of hub %s; per-device-type defaults now apply",
                LEGACY_DEFAULT_AVAILABILITY_TIMEOUT,
                entry.title,
            )
        hass.config_entries.async_update_entry(
            entry, options=new_options, version=2, minor_version=7
        )

    return True
