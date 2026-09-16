"""Config-entry migration and one-time legacy cleanup for the rtl_433 integration.

This module owns everything that exists only to upgrade an install from an older
shape to the current one — it is deliberately separate from the steady-state
lifecycle in ``__init__.py``:

* :func:`async_migrate_entry` — the config-entry ``VERSION`` 1 → 2 migration (the
  0.1.0 per-device-entry model → the receiver model), the minor-version bumps that
  seed user mappings, disable legacy "Last seen" sensors, drop the legacy global
  availability timeout, and strip the retired discovery toggle, and the 2 → 3
  migration (the standalone-receiver model → the location/receiver-subentry model).
* :func:`_migrate_receiver_entry` / :func:`_rehome_device_objects` — fold legacy child
  device entries into the receiver and re-home their registry objects first.
* :func:`_migrate_entry_to_location` / :func:`_migrate_receiver_control_unique_ids` /
  :func:`_migrate_link_field_unique_ids` — the v3 step: give the entry the receiver
  subentry it now needs to load at all, and re-key the two unique_id families whose
  template gained a receiver segment.
* :func:`_cleanup_phantom_unknown_device` /
  :func:`_migrate_motion_event_to_binary_sensor` — idempotent cleanups driven from
  ``async_setup_entry`` on every startup (a pre-fix phantom ``unknown`` device and
  the pre-fix ``event.*_motion`` entity, respectively).
* :func:`_disable_existing_last_seen_sensors` / :func:`_read_legacy_overrides` —
  one-shot helpers used by the minor-version migration steps.
"""

from __future__ import annotations

from typing import Any

from pyrtl_433.library import (
    USER_OVERRIDE_FILENAME,
    event_driven_field_keys,
    normalize_overrides,
)
import yaml

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import repairs
from .const import (
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEVICE_KEY,
    CONF_DEVICES,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_INITIAL_FREQUENCY,
    CONF_MANAGE_SETTINGS,
    CONF_MODEL,
    CONF_PATH,
    CONF_PORT,
    CONF_RECEIVER_ENTRY_ID,
    CONF_USER_MAPPINGS,
    DEVICE_EVENT_TYPES,
    DEVICE_FIELDS,
    DEVICE_MOTION_CLEAR_DELAY,
    DEVICE_TIMEOUT_OVERRIDE,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    LEGACY_DEFAULT_AVAILABILITY_TIMEOUT,
    LOGGER,
    SUBENTRY_TYPE_RECEIVER,
    receiver_identity,
)
from .library import _async_load_library, _merge_entry_library
from .receiver_settings import receiver_subentries

# The 0.1.0 per-device config entries stored the set of observed mapped field
# keys under this literal options key. It is intentionally *not* exported from
# const.py (the v2 model uses ``DEVICE_FIELDS`` inside the receiver devices map); it
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

# The retired per-receiver discovery toggle. Adoption is explicit now, so the key
# gates nothing and is stripped from existing entries by the minor-7 → 8 step.
# It is a literal (not a ``const.py`` export) because the constant no longer
# exists: only entries written by older versions still carry the string.
_RETIRED_DISCOVERY_KEY = "discovery_enabled"

# The ``object_suffix`` (and unique-id tail) of the per-device "Last seen"
# timestamp sensor. It now ships disabled-by-default; the one-time migration
# sweep below disables any already-created instances on existing installs.
_LAST_SEEN_OBJECT_SUFFIX = "last_seen"

# The v2 identity marker for a receiver-owned entity: every radio control, every
# receiver diagnostic sensor and the connectivity sensor was keyed
# ``f"{entry_id}:hub:{object_suffix}"``. A literal rather than a ``const.py``
# export because the spelling is retired -- only entries written by an older
# version still carry it, and the v3 step below is the last thing that reads it.
_LEGACY_RECEIVER_SEGMENT = "hub"

# The ``object_suffix`` tails of the three **link** fields -- the measurements
# that describe one receiver's reception of a sensor rather than the sensor
# itself. Their template gained a receiver segment when a location grew several
# receivers (``entity.field_unique_id``), so v2's three-segment rows have to be
# re-keyed or the rebuilt four-segment entities mint ``_2`` entity_ids beside
# the stranded originals. Frozen literals for the same reason as the marker
# above: these are the tails v2 actually wrote.
_LINK_OBJECT_SUFFIXES = frozenset({"rssi", "snr", _LAST_SEEN_OBJECT_SUFFIX})

