---
id: 2
group: "implementation"
dependencies: []
status: "completed"
created: 2026-06-04
skills:
  - python
---
# Migrate persisted doorbell `event_types` to the mapped values (minor_version 5)

## Objective
Add a one-time, idempotent config-entry migration that rewrites any already-persisted doorbell `event_types` from the raw `"0"`/`"1"` strings to the new mapped `"ring"`/`"secret_knock"` values, behind a new `minor_version=5` gate.

## Skills Required
- `python` (Home Assistant config-entry migration)

## Acceptance Criteria
- [ ] A new helper (e.g. `_migrate_doorbell_event_types`) rewrites persisted `event_types` for the doorbell field using the map `{"0": "ring", "1": "secret_knock"}`.
- [ ] Values already equal to `"ring"`/`"secret_knock"` and any unrecognized values pass through unchanged (idempotent; running twice yields a stable result).
- [ ] The migration is gated by a new `minor_version=5` bump in `async_migrate_entry`, following the existing `< 4` pattern; the entry version is updated to `version=2, minor_version=5`.
- [ ] No entity is removed and no repairs issue is raised (the doorbell `unique_id`/`object_suffix` are unchanged — unlike the motion migration).
- [ ] The devices-map write only occurs when a value actually changes (no-op otherwise), mirroring `_migrate_motion_event_to_binary_sensor`.

## Technical Requirements
- Edit `custom_components/rtl_433/migration.py`.
- Reuse constants `CONF_DEVICES`, `DEVICE_EVENT_TYPES` already imported there.

## Input Dependencies
None (does not import entity code; the value map is local to the migration).

## Output Artifacts
- Migrated stored data, validated by task 3.

## Implementation Notes

<details>
<summary>Step-by-step implementation</summary>

**1. Add the helper.** Model it on `_migrate_motion_event_to_binary_sensor` (lines ~97-160) but simpler — there is no entity removal and no repairs issue.

- The doorbell field is identified by its `object_suffix`/`field_key` `secret_knock` (the persisted `DEVICE_EVENT_TYPES` dict is keyed by field_key). Define a module constant near `_MOTION_OBJECT_SUFFIX`, e.g. `_DOORBELL_FIELD_KEY = "secret_knock"` and `_DOORBELL_EVENT_MAP = {"0": "ring", "1": "secret_knock"}`.
- Walk `entry.data.get(CONF_DEVICES, {})`. For each `device_key, record` where `record` is a dict and `_DOORBELL_FIELD_KEY` is in `record.get(DEVICE_EVENT_TYPES, {})`:
  - Take the persisted list `old = record[DEVICE_EVENT_TYPES][_DOORBELL_FIELD_KEY]`.
  - Compute `new = sorted({_DOORBELL_EVENT_MAP.get(v, v) for v in old})` (map known raw values, pass others through; sort to match `async_upsert_event_types`' stored-sorted convention).
  - If `new != old`, deep-copy the record (`new_record = dict(record)`, copy the inner event-types dict) and set the new list; mark changed.
- Only call `hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICES: new_devices})` if something changed.

**2. Wire into `async_migrate_entry`** (around line 297-385). After the existing `if (entry.minor_version or 1) < 4:` block, add:
```python
if (entry.minor_version or 1) < 5:
    _migrate_doorbell_event_types(hass, entry)
    hass.config_entries.async_update_entry(entry, version=2, minor_version=5)
```
Match the surrounding style (some blocks pass `version=2`; keep `version=2, minor_version=5`). Ensure the early-return guard at the top (`if entry.version > 2: return True`) still permits this minor bump — it does, since version stays 2.

**3. Idempotency check.** Because the map only rewrites `"0"`/`"1"` and leaves `"ring"`/`"secret_knock"` untouched, a second run produces `new == old` and writes nothing. Confirm with a quick mental trace (test covers it in task 3).
</details>
