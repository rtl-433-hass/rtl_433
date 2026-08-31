"""In-place re-key of a nested device onto a new ``device_key``.

Cheap 433 MHz sensors usually draw a fresh transmitter id when their batteries
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

Only the ``device_key`` *value* a row carries changes: the entity ``unique_id``
template ``f"{hub_entry_id}:{device_key}:{object_suffix}"`` and the device
identifiers template ``(DOMAIN, f"{hub_entry_id}:{device_key}")`` are re-emitted
verbatim, so nothing in ``COMPATIBILITY_CONTRACT.md`` moves.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CONF_DEVICES, CONF_MODEL, DEVICE_FIELDS, DOMAIN


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
    replacement is normally a *pending* device -- heard but never added, so it has
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

    # Step 1 — free the duplicate. The registry's entry view is live, so snapshot
    # it with ``list(...)`` before removing anything.
    for regent in list(er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
        if regent.unique_id.startswith(new_prefix):
            ent_reg.async_remove(regent.entity_id)

    duplicate = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}:{new_key}")}
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

    # Step 3 — re-point the device row itself. ``name`` is deliberately untouched:
    # a user-assigned ``name_by_user`` is a separate registry field that must be
    # preserved, and the generated name is recomputed from the new key when the
    # entities are rebuilt after the reload.
    old_device = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}:{old_key}")}
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

    # Step 5 — reload so the platforms rebuild every entity from the updated
    # devices map and the coordinator's runtime dicts (``devices``, ``last_seen``,
    # ``available``, ``_discovered``, ``pending``) are rebuilt from scratch — which
    # is why no separate runtime-state transfer is needed, and why re-keying onto
    # a pending candidate needs no separate eviction: the whole in-memory pending
    # list goes with the old coordinator, and ``new_key`` is in the stored devices
    # map by now, so its next transmission is routed as an adopted device. Kept even though
    # ``async_update_entry`` may itself trigger the update listener: the reload is
    # idempotent and makes the helper correct when called from a context that
    # does not reload.
    await hass.config_entries.async_reload(entry.entry_id)
