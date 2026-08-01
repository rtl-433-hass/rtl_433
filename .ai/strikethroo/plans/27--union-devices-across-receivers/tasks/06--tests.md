---
id: 6
group: "tests-and-docs"
dependencies: [5]
status: "pending"
created: "2026-07-23"
skills:
  - python
  - testing
---
# Test suite: union, dedup, availability, subentry flow, migration

## Objective
Realign and extend the pytest suite to cover the location/receiver model and every union invariant, so the whole plan's Self Validation checks pass.

## Skills Required
- `python` / `testing`: `pytest-homeassistant-custom-component`, registry assertions, `MockConfigEntry` + subentries, freezegun for time/dedup/availability.

## Acceptance Criteria
- [ ] Union: a location with two receiver subentries feeding the same `device_key` yields one device + one entity per field.
- [ ] Dedup: within-window near-duplicate applies exactly once; clearly-newer advances; stale/replay (`T0≪T1`) causes no value regression; modest skew (within window) does not flap.
- [ ] Availability: merged device stays available while any receiver is fresh; unavailable only when all are stale.
- [ ] Diagnostics: per-receiver RSSI/SNR/last-seen present under the merged device.
- [ ] Subentry flow: add-receiver subentry, reconfigure at subentry scope, Supervisor discovery → new location.
- [ ] Migration: `:hub:`→receiver unique_id rewrite preserves entity_ids; two separate v2 receiver entries migrate to two locations with **no merge**; on consolidation, the earliest-added receiver's entity survives and a Repairs issue is raised.
- [ ] `uv run pytest tests/` passes; `uv run ruff check` clean.

Use your internal Todo tool to track these.

## Technical Requirements
- Files under `tests/` (conftest updated for location/subentry builders; new/updated test modules).
- File-disjoint from Task 7 (docs), so the two may run in parallel.

## Input Dependencies
- Tasks 1–5 (all runtime + migration behavior).

## Output Artifacts
- Green test suite covering the plan's Self Validation items.

## Implementation Notes
- Prefer deterministic time control (freezegun) for the debounce window and availability timers.
