---
id: 10
group: "union-devices-across-receivers"
dependencies: [7, 8]
status: "pending"
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