# The v2 receiver entry's own connection keys. They describe *one server*, so the
# v3 step moves them onto the receiver subentry it creates and strips them from
# the location's data. ``"secure"`` is spelled literally here exactly as
# ``receiver_settings._receiver_secure`` spells it: the key is frozen storage
# written by the config flow, which keeps its own private constant for it.
_RECEIVER_DATA_KEYS = (
    CONF_HOST,
    CONF_PORT,
    CONF_PATH,
    "secure",
    CONF_MANAGE_SETTINGS,
    CONF_INITIAL_FREQUENCY,
)

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
    ``(DOMAIN, f"{entry_id}:unknown")`` if present. Never touches the receiver device
    or real nested devices. Safe to run on every setup.
    """
    devices = entry.data.get(CONF_DEVICES, {})
    if PHANTOM_DEVICE_KEY in devices:
        cleaned = {k: v for k, v in devices.items() if k != PHANTOM_DEVICE_KEY}
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICES: cleaned}
        )

    phantom = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}:{PHANTOM_DEVICE_KEY}"), entry.entry_id
    )
    if phantom is not None:
        device_registry.async_remove_device(phantom.id)


def _migrate_motion_event_to_binary_sensor(
    hass: HomeAssistant, entry: ConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Remove the orphaned ``event.*_motion`` entity and announce the move.

    Pre-fix versions exposed motion as an ``event.*_motion`` entity; it is now a
    ``binary_sensor.*_motion``. This sweep finds this receiver's ``event``-domain
    registry entries whose unique-id tail is ``:motion`` (unique-id shape
    ``f"{receiver_entry_id}:{device_key}:{object_suffix}"``), removes them, and drops
    the ``motion`` slot from any persisted ``DEVICE_EVENT_TYPES`` so the event
    platform never recreates them. Only when at least one orphaned entity was
    removed is a single, integration-wide repairs issue raised announcing the
    move (so automations referencing the old entity get updated).

    Idempotent and safe on every startup: re-removing an already-removed entity
    finds nothing, the devices-map write only persists when it changes, and the
    issue id is stable so it is never duplicated across receivers or restarts.
    """
    removed_any = False
    removed_device_keys: set[str] = set()
    for ent in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if ent.domain != "event" or not ent.unique_id.endswith(
            f":{_MOTION_OBJECT_SUFFIX}"
        ):
            continue
        # unique_id is ``{receiver_entry_id}:{device_key}:motion``; the middle part is
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
    one-time sweep finds this receiver's ``sensor``-domain registry entries whose
    unique-id tail is ``:last_seen`` (unique-id shape
    ``f"{receiver_entry_id}:{device_key}:{object_suffix}"``) and disables any the user
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

    Resolves the event-driven field keys from this receiver's merged library (shipped
    descriptors plus user mappings) and matches each device's adopted
    ``DEVICE_FIELDS`` against them — the same classification setup uses, but
    without a coordinator (migration runs first).

    The unique_id built here is deliberately the **three-segment** v2 shape, even
    though "Last seen" is now a per-receiver link field with a receiver segment.
    This is a one-time minor-6 sweep over rows that already exist, and it runs
    inside the version-2 ladder — strictly before the v3 step re-keys those rows
    (:func:`_migrate_link_field_unique_ids`) — so the rows it has to find are
    still at three segments. An entry that reaches v3 never runs this step again.
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

    Used only by the one-time minor-version migration to seed each receiver's
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
    hass: HomeAssistant, device_entry: ConfigEntry, receiver_entry_id: str
) -> None:
    """Re-home a legacy device entry's registry objects onto the receiver entry.

    The 0.1.0 registry devices and entities are owned by a per-device config
    entry. Before that entry can be removed, its device-registry device and all
    of its entities must be re-associated with the receiver config entry, otherwise
    removing the legacy entry would delete them (and their history). The device
    identifiers and the entity unique_ids/entity_ids are never touched — only
    *which config entry owns them* changes — so history is preserved.

    **The entity pass runs first, and the order is load-bearing.** Moving a device
    with ``new_config_entry_id`` makes the entity registry *delete* every entity
    still pointing at the device's previous config entry
    (``entity_registry.async_device_modified``): an entity whose owner no longer
    matches its device's owner is treated as left behind by the move. Re-homing
    the devices first therefore destroys the very entities the second pass exists
    to move, and the second pass then finds nothing. Re-homing the entities first
    means that by the time the device moves, every entity on it already carries
    the receiver entry id, so the deletion branch matches none of them.

    That failure used to be invisible: a successful setup immediately recreated
    the rows under the same unique_ids, so history appeared to survive by luck
    rather than because the re-home worked. It stopped being invisible once an
    unmigrated entry was refused setup, which is why the order is now asserted by
    a test that checks the re-homed rows are the *same* registry rows (same
    registry id and ``entity_id``), not recreated ones.

    The function is idempotent: if a device/entity has already been re-homed it
    simply finds nothing left to move.
    """
    if receiver_entry_id == device_entry.entry_id:
        return

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    # Snapshot both with ``list(...)``: the registry's per-entry views are live,
    # and each update removes the row from the very list being iterated.
    for entity in list(
        er.async_entries_for_config_entry(ent_reg, device_entry.entry_id)
    ):
        ent_reg.async_update_entity(entity.entity_id, config_entry_id=receiver_entry_id)

    for device in list(
        dr.async_entries_for_config_entry(dev_reg, device_entry.entry_id)
    ):
        dev_reg.async_update_device(device.id, new_config_entry_id=receiver_entry_id)


