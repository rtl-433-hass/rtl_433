---
id: 10
group: "union-devices-across-receivers"
dependencies: [7, 8]
status: "completed"
created: 2026-09-12
skills:
  - python
  - pytest
  - home-assistant-integration
---
# Test suite for union, dedup, availability, adoption, subentries, migration and ownership

## Objective
Cover every invariant the plan introduces, including the ones that fail silently.

## Skills Required
python, pytest, home-assistant-integration

## Acceptance Criteria
- [ ] Cross-receiver union: one device, one entity per mapped field; value advances on a clearly-newer frame; a within-window near-duplicate applies exactly once
- [ ] Stale-overwrite guard and skew tolerance: live-then-replay shows no regression; frames differing only within the debounce window do not flap
- [ ] Merged availability, all four cross-product cases including the connected-but-deaf / offline-but-fresh case ⇒ unavailable
- [ ] Per-receiver signal fields: one entity per (sensor × receiver), receiver in the name, NO `_2` entity-id suffix, and receiver B's frame never changes receiver A's `rssi`
- [ ] Registry ownership: merged device `config_subentry_id is None`, receiver devices carry their subentry id, and `caplog` shows no subentry-move deprecation report after both receivers load
- [ ] Parser classification: all three unique_id shapes classify correctly through `device_trigger.py` and the `__init__.py` orphan scan; none yields `device_key == "receiver"`
- [ ] Union add-device page: one pending row for a sensor heard by two receivers, last-received data, both receivers recorded, adopt-once leaves the list location-wide
- [ ] Control unique-id migration preserves every `entity_id`; two receivers in one location have distinct full control sets; `grep -rn ':hub:' custom_components/rtl_433/` returns only migration legacy reads
- [ ] No-merge upgrade: two v2 hub entries with entities for one sensor migrate to TWO locations, no merge, all entity IDs preserved; deliberate consolidation keeps the earliest-added receiver's entity with its original `entity_id` and raises a Repairs issue
- [ ] Migration idempotency: running twice changes nothing; entering from each of `minor_version` 1–8 converges
- [ ] Receiver removal: merged device survives with history and stays available; a device only that receiver heard becomes unavailable, not deleted
- [ ] Single-step setup produces one location entry with exactly one receiver subentry and no location `unique_id`
- [ ] `uv run pytest tests/` passes and lint/format are clean

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
`pytest-homeassistant-custom-component`, `MockConfigEntry` with subentries, registry assertions, freezegun for availability/dedup timing.

## Input Dependencies
Tasks 007 and 008 — the full runtime and migration behaviour.

## Output Artifacts
The regression net for every invariant the plan introduces.

## Implementation Notes
File-disjoint from task 011, so the two run in parallel.

## Mutation-floor debt this task MUST clear (measured in CI, not predicted)

The `Mutation floor` gate is **failing** on the branch. CI run 34715141112
(shards 1 and 3) reports three per-file regressions, all introduced by tasks
002–003 rather than by the task that found them:

| File | Now | Baseline | Band | Killed |
|---|---|---|---|---|
| `custom_components/rtl_433/entity.py` | 0.812 | 0.873 | 0.020 | 358/441 |
| `custom_components/rtl_433/config_flow.py` | 0.905 | 0.938 | 0.020 | 632/698 |
| `custom_components/rtl_433/diagnostics.py` | 0.938 | 1.000 | 0.021 | 137/146 |

`entity.py` grew from 363 mutants at baseline to 483, so the new surface is
under-tested rather than the old surface having rotted. Task 004 measured the
survivor distribution: `async_setup_receiver_platform` 26, `_setup_receiver_platform`
21, `_known_fields` 16, `async_upsert_*` 18 — i.e. the platform fan-out and
field bookkeeping added by tasks 002/003.

**Do not fix this by rewriting the baseline.** Raising a baseline to meet a
lowered score defeats the ratchet. Write the missing tests. Use
`uv run mutmut run "custom_components.rtl_433.<module>.*"` then
`uv run mutmut show` to list survivors and target them specifically.

`aggregator.py` is fine (0.969 vs 0.935 floor) and needs no work.
