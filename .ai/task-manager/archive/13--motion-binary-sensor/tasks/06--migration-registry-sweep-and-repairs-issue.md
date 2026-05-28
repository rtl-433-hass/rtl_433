---
id: 6
group: "migration"
dependencies: [1, 2]
status: "completed"
created: "2026-05-28"
skills:
  - python
---
# Migration: remove orphaned motion event entity + raise repairs issue

## Objective
On upgrade, clean up the now-orphaned `event.*_motion` entity and tell the user it has moved to `binary_sensor.*_motion`, reusing the existing repairs infrastructure.

## Skills Required
`python` — entity registry + issue registry (`repairs.py`, setup).

## Acceptance Criteria
- [ ] At setup, an **idempotent guarded registry sweep** locates this integration's `event`-domain registry entries whose unique-id suffix equals the motion `object_suffix` (`motion`) and removes them from the entity registry.
- [ ] `motion` is dropped from any persisted `DEVICE_EVENT_TYPES` slot so the event platform never recreates the entity.
- [ ] **Only when** at least one orphaned motion event entity was found/removed, a single repairs issue is raised (reusing `repairs.py` `ir.async_create_issue` shape, new `translation_key`) explaining the move and that automations on the old entity must be updated. `is_fixable=False`, `severity=IssueSeverity.WARNING`.
- [ ] On a clean install (no orphaned entity) nothing is removed and no issue is raised; the sweep is safe to run on every startup.
- [ ] Issue strings added to `translations/en.json` under `issues`.
- [ ] `uvx ruff check .` and `uvx ruff format --check .` pass; the integration sets up without error.

## Technical Requirements
- `repairs.py` issue pattern: `ISSUE_*` translation_key + stable issue id helper + `ir.async_create_issue(...)` / `ir.async_delete_issue(...)` (`repairs.py:39-66`).
- Entity registry: `er.async_get(hass)`, `er.async_entries_for_config_entry` / filter by `domain == "event"`, `platform == DOMAIN`, unique-id suffix `:motion`; remove via `registry.async_remove(entity_id)`.
- `DEVICE_EVENT_TYPES` persisted under `entry.data[CONF_DEVICES][device_key]` (`const.py:82`).
- Setup entrypoint is `__init__.py` `async_setup_entry`.

## Input Dependencies
- Task 1: constants.
- Task 2: the library move (what orphans the event entity).

## Output Artifacts
- Clean upgrade: old motion event entity removed, user notified once.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. Add a helper (in `repairs.py` or a small `_migrate_motion_*` in `__init__.py`) called from `async_setup_entry` after the registry/coordinator are available:
   - `reg = er.async_get(hass)`
   - iterate `er.async_entries_for_config_entry(reg, entry.entry_id)` (or per-device entries), keep `e.domain == "event"` and `e.unique_id endswith ":motion"` (unique-id shape is `f"{hub_entry_id}:{device_key}:{object_suffix}"`).
   - for each, `reg.async_remove(e.entity_id)` and record that at least one was removed.

2. Drop `motion` from persisted event types: for affected `device_key`s, if `entry.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES]` contains a `motion` slot, write back the record without it (use `hass.config_entries.async_update_entry(entry, data=...)` with a copied dict, matching how the codebase mutates `entry.data`).

3. Raise the issue **only** if something was removed:
   ```python
   ir.async_create_issue(
       hass, DOMAIN, "motion_moved_to_binary_sensor",
       is_fixable=False, severity=ir.IssueSeverity.WARNING,
       translation_key="motion_moved_to_binary_sensor",
   )
   ```
   Use a stable issue id (constant or per-entry, matching `repairs.py` style). Idempotent: removing an already-removed entity is a no-op, and the issue id is stable so it is not duplicated.

4. Add to `translations/en.json` under `issues`:
   ```json
   "motion_moved_to_binary_sensor": {
     "title": "Motion entities moved to binary sensors",
     "description": "rtl_433 motion is now a binary_sensor (occupancy) with an auto-off clear delay. The old `event.*_motion` entity was removed. Update any automations that referenced it to use the new `binary_sensor.*_motion` entity."
   }
   ```

5. Run ruff and a quick setup smoke test.
</details>