def _migrate_receiver_control_unique_ids(
    entry: ConfigEntry, receiver_id: str, entity_registry: er.EntityRegistry
) -> None:
    """Re-key this entry's ``:hub:`` entities onto the receiver-scoped template.

    v2 had one server per config entry, so every receiver-owned entity -- the
    radio controls (``number`` / ``select`` / ``switch``), the receiver
    diagnostic sensors and the connectivity binary sensor -- was keyed
    ``f"{entry_id}:hub:{object_suffix}"``. A location can now hold several
    receivers, so the template carries the receiver's subentry id
    (:func:`~custom_components.rtl_433.const.receiver_identity`) and two
    receivers no longer mint the same id.

    Every row is mutated in place with ``async_update_entity``, never removed and
    recreated: recorder history, statistics, dashboards and automations are all
    keyed on ``entity_id``, and only the ``unique_id`` changes here. The device
    the row hangs off is left alone -- the platforms re-point it onto the new
    receiver device when they rebuild, because ``async_get_or_create`` updates an
    existing row's ``device_id`` and ``config_subentry_id`` in place.

    ``hub`` was never a reserved token in v2, so a device whose ``device_key`` is
    literally ``hub`` would have minted colliding three-segment ids. Those are
    device fields, not receiver controls, and the devices map is what says which:
    when it holds a ``hub`` key the sweep does nothing rather than mangling the
    user's sensor.

    Idempotent: a re-run finds no ``:hub:`` rows left, because the rewritten ids
    carry the ``receiver`` marker instead.
    """
    if _LEGACY_RECEIVER_SEGMENT in entry.data.get(CONF_DEVICES, {}):
        LOGGER.warning(
            "Not re-keying the receiver controls of %s: it has adopted a device "
            "keyed %r, whose entities are indistinguishable from the v2 receiver "
            "controls",
            entry.title,
            _LEGACY_RECEIVER_SEGMENT,
        )
        return

    old_prefix = f"{entry.entry_id}:{_LEGACY_RECEIVER_SEGMENT}:"
    new_prefix = f"{receiver_identity(entry.entry_id, receiver_id)}:"
    for regent in list(
        er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    ):
        if not regent.unique_id.startswith(old_prefix):
            continue
        # Sliced by ``len(old_prefix)`` rather than split on ":" so the suffix is
        # carried across byte-for-byte, matching the frozen template exactly.
        object_suffix = regent.unique_id[len(old_prefix) :]
        entity_registry.async_update_entity(
            regent.entity_id, new_unique_id=f"{new_prefix}{object_suffix}"
        )


