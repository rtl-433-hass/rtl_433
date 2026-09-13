---
id: 8
group: "union-devices-across-receivers"
dependencies: [4, 5]
status: "completed"
created: 2026-09-12
skills:
  - python
  - home-assistant-integration
---
# v2→v3 migration: control unique-ids, non-merging conversion, consolidation via device_replace

## Objective
Upgrade existing installs in place with history preserved, auto-merging forced-duplicate histories with a visible notice only when a user deliberately consolidates.

## Skills Required
python, home-assistant-integration

## Acceptance Criteria
- [ ] Config-entry `VERSION` is 3 (from `VERSION = 2, MINOR_VERSION = 8`) and the future-schema guard rejects only `entry.version > 3`; the ladder stays forward-only
- [ ] The four families of `:hub:` control unique_ids are rewritten to `f"{location_entry_id}:receiver:{receiver_subentry_id}:{object_suffix}"` via `entity_registry.async_update_entity`; every control `entity_id` is preserved
- [ ] Each existing standalone receiver entry becomes its OWN new location entry with a single receiver subentry — deliberately NON-MERGING; nested devices/entities are re-homed onto the location-scoped identity via `_rehome_device_objects`, preserving entity IDs and history
- [ ] The forced-merge path used when a user LATER consolidates receivers is implemented by EXTENDING `device_replace.py` — never by open-coding registry surgery in `migration.py`
- [ ] That path deterministically keeps the entity of the EARLIEST-ADDED receiver (subentry creation order), removes the other, and raises a Repairs issue naming which receiver's duplicate history was dropped
- [ ] Each v2→v3 step is idempotent: re-running migration changes nothing, and entering from `minor_version` 1 through 8 converges to the same v3 state
- [ ] Two receivers in one location each expose their own complete, NON-COLLIDING set of radio controls, noise sensors and connectivity entity
- [ ] `uv run pytest tests/` passes and lint/format are clean

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
`device_replace.py`'s load-bearing ordering — free the duplicate rows first, then mutate survivors via `async_update_entity` — is exactly what the forced merge needs so `entity_id` and recorder history carry through. `_rehome_device_objects` (`migration.py:336-353`) moves devices in a single `new_config_entry_id` update. AGENTS.md forbids open-coding a re-key anywhere else.

## Input Dependencies
Task 004's availability/signal model and task 005's adoption sets.

## Output Artifacts
A seamless, loss-free upgrade and a deterministic consolidation path.

## Implementation Notes
The upgrade never triggers a forced merge — existing separate entries each become their own location. See plan Component 8 and Clarifications #2, #5, #13.

## Findings carried forward from earlier tasks

**From task 002 — `_rehome_device_objects` deletes the entities it is meant to move.**
Moving a device with `new_config_entry_id` makes HA delete the entities still
pointing at the old entry, so the entity re-home loop that runs afterwards finds
nothing left to move. At `main` this was invisible: a successful setup
immediately recreated the rows under the same unique_ids, so history appeared to
survive by luck rather than by the re-home working. Task 002 made it visible by
refusing setup for an unmigrated entry. **Fix by re-homing entities BEFORE
devices**, and add a regression test that asserts the re-homed entity registry
rows are the *same* rows (same registry id and `entity_id`), not recreated ones.

**From task 002 — device-field unique_ids currently sit at subentry scope.**
Task 002 keyed them `f"{receiver_id}:{device_key}:{suffix}"` (receiver_id = the
subentry id) and task 003 moves them to `f"{location_entry_id}:{device_key}:{suffix}"`.
Because each existing v2 entry becomes its own location and KEEPS its entry id,
the post-task-003 device-field template is byte-identical to the v2 one — so the
migration should need to rewrite **only** the receiver-control unique_ids
(`:hub:` → `:receiver:{subentry_id}:`), not the device fields. Verify this holds
before writing the migration; if it does, assert it with a test rather than
rewriting rows unnecessarily.

**From task 002 — existing v2 entries do not load.**
An entry with no receiver subentry raises `ConfigEntryError`. This task must make
them load again by converting each one into a location with a single receiver
subentry. Until this task lands, the branch cannot set up a pre-existing install.
