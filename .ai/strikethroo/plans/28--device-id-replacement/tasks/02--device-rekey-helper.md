---
id: 2
group: "device-identity"
dependencies: []
status: "completed"
created: 2026-08-31
skills:
  - home-assistant-integration
  - python
---
# Shared in-place device re-key helper (`async_replace_device`)

## Objective

Add the single sanctioned helper that re-points an existing nested device onto a
new `device_key`: it frees the duplicate device HA created for the new
transmitter id, rewrites every surviving entity's `unique_id` and the device
row's `identifiers` onto the new key, and folds the stored per-device settings
across — so `entity_id`, and therefore recorder history, is preserved.

## Skills Required

`home-assistant-integration` — device/entity registry APIs and config-entry data
updates. `python` — careful ordering and guard handling in a
partial-failure-sensitive routine.

## Acceptance Criteria

- [ ] New module `custom_components/rtl_433/device_replace.py` exposes
      `async_replace_device(hass, entry, old_key, new_key) -> None` and a typed
      error (e.g. `DeviceReplaceError`) raised on guard violations.
- [ ] Guards raise rather than silently no-op: `old_key` missing from
      `entry.data[CONF_DEVICES]`; `new_key == old_key`; either key empty.
- [ ] Every entity registry row with unique_id prefix
      `f"{entry.entry_id}:{new_key}:"` is removed **before** any survivor is
      rewritten (otherwise `async_update_entity` raises on collision).
- [ ] Each entity row with prefix `f"{entry.entry_id}:{old_key}:"` is updated via
      `async_update_entity(..., new_unique_id=f"{entry.entry_id}:{new_key}:{object_suffix}")`,
      carrying the object suffix across verbatim. Rows are **updated, never
      recreated**, so `entity_id` and the registry row id are unchanged.
- [ ] The `new_key` device registry row (if any) is removed and the `old_key`
      device row is updated with
      `new_identifiers={(DOMAIN, f"{entry.entry_id}:{new_key}")}`.
- [ ] `entry.data[CONF_DEVICES]`: the old record moves to `new_key` carrying
      `timeout_override`, `calibration`, `motion_clear_delay` and `event_types`;
      `fields` is the **union** of the old and any existing new record; the old
      key is deleted. Written once via `async_update_entry`.
- [ ] A `new_key` that has no record in the devices map is handled (treated as an
      empty record), so a device seen only by the coordinator can be adopted.
- [ ] The entry is reloaded after the write so platforms rebuild.
- [ ] `unique_id` and `identifiers` still match the `COMPATIBILITY_CONTRACT.md`
      templates byte-for-byte; `VERSION`/`MINOR_VERSION` unchanged;
      `tests/test_migration_roundtrip.py` passes **unmodified**.
- [ ] `uv run pytest tests/` passes.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- New file: `custom_components/rtl_433/device_replace.py`. A sibling module is
  the established pattern in this repo for a specialized concern lifted out of a
  larger file (`migration.py`, `library.py`, `hub_settings.py` all split out of
  `__init__.py`); `entity.py` is already ~640 lines. Putting it here also keeps
  this task from colliding with task 1, which edits `entity.py` in parallel.
- Registry APIs: `homeassistant.helpers.device_registry as dr` and
  `homeassistant.helpers.entity_registry as er`;
  `er.async_entries_for_config_entry`, `ent_reg.async_update_entity`,
  `ent_reg.async_remove`, `dev_reg.async_get_device`,
  `dev_reg.async_update_device`, `dev_reg.async_remove_device`.
- Constants from `.const`: `DOMAIN`, `CONF_DEVICES`, `CONF_MODEL`,
  `DEVICE_FIELDS`, `DEVICE_TIMEOUT_OVERRIDE`, `DEVICE_MOTION_CLEAR_DELAY`,
  `DEVICE_CALIBRATION`, `DEVICE_EVENT_TYPES`.
- `migration.py` contains close prior art for registry sweeps keyed on unique-id
  prefixes and for re-homing registry objects — read `_rehome_device_objects`
  and the `:last_seen` sweeps before writing new code.

## Input Dependencies

None. Independent of task 1 (different file), so both run in Phase 1.

## Output Artifacts

- `custom_components/rtl_433/device_replace.py` with `async_replace_device` and
  `DeviceReplaceError`, consumed by the options flow in task 3.

## Implementation Notes

<details>
<summary>Step-by-step implementation</summary>

**Why the ordering matters.** The entity registry refuses a `unique_id` that
another row already holds. After a battery swap HA has usually already created a
full set of entities for the new key. So the duplicate's rows must be removed
*first*, then the survivors rewritten onto the freed ids. Getting this backwards
leaves the device half-migrated.

