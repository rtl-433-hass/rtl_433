---
id: 2
group: "compatibility"
dependencies: []
status: "pending"
created: 2026-07-06
skills:
  - home-assistant
  - python
complexity_score: 5
complexity_notes: "Requires reading and precisely transcribing existing registry/migration semantics into a frozen ABI; correctness matters because both builds must produce byte-identical identifiers."
---
# Freeze the HACS/Core Compatibility Contract as an ABI

## Objective
Document, as an explicit and frozen ABI in this repository, the three identity surfaces that must stay byte-identical between the HACS full build and the minimal core build: the config-entry `version`/`minor_version` + migration scheme, the entity `unique_id` formats, and the device identifier tuples. This is the central risk control for the single-shared-domain strategy.

## Skills Required
- **home-assistant**: config-entry versioning/migration semantics, entity registry `unique_id`, device registry identifiers.
- **python**: reading the current integration modules accurately.

## Acceptance Criteria
- [ ] A new document (e.g. `COMPATIBILITY_CONTRACT.md` at the repo root) exists capturing all three surfaces verbatim from the current code.
- [ ] The config-entry section records current `VERSION = 2` and the `minor_version` ladder (currently up to 7), and states the rule: migrations are monotonic and non-destructive; the minimal core build must tolerate options/minor-versions written by the full build; no migration may downgrade.
- [ ] The `unique_id` section records every current format: device entities `f"{hub_entry_id}:{device_key}:{object_suffix}"`, hub entities `f"{hub_entry_id}:hub:{object_suffix}"`, and the hub connectivity `f"{hub_entry_id}:hub:connectivity"`.
- [ ] The device-identifier section records: hub device `(DOMAIN, entry.entry_id)`, per-device `(DOMAIN, f"{hub_entry_id}:{device_key}")`, and the phantom `(DOMAIN, f"{entry.entry_id}:{PHANTOM_DEVICE_KEY}")`.
- [ ] The document explicitly states these are frozen and must not change without a coordinated migration across both builds.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Source of truth files in `custom_components/rtl_433/`: `migration.py` (`async_migrate_entry`), `entity.py` (unique_id + identifiers), `binary_sensor.py` (hub connectivity), `const.py` (`PHANTOM_DEVICE_KEY`, object suffixes).
- Do not change any code in this task — this is a documentation/ABI-freeze task only.

## Input Dependencies
None. Reads the current HACS integration only. Phase 1 task.

## Output Artifacts
- `COMPATIBILITY_CONTRACT.md` at the repo root — consumed by Task 3 (migration tests) and Task 4 (core scaffold must reproduce these exact formats).

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

- Read `custom_components/rtl_433/migration.py` fully. Record the migration entry point behaviour: it rejects `entry.version > 2`, migrates `version == 1` → `version 2, minor_version 2`, then applies a ladder of `minor_version` bumps (2→3→4→5→6→7), each guarded by `if (entry.minor_version or 1) < N`. Transcribe the ladder accurately (what each minor bump does at a one-line level), citing line numbers.
- Read `custom_components/rtl_433/entity.py` and record the three `unique_id` templates and the two device-identifier templates verbatim (with the exact f-string form and where `object_suffix` / `device_key` / `hub_entry_id` come from).
- Note the subtlety that `hub_entry_id` equals the config entry's `entry_id`; both `entry.entry_id`-scoped identifiers and `hub_entry_id`-scoped identifiers appear in the code — document that they are the same value so the core build uses the identical scoping.
- State the invariant plainly for a future maintainer: "The minimal core `rtl_433` integration MUST construct `unique_id` and device `identifiers` using these exact templates, and MUST carry the same `VERSION`/`minor_version` and a migration path that is a superset-tolerant, non-downgrading subset of the above. Changing any template is a breaking change requiring a coordinated migration in both builds."
- Keep the document concise and reference-grade; it is an ABI spec, not a tutorial.
</details>
