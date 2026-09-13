"""In-place re-key of registry identity: a new ``device_key``, or a new location.

Cheap 433 MHz sensors usually generate a fresh transmitter id when their batteries
are pulled, so the same physical hardware starts arriving under a new
``device_key`` — normally as a *pending* candidate the user has not added (and,
if they do add it, a brand-new device-registry row with brand-new entities and no
history), while the original goes permanently unavailable. This module is the
**only** sanctioned place that rewrites a nested device's identity: it re-points
an existing device (and every entity hanging off it) from ``old_key`` onto
``new_key`` so ``entity_id`` — and therefore recorder history, statistics,
dashboards and automations — carries straight through.

The step order in :func:`async_replace_device` is load-bearing:

1. **Free the duplicate first.** The entity registry refuses a ``unique_id``
   another row already holds, and by the time a user runs a replace, Home
   Assistant has usually already built a full set of entities for ``new_key``.
   Those throwaway rows must be removed *before* any survivor is rewritten;
   getting this backwards makes :meth:`async_update_entity` raise part-way and
   leaves the device half-migrated.
2. **Update the survivors, never recreate them.** Recorder rows are keyed on
   ``entity_id``. ``async_update_entity`` mutates the existing registry row in
   place, so both the ``entity_id`` and the immutable registry row id survive and
   only the ``unique_id`` changes. Removing and recreating a survivor would mint
   a new ``entity_id`` (or a ``_2`` suffix) and orphan its history.

**No identity template is defined here.** Both rewrites are a prefix swap --
``f"{entry_id}:{old_key}:"`` for ``f"{entry_id}:{new_key}:"`` on a re-key,
``f"{source_entry_id}:"`` for ``f"{target_entry_id}:"`` on a consolidation -- so
whatever shape ``COMPATIBILITY_CONTRACT.md`` (§2, §3) freezes is re-emitted
verbatim with one value changed. Do not hardcode a template here: the contract
has already moved once (revision 1's single-server "hub" model to revision 2's
location/receiver one), and a copy kept in this docstring is a copy that will be
wrong after the next amendment.

Two consequences of the revision-2 shapes are worth stating, because they are
what make one pass enough:

* **A location may hold several receivers, but they all feed one merged device**
  under one location-scoped identity, so a re-key is a single pass rather than
  one per receiver: there is only ever one device row to re-point, and one entity
  per mapped field, however many servers received the sensor.
* **The per-receiver link fields (``rssi`` / ``snr`` / ``last_seen``) carry
  through for free.** They are one entity per (sensor x receiver) and their
  ``unique_id`` gains a receiver segment -- but it sits *after* the
  ``device_key`` and *before* the object suffix, so the ``{entry_id}:{key}:``
  prefix the swap matches on is unchanged. That ordering is deliberate (see
  ``entity.field_unique_id``); reversing it would silently strand every link
  entity on the old key.

The second caller of that ordering is :func:`async_consolidate_location`, which
folds one location into another when a user decides two of their rtl_433 servers
are in fact in the same place. It is the same problem one scope up -- every row
under the source location's ``entry_id`` is re-pointed onto the target's -- with
one addition the battery-swap case never hits: both locations may already have
been recording the *same physical sensor*, and Home Assistant will not let two
registry rows hold one ``unique_id``. Exactly one of the two histories can
survive, so the merge keeps the **earliest-added receiver's** row, removes the
other, and raises a Repairs notice naming what was dropped.

Nothing here runs during the v2 → v3 upgrade: that conversion turns each
existing config entry into its own location, keeping its ``entry_id``, precisely
so no history is ever forced to merge by an upgrade the user did not ask for.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    CONF_DEVICES,
    CONF_IGNORED_DEVICES,
    CONF_MODEL,
    CONF_USER_MAPPINGS,
    DEVICE_FIELDS,
    DOMAIN,
)
from .receiver_settings import receiver_subentries


class DeviceReplaceError(Exception):
    """Raised when a device replacement request fails validation.

    Carried up to the options flow, which renders it as a form error rather than
    a traceback. The guards deliberately raise instead of silently no-opping: a
    replace that quietly did nothing would look successful while leaving the
    user's history stranded on the old key.
    """


async def async_replace_device(
    hass: HomeAssistant, entry: ConfigEntry, old_key: str, new_key: str
) -> None:
    """Re-point the device at ``old_key`` onto ``new_key``, preserving history.

    Frees the duplicate device Home Assistant created for the new transmitter id,
    rewrites every surviving entity's ``unique_id`` and the device row's
    ``identifiers`` onto ``new_key``, folds the stored per-device settings across,
    and reloads the entry so the platforms rebuild.

    ``new_key`` need **not** already exist in ``entry.data[CONF_DEVICES]``: the
    replacement is normally a *pending* device -- received but never added, so it has
    no stored record, no device row and no entities -- and re-keying onto such a
    key is the ordinary battery-swap case, not an edge one (the fold treats a
    missing record as an empty one, and steps 1-3 simply find nothing to free).
    ``old_key``, by contrast, must exist — there would otherwise be no settings,
    and no device, to carry across.

    Raises:
        DeviceReplaceError: when either key is empty, the keys are equal, or
            ``old_key`` has no record in the devices map.
    """
    if not old_key or not new_key:
        raise DeviceReplaceError("Both the old and the new device key are required")
    if old_key == new_key:
        raise DeviceReplaceError("The new device key must differ from the old one")
    if old_key not in entry.data.get(CONF_DEVICES, {}):
        raise DeviceReplaceError(f"Unknown device key: {old_key}")

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    old_prefix = f"{entry.entry_id}:{old_key}:"
    new_prefix = f"{entry.entry_id}:{new_key}:"

    # Step 1 — free the duplicate. The registry's entry view is live, so
    # snapshot it with ``list(...)`` before removing anything.
    for regent in list(er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
        if regent.unique_id.startswith(new_prefix):
            ent_reg.async_remove(regent.entity_id)

    duplicate = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}:{new_key}"), entry.entry_id
    )
    if duplicate is not None:
        dev_reg.async_remove_device(duplicate.id)

    # Step 2 — re-key the survivors onto the now-free unique_ids. Re-read the
    # entries because step 1 mutated the registry. The suffix is sliced by
    # ``len(old_prefix)`` rather than split on ":" so it is carried across
    # byte-for-byte, matching the frozen unique_id template exactly.
    for regent in list(er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
        if not regent.unique_id.startswith(old_prefix):
            continue
        object_suffix = regent.unique_id[len(old_prefix) :]
        ent_reg.async_update_entity(
            regent.entity_id, new_unique_id=f"{new_prefix}{object_suffix}"
        )

    # Step 3 — re-point the device row itself. ``name`` is deliberately
    # untouched: a user-assigned ``name_by_user`` is a separate registry field
    # that must be preserved, and the generated name is recomputed from the new
    # key when the entities are rebuilt after the reload.
    old_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}:{old_key}"), entry.entry_id
    )
    if old_device is not None:
        dev_reg.async_update_device(
            old_device.id,
            new_identifiers={(DOMAIN, f"{entry.entry_id}:{new_key}")},
        )

    # Step 4 — fold the stored record onto the new key. Deep-copy the map the way
    # ``async_upsert_device`` does so nested dicts are never shared with
    # ``entry.data``. Starting from the old record is what carries the user's
    # deliberate settings (timeout override, calibration, motion clear delay,
    # event types) across; only ``fields`` is unioned and only ``model`` falls
    # back to the new record.
    devices: dict[str, dict[str, Any]] = {
        k: dict(v) for k, v in entry.data.get(CONF_DEVICES, {}).items()
    }
    old_rec = devices.pop(old_key, {})
    new_rec = devices.get(new_key, {})

    merged = dict(old_rec)
    # The replacement may already have transmitted a field the original never did.
    merged[DEVICE_FIELDS] = sorted(
        set(old_rec.get(DEVICE_FIELDS, [])) | set(new_rec.get(DEVICE_FIELDS, []))
    )
    # Prefer the newly observed model when the old record never learned one.
    if not merged.get(CONF_MODEL):
        merged[CONF_MODEL] = new_rec.get(CONF_MODEL, "")
    devices[new_key] = merged

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_DEVICES: devices}
    )

    # Step 5 -- reload the entry. The platforms rebuild every entity from the
    # updated devices map, and the coordinator's runtime dicts (``devices``,
    # ``last_seen``, ``available``, ``_discovered``, ``pending``) are rebuilt
    # from scratch.
    #
    # That is why no runtime state has to be copied across by hand, and why
    # re-keying onto a pending candidate does not need the candidate evicted
    # first: the old coordinator's in-memory pending list is discarded with it,
    # and ``new_key`` is in the stored devices map by now, so the next frame for
    # it is routed as an adopted device.
    #
    # The reload is kept even though ``async_update_entry`` may trigger the
    # update listener itself: reloading twice is harmless, and doing it here
    # makes the helper correct when called from a context that does not
    # reload.
    await hass.config_entries.async_reload(entry.entry_id)


def _retarget(value: str, source_entry_id: str, target_entry_id: str) -> str | None:
    """Swap a leading location entry id in an identity string, or return ``None``.

    Every rtl_433 identity string -- entity ``unique_id`` and device-registry
    identifier alike -- starts with the entry id of the location that owns it,
    either alone (the location device, ``f"{entry_id}"``) or followed by a colon.
    Swapping that one leading segment is the whole of a consolidation re-key:
    the ``device_key``, the receiver subentry id and the ``object_suffix`` are
    re-emitted byte-for-byte, exactly as the ``device_key`` swap does.

    Returns ``None`` for a string that is not in the source location's scope, so
    a caller can skip a row rather than corrupt it. The colon test matters: a
    bare ``startswith(source_entry_id)`` would also match an unrelated id that
    merely began with the same characters.
    """
    if value == source_entry_id:
        return target_entry_id
    if value.startswith(f"{source_entry_id}:"):
        return f"{target_entry_id}{value[len(source_entry_id) :]}"
    return None


def _fold_devices(
    source: dict[str, Any], target: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Union two locations' adopted-device maps, the target's record winning.

    A device_key both locations adopted is one physical sensor received by both, and
    its surviving entity is the target's -- so the target's record (timeout
    override, calibration, motion clear delay, event types) is the one that still
    describes a live entity, and it wins. ``fields`` is the exception and is
    unioned: the source may have received a field the target never did, and a field
    the merged device can produce should not be forgotten because only one
    receiver ever decoded it. ``model`` falls back to the source's when the
    target's record never learned one.

    Records are copied, never shared with either ``entry.data``, the way
    :func:`async_replace_device` copies them.
    """
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in source.items()}
    for device_key, target_record in target.items():
        record = dict(target_record)
        source_record = merged.get(device_key, {})
        record[DEVICE_FIELDS] = sorted(
            set(source_record.get(DEVICE_FIELDS, []))
            | set(target_record.get(DEVICE_FIELDS, []))
        )
        if not record.get(CONF_MODEL):
            record[CONF_MODEL] = source_record.get(CONF_MODEL, "")
        merged[device_key] = record
    return merged