def _migrate_link_field_unique_ids(
    entry: ConfigEntry, receiver_id: str, entity_registry: er.EntityRegistry
) -> None:
    """Give this entry's ``rssi`` / ``snr`` / ``last_seen`` rows a receiver segment.

    Every other device field is **unioned** across a location's receivers and so
    keeps v2's three-segment ``f"{entry_id}:{device_key}:{object_suffix}"``
    byte-for-byte -- the whole reason the v2 → v3 upgrade rewrites so little. The
    three link fields are the exception: "how well does *this* receiver hear the
    sensor" is a different measurement per receiver, so their template gained a
    fourth segment. A v2 row left at three segments would be stranded (never
    provided again) while the rebuilt entity took a ``_2`` entity_id beside it,
    which is exactly the history loss this migration exists to prevent.

    The entry's single receiver is the unambiguous owner of every existing row --
    v2 had exactly one server -- so the re-key is a pure insert of
    ``receiver_id`` before the suffix, done in place so ``entity_id`` survives.

    Runs *after* :func:`_migrate_receiver_control_unique_ids`, which is what makes
    the segment count sufficient to recognise a device field: a ``device_key``
    cannot contain ``:`` (``pyrtl_433.naming.safe_token`` maps it to ``_``) and a
    config entry id is a ULID, so a three-segment id whose tail is a link suffix
    is a device field once the receiver-owned rows have four segments.

    Not a ``device_replace`` re-key, despite touching a nested device's entities:
    that helper is the only sanctioned place to change the ``device_key`` a row
    *carries*, and this changes none — it rewrites the **template** those rows
    are built from, which is what a schema migration is, and the only place a
    version bump can do it is here.

    Idempotent: a re-run sees four segments and skips every row.
    """
    prefix = f"{entry.entry_id}:"
    for regent in list(
        er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    ):
        if not regent.unique_id.startswith(prefix):
            continue
        parts = regent.unique_id.split(":")
        if len(parts) != 3 or parts[2] not in _LINK_OBJECT_SUFFIXES:
            continue
        entity_registry.async_update_entity(
            regent.entity_id,
            new_unique_id=f"{parts[0]}:{parts[1]}:{receiver_id}:{parts[2]}",
        )