**Why this preserves history.** Recorder rows are keyed on `entity_id`.
`async_update_entity` mutates the existing registry row in place, so both the
`entity_id` and the immutable registry row id survive; only the `unique_id`
changes. Removing and recreating the row would produce a new `entity_id`
(or a `_2` suffix) and orphan the history — never do that for the survivors.

1. Create `custom_components/rtl_433/device_replace.py` with a module docstring
   explaining that this is the only sanctioned place to rewrite a nested device's
   identity, and why the step order is load-bearing.

2. Define the error:

   ```python
   class DeviceReplaceError(Exception):
       """Raised when a device replacement request fails validation."""
   ```

3. Implement the helper:

   ```python
   async def async_replace_device(
       hass: HomeAssistant, entry: ConfigEntry, old_key: str, new_key: str
   ) -> None:
   ```

   **Guards first.** Raise `DeviceReplaceError` when `not old_key or not new_key`,
   when `old_key == new_key`, or when `old_key not in entry.data.get(CONF_DEVICES, {})`.
   Do not guard on `new_key` being present in the map — adopting a
   coordinator-seen-but-unregistered key is a supported case.

   **Step 1 — free the duplicate.**

   ```python
   ent_reg = er.async_get(hass)
   dev_reg = dr.async_get(hass)
   new_prefix = f"{entry.entry_id}:{new_key}:"
   old_prefix = f"{entry.entry_id}:{old_key}:"

   for regent in list(er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
       if regent.unique_id.startswith(new_prefix):
           ent_reg.async_remove(regent.entity_id)

   dup = dev_reg.async_get_device(
       identifiers={(DOMAIN, f"{entry.entry_id}:{new_key}")}
   )
   if dup is not None:
       dev_reg.async_remove_device(dup.id)
   ```

   Snapshot the entries with `list(...)` before mutating — removing while
   iterating the registry's live view is a bug.

   **Step 2 — re-key the survivors.** Re-read the entries (step 1 mutated the
   registry), and for each row whose unique_id starts with `old_prefix`, split off
   the object suffix and rebuild:

   ```python
   for regent in list(er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
       if not regent.unique_id.startswith(old_prefix):
           continue
       object_suffix = regent.unique_id[len(old_prefix):]
       ent_reg.async_update_entity(
           regent.entity_id,
           new_unique_id=f"{entry.entry_id}:{new_key}:{object_suffix}",
       )
   ```

   Slicing by `len(old_prefix)` (rather than `split(":")`) is deliberate: it
   carries the suffix across byte-for-byte even though `device_key` itself never
   contains a colon.

   **Step 3 — re-point the device row.**

   ```python
   old_device = dev_reg.async_get_device(
       identifiers={(DOMAIN, f"{entry.entry_id}:{old_key}")}
   )
   if old_device is not None:
       dev_reg.async_update_device(
           old_device.id,
           new_identifiers={(DOMAIN, f"{entry.entry_id}:{new_key}")},
       )
   ```

   Do not touch `name`: a user-assigned `name_by_user` is a separate field and
   must be preserved, and the generated name is recomputed from the new key when
   entities are rebuilt after the reload.

   **Step 4 — fold the stored record.** Deep-copy the map the way
   `async_upsert_device` (`entity.py`) and `_write_device_record`
   (`options_flow.py`) already do, so nested dicts are not shared with
   `entry.data`:

   ```python
   devices = {k: dict(v) for k, v in entry.data.get(CONF_DEVICES, {}).items()}
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
   ```

   Starting from `dict(old_rec)` is what carries `timeout_override`,
   `calibration`, `motion_clear_delay` and `event_types` across — the user's
   deliberate settings win over the throwaway new record. Only `fields` is
   unioned and only `model` falls back.

   **Step 5 — reload.** `await hass.config_entries.async_reload(entry.entry_id)`.
   This rebuilds every entity from the updated devices map and resets the
   coordinator's runtime dicts (`devices`, `last_seen`, `available`,
   `_discovered`), which is why no separate runtime-state transfer is needed.

   Note that `async_update_entry` may itself trigger the update listener; if the
   reload turns out to be redundant in testing, keep it anyway — it is idempotent
   and makes the helper correct when called from a context that does not reload.

4. Type-annotate fully and keep the docstring style of the surrounding modules
   (imperative summary line, then a paragraph explaining *why*, matching
   `migration.py` and `entity.py`).

5. Run `uv run pytest tests/` and confirm `tests/test_migration_roundtrip.py`
   passes without being edited. If it fails, the design is wrong — stop and
   report rather than editing the guard.
</details>