async def async_consolidate_location(
    hass: HomeAssistant, source_entry: ConfigEntry, target_entry: ConfigEntry
) -> list[str]:
    """Fold ``source_entry``'s receivers and devices into ``target_entry``.

    The deliberate opposite of the v2 → v3 upgrade: the upgrade never merges, and
    this is the path a user takes when they decide two of their rtl_433 servers
    are in the same place after all. Every receiver subentry of the source moves
    to the target (keeping its ``subentry_id``, so its radio controls and its
    managed-settings ``Store`` survive), every device and entity is re-keyed from
    the source's location scope onto the target's in place, and the emptied
    source entry is removed.

    **Which entity survives a collision is the one irreducible loss.** A sensor
    both locations recorded has one row under each scope, and Home Assistant
    forbids two rows holding one ``unique_id`` -- so one history must go. The
    survivor is the target's row, which is the **earliest-added receiver's**: a
    receiver is always consolidated *into* the older location, and the target's
    own receivers therefore precede the absorbed ones in the merged subentry
    order. Recorder history is not queryable here, so "keep the longer one" is
    not available; keeping the earliest is at least predictable. Every dropped
    row is named in a Repairs notice.

    The step order is :func:`async_replace_device`'s, one scope up:

    1. **Free the duplicates first** -- remove the source rows whose re-keyed
       ``unique_id`` the target already holds. Doing this after the re-key would
       make :meth:`async_update_entity` raise part-way through and leave the
       location half-merged.
    2. **Re-key the survivors in place**, never recreate them, so ``entity_id``
       (and therefore recorder history, statistics, dashboards and automations)
       carries through.
    3. **Move the subentries before the devices**, because a receiver device
       carries its subentry id and the registry validates it against the owning
       entry.
    4. **Re-home the entities before their devices**, for the reason
       ``migration._rehome_device_objects`` documents: moving a device to a new
       config entry makes Home Assistant delete every entity still pointing at
       the old one.

    Returns the ``entity_id``s whose history was dropped (empty when the two
    locations had no sensor in common).

    Raises:
        DeviceReplaceError: when the two entries are the same, or either one is
            not a location holding at least one receiver.
    """
    if source_entry.entry_id == target_entry.entry_id:
        raise DeviceReplaceError("A location cannot be consolidated into itself")
    for entry in (source_entry, target_entry):
        if not receiver_subentries(entry):
            raise DeviceReplaceError(f"{entry.title} holds no receiver")

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    source_id = source_entry.entry_id
    target_id = target_entry.entry_id

    # Step 0 -- take both locations down first. Every step below moves registry
    # rows between the two entries, and a loaded entry would race it: its
    # platforms re-create entities under the ids being freed, and adding a
    # subentry to a loaded target fires the update listener, which reloads it
    # mid-surgery. Unloading the source is also what stops its coordinators --
    # after step 3 its receivers belong to the target, so the unload that comes
    # with removing it would no longer find them and their watchdogs would run on
    # against a location that no longer exists.
    await hass.config_entries.async_unload(source_id)
    await hass.config_entries.async_unload(target_id)

    # Step 1 -- free the duplicates. The registry's per-entry view is live, so
    # snapshot it with ``list(...)`` before removing anything.
    dropped: list[str] = []
    for regent in list(er.async_entries_for_config_entry(ent_reg, source_id)):
        new_unique_id = _retarget(regent.unique_id, source_id, target_id)
        if new_unique_id is None:
            continue
        if ent_reg.async_get_entity_id(regent.domain, DOMAIN, new_unique_id) is None:
            continue
        ent_reg.async_remove(regent.entity_id)
        dropped.append(regent.entity_id)

    # Step 2 -- re-key the survivors onto the now-free unique_ids.
    for regent in list(er.async_entries_for_config_entry(ent_reg, source_id)):
        new_unique_id = _retarget(regent.unique_id, source_id, target_id)
        if new_unique_id is None:
            continue
        ent_reg.async_update_entity(regent.entity_id, new_unique_id=new_unique_id)

    # Step 3 -- move the receiver subentries, keeping their ids. They are added
    # to the target before they are removed from the source so the registry rows
    # that name them are never pointing at a subentry that exists nowhere.
    moved_subentries = receiver_subentries(source_entry)
    for subentry in moved_subentries:
        hass.config_entries.async_add_subentry(
            target_entry,
            ConfigSubentry(
                data=subentry.data,
                subentry_id=subentry.subentry_id,
                subentry_type=subentry.subentry_type,
                title=subentry.title,
                unique_id=subentry.unique_id,
            ),
        )

    # Step 4 -- re-home every surviving entity onto the target entry, before any
    # device moves (see the docstring). ``config_subentry_id`` is passed back
    # unchanged rather than left out: the registry refuses to move an entity
    # between config entries without being told which subentry it lands in, and
    # a receiver-owned entity lands in the very subentry that moved with it.
    for regent in list(er.async_entries_for_config_entry(ent_reg, source_id)):
        ent_reg.async_update_entity(
            regent.entity_id,
            config_entry_id=target_id,
            config_subentry_id=regent.config_subentry_id,
        )

    # Step 5 -- the device rows. A device_key both locations adopted already has
    # a row under the target identifier, so the source's row is the duplicate:
    # its surviving entities (the per-receiver link fields, which never collide)
    # move onto the target's row and the duplicate goes. Everything else is
    # re-pointed in place, keeping its registry id, its area and its name_by_user.
    for device in list(dr.async_entries_for_config_entry(dev_reg, source_id)):
        identifier = next(
            (ident for ident in device.identifiers if ident[0] == DOMAIN), None
        )
        if identifier is None:
            continue
        new_identifier = _retarget(identifier[1], source_id, target_id)
        if new_identifier is None:
            continue
        duplicate = dev_reg.async_get_device_by_identifier(
            (DOMAIN, new_identifier), target_id
        )
        if duplicate is not None:
            for regent in list(er.async_entries_for_device(ent_reg, device.id)):
                ent_reg.async_update_entity(regent.entity_id, device_id=duplicate.id)
            dev_reg.async_remove_device(device.id)
            continue
        dev_reg.async_update_device(
            device.id,
            new_identifiers={(DOMAIN, new_identifier)},
            new_config_entry_id=target_id,
            new_config_subentry_id=device.config_subentry_id,
        )

    # Step 6 -- fold the stored state. Adopted devices union with the target's
    # record winning; the ignore list unions outright, because ignoring is a
    # statement about a sensor and a second receiver must not re-offer one the
    # user already dismissed. User mappings union with the target's winning, for
    # the same reason its device records do: they describe the surviving entities.
    hass.config_entries.async_update_entry(
        target_entry,
        data={
            **target_entry.data,
            CONF_DEVICES: _fold_devices(
                source_entry.data.get(CONF_DEVICES, {}),
                target_entry.data.get(CONF_DEVICES, {}),
            ),
            CONF_IGNORED_DEVICES: sorted(
                set(source_entry.data.get(CONF_IGNORED_DEVICES, []))
                | set(target_entry.data.get(CONF_IGNORED_DEVICES, []))
            ),
            CONF_USER_MAPPINGS: {
                **source_entry.data.get(CONF_USER_MAPPINGS, {}),
                **target_entry.data.get(CONF_USER_MAPPINGS, {}),
            },
        },
    )

    # Step 7 -- the source location is empty now; removing it takes its
    # subentries with it, and they hold nothing (everything moved in step 3-5).
    dropped_title = moved_subentries[0].title
    await hass.config_entries.async_remove(source_id)

    if dropped:
        # Imported here, not at module scope: ``repairs`` imports the config flow,
        # which imports the options flow, which imports this module.
        from . import repairs

        repairs.async_raise_history_merged(
            hass,
            target_entry,
            kept=receiver_subentries(target_entry)[0].title,
            dropped=dropped_title,
            entity_ids=dropped,
        )

    # Step 8 -- bring the target back up. Its platforms rebuild every entity from
    # the folded devices map, ``async_get_or_create`` re-points each re-keyed row
    # onto its device and subentry, and a coordinator is started for each
    # absorbed receiver.
    await hass.config_entries.async_setup(target_id)
    return dropped
