---
id: 5
group: "migration"
dependencies: [2, 3, 4]
status: "pending"
created: "2026-07-23"
skills:
  - python
  - home-assistant
---
# Seamless v2→v3 migration (rename literal, per-entry→location, opt-in forced merge)

## Objective
Upgrade existing installs in place with entity IDs and history preserved and **no auto-merge**: each existing receiver ("hub") config entry becomes its own location with one receiver subentry; rewrite the `:hub:` SDR-control unique_ids; and provide the deterministic forced-merge path used only when a user later consolidates receivers into one location.

## Skills Required
- `python`: `async_migrate_entry`, entity/device registry rewrites, Repairs.
- `home-assistant`: `entity_registry.async_update_entity(new_unique_id=…)`, `_rehome_device_objects`, `ir.async_create_issue`.

## Acceptance Criteria
- [ ] `VERSION` bumped to `3`; `async_migrate_entry` handles v2→v3 idempotently.
- [ ] (a) Every existing `:hub:` SDR-control entity unique_id is rewritten to the `receiver` literal; entity_ids preserved.
- [ ] (b) Each existing standalone receiver entry becomes its own **new location** entry with one receiver subentry; nested devices/entities re-homed onto the location-scoped identity via `_rehome_device_objects`; entity_ids/history preserved; **no merge across separate entries**.
- [ ] (c) Forced-merge path (on user consolidation): where two receivers' entities map to the same location-scoped unique_id, deterministically keep the **earliest-added receiver's** entity (subentry creation order), remove the other, and raise a Repairs issue naming the dropped receiver (Clarification #5).
- [ ] A single-receiver install migrates with zero merges and zero history loss; re-running migration is a no-op.
- [ ] Tests colocated in Task 6 (or a migration test here) prove the above.

Use your internal Todo tool to track these.

## Technical Requirements
- File: `custom_components/rtl_433/migration.py` (extend `async_migrate_entry`), `repairs.py` (new `async_raise_*` helper mirroring `async_raise_motion_moved`).
- Reuse `_rehome_device_objects` (add new config-entry/subentry association before removing the old).

## Input Dependencies
- Tasks 2/3/4: the target location-scoped identity and merged model the migration must land on.

## Output Artifacts
- v2→v3 migration + a duplicate-history Repairs helper.

## Implementation Notes
- The upgrade must be loss-free by construction (non-merging default); the only history-drop is the opt-in consolidation path.
- Recorder history is not queryable at migration time — the survivor rule is deterministic (creation order), not history-length based.