def _migrate_entry_to_location(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Give a v2 receiver entry the receiver subentry that makes it a location.

    A v2 entry *was* one rtl_433 server: its data carried the connection target
    and its ``unique_id`` was that server's identity. A v3 entry is a **location**
    that holds one receiver subentry per server, and an entry with no receiver
    subentry cannot be set up at all -- so this is the step that makes every
    existing install load again.

    The conversion is deliberately **non-merging**: each existing entry becomes
    its own location, keeping its ``entry_id``. That is not a convenience, it is
    the whole reason the upgrade is loss-free. Device identity is scoped by the
    *location* entry id, so an entry that keeps its id re-emits
    ``f"{entry_id}:{device_key}:{object_suffix}"`` and
    ``(DOMAIN, f"{entry_id}:{device_key}")`` byte-for-byte, and the v2 hub device
    identifier ``(DOMAIN, entry_id)`` is already the v3 *location* device
    identifier. Folding two entries into one location would instead force two
    histories onto one unique_id, and the integration cannot know which of a
    user's separate entries are co-located. A user who wants that opts in later
    (see :func:`~custom_components.rtl_433.device_replace.async_consolidate_location`).

    The connection keys move onto the subentry and the server identity becomes
    the subentry's ``unique_id``, because both describe the server; the location
    is left with no ``unique_id`` of its own, exactly as the config flow now
    creates one. Everything that describes *sensors* -- the devices map, the
    ignore list, the user mappings, the availability options -- stays on the
    entry untouched.

    Returns the receiver's subentry id. Idempotent: an entry that already has a
    receiver subentry (a re-run, or an entry written at v3) keeps it and is not
    rewritten.
    """
    existing = receiver_subentries(entry)
    if existing:
        return existing[0].subentry_id

    subentry = ConfigSubentry(
        data={key: entry.data[key] for key in _RECEIVER_DATA_KEYS if key in entry.data},
        subentry_type=SUBENTRY_TYPE_RECEIVER,
        title=entry.title,
        unique_id=entry.unique_id,
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry.subentry_id


async def _migrate_receiver_entry(
    hass: HomeAssistant, receiver_entry: ConfigEntry
) -> None:
    """Consolidate every legacy child device entry into the receiver entry.

    The receiver entry is the migration anchor. All legacy per-device config entries
    that recorded this receiver as their parent (``CONF_RECEIVER_ENTRY_ID``) are folded
    into the receiver's ``entry.data[CONF_DEVICES]`` map, their registry objects are
    re-homed onto the receiver **before** removal, and the now-obsolete device config
    entries are removed. The end state: only the receiver entry remains, its devices
    map carries every device's model/fields/optional timeout override, and the
    re-homed registry devices/entities are owned by the receiver.

    Idempotent: re-running finds no remaining children (they were removed) and
    leaves the already-folded map untouched.
    """
    children = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_RECEIVER_ENTRY_ID) == receiver_entry.entry_id
        and e.entry_id != receiver_entry.entry_id
    ]

    devices = dict(receiver_entry.data.get(CONF_DEVICES, {}))
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
        _rehome_device_objects(hass, child, receiver_entry.entry_id)

    hass.config_entries.async_update_entry(
        receiver_entry, data={**receiver_entry.data, CONF_DEVICES: devices}
    )

    for child in children:
        await hass.config_entries.async_remove(child.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry up to the current location/receiver schema.

    Version 1 (0.1.0) stored each RF device as its own config entry carrying a
    ``CONF_RECEIVER_ENTRY_ID`` back-reference. Version 2 nests all devices under the
    receiver entry's ``entry.data[CONF_DEVICES]`` map. This migration consolidates the
    legacy entries in place with entity_ids and history preserved.

    The receiver entry is the authoritative anchor: when it migrates it folds every
    legacy child into its devices map, re-homes the children's registry objects
    onto itself, and removes the children. A legacy *device* entry that Home
    Assistant happens to migrate first only re-homes its own registry objects to
    its parent receiver (so they survive an early removal) and bumps its version; the
    receiver later folds + removes it. Either ordering converges on the same
    invariant, and re-running is safe.

    Version 2 minor 2 additionally seeds the receiver's
    ``entry.data[CONF_USER_MAPPINGS]`` from any pre-existing
    ``<config>/rtl_433_mappings.yaml`` (read once, in the executor, never
    modified or deleted). Version 2 minor 3 disables any already-created
    "Last seen" sensors, which now ship disabled-by-default. Version 2 minor 4
    drops a receiver availability timeout still pinned to the legacy global default
    (600s) so the new device-class defaults apply. Version 2 minor 5 rewrites any
    already-persisted doorbell ``event_types`` from the raw ``"0"``/``"1"`` strings
    to the standardized ``"ring"``/``"secret_knock"`` types. Entries created at the
    current minor version skip these steps; new receivers added after the upgrade start
    with no mappings and their "Last seen" sensors already disabled. Version 2
    minor 6 re-enables the "Last seen" sensor for event-driven devices (which now
    never expire, making it their only freshness signal) — only instances the
    integration disabled, not ones the user disabled. Version 2 minor 7 repeats the
    minor-4 cleanup: it drops a receiver availability timeout still pinned to the legacy
    global default (600s) that the options flow re-persisted on save, which masked
    the device-class defaults again (expiring event-driven devices); the options
    flow no longer writes that sentinel, so this heal is final. Version 2 minor 8
    strips the retired ``discovery_enabled`` key, which no longer gates anything
    now that a device is only created once the user adopts it.

    Version 3 re-homes the whole model: a config entry stops being one rtl_433
    server and becomes a **location** holding one **receiver subentry** per
    server. Each existing entry is converted into its own location, keeping its
    ``entry_id``, with its connection target and server identity moved onto a
    single new receiver subentry
    (:func:`_migrate_entry_to_location`). The conversion is deliberately
    **non-merging** -- two of a user's entries are never folded together, because
    the integration cannot know which are co-located and folding them would force
    two histories onto one unique_id. Two entity families are re-keyed because
    their template gained a receiver segment: the receiver-owned controls and
    diagnostics (:func:`_migrate_receiver_control_unique_ids`) and the per-receiver
    link fields (:func:`_migrate_link_field_unique_ids`). Everything else --
    every unioned device field's unique_id, every device-registry identifier, and
    the ``via_device`` link -- is byte-identical to v2 and is deliberately left
    alone. The ladder is forward-only: a v3 entry walks none of the v2 steps, and
    an entry from a *newer* schema is refused rather than downgraded.
    """
    if entry.version > 3:
        # Downgrade from a future schema is unsupported.
        return False

    if entry.version == 1:
        is_device = entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_DEVICE
        if is_device:
            # A legacy device entry processed on its own: protect its registry
            # objects by re-homing them to the parent receiver before anything can
            # remove this entry. The receiver migration remains responsible for
            # folding the field/override state and removing this entry.
            receiver_id = entry.data.get(CONF_RECEIVER_ENTRY_ID)
            if receiver_id:
                _rehome_device_objects(hass, entry, receiver_id)
            hass.config_entries.async_update_entry(entry, version=2, minor_version=2)
            return True

        # Receiver entry: consolidate all children into the devices map.
        await _migrate_receiver_entry(hass, entry)

    if entry.version <= 2:
        # The version-2 minor ladder. Gated on the major version because each
        # step below tests only ``minor_version``, and a v3 entry starts again
        # at minor 1 -- an ungated ladder would read that as an install stuck
        # at v2 minor 1 and walk the whole ladder again, re-disabling sensors
        # the user re-enabled and writing ``version=2`` back over a v3 entry.
        if entry.version < 2 or (entry.minor_version or 1) < 2:
            # Seed this receiver's stored user mappings from the legacy file (read only
            # during migration). Each entry migrates independently, so every
            # existing receiver gets its own copy of the file contents.
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
            # explicit receiver option would mask those per-class defaults, so drop that
            # exact value and let the class default apply. A receiver timeout the user
            # deliberately set to anything else is preserved.
            new_options = dict(entry.options)
            if (
                new_options.get(CONF_AVAILABILITY_TIMEOUT)
                == LEGACY_DEFAULT_AVAILABILITY_TIMEOUT
            ):
                del new_options[CONF_AVAILABILITY_TIMEOUT]
                LOGGER.info(
                    "Removed the old %ss availability timeout from receiver %s; "
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
            # defaults apply again; a receiver timeout the user deliberately set to anything
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
                    "options of receiver %s; per-device-type defaults now apply",
                    LEGACY_DEFAULT_AVAILABILITY_TIMEOUT,
                    entry.title,
                )
            hass.config_entries.async_update_entry(
                entry, options=new_options, version=2, minor_version=7
            )

        if (entry.minor_version or 1) < 8:
            # Discovery stopped being a toggle: every heard device waits in the
            # coordinator's pending list until the user adopts it, so the per-receiver
            # ``discovery_enabled`` flag gates nothing. Strip it from both data and
            # options so no stale value survives into diagnostics or a config-entry
            # export. Adopted devices and their settings are untouched.
            #
            # Deliberately narrow: the rebuilt mappings drop that one key and
            # nothing else, so ``CONF_DEVICES`` -- every adopted device, its
            # per-device overrides and its calibration -- survives byte-for-byte.
            #
            # The strip and the version bump are one migration, so they are one
            # write, as in the minor-4 and minor-7 steps above. ``data`` / ``options``
            # are passed only when the key was actually there: the vast majority of
            # entries reaching this step never carried it (it was optional, and
            # every entry gets the bump), and rewriting mappings that did not change
            # fires the update listener with a fresh ``entry.data`` for nothing --
            # which on a loaded receiver is a chance to reload for nothing.
            changes: dict[str, Any] = {}
            if _RETIRED_DISCOVERY_KEY in entry.data:
                changes["data"] = {
                    k: v for k, v in entry.data.items() if k != _RETIRED_DISCOVERY_KEY
                }
            if _RETIRED_DISCOVERY_KEY in entry.options:
                changes["options"] = {
                    k: v
                    for k, v in entry.options.items()
                    if k != _RETIRED_DISCOVERY_KEY
                }
            hass.config_entries.async_update_entry(
                entry, **changes, version=2, minor_version=8
            )

    if entry.version < 3:
        # A v2 entry *was* one rtl_433 server. Give it the receiver subentry
        # that makes it a location holding that one server -- without it the
        # entry cannot be set up at all -- and re-key the two unique_id
        # families whose template gained a receiver segment. Every other
        # identity is byte-identical to v2, because the entry keeps its
        # ``entry_id`` and device identity is scoped by the location.
        #
        # Deliberately non-merging: a user's separate entries each become
        # their own location, so no two histories are ever forced onto one
        # unique_id by the upgrade itself.
        receiver_id = _migrate_entry_to_location(hass, entry)
        entity_registry = er.async_get(hass)
        # Controls first: that is what leaves the receiver-owned rows at four
        # segments, so the link sweep can recognise a device field by segment
        # count alone.
        _migrate_receiver_control_unique_ids(entry, receiver_id, entity_registry)
        _migrate_link_field_unique_ids(entry, receiver_id, entity_registry)
        # One write for the strip and the bump, as in the minor steps above.
        # The connection keys are gone from the location's data (they live on
        # the receiver subentry now) and so is the server's ``unique_id``: a
        # location is a user-named grouping with no hardware identity.
        hass.config_entries.async_update_entry(
            entry,
            data={
                key: value
                for key, value in entry.data.items()
                if key not in _RECEIVER_DATA_KEYS
            },
            unique_id=None,
            version=3,
            minor_version=1,
        )

    return True
