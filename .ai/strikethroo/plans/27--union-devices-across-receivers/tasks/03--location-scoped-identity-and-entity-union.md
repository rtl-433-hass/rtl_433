---
id: 3
group: "union-devices-across-receivers"
dependencies: [2]
status: "pending"
created: 2026-09-12
skills:
  - python
  - home-assistant-integration
---
# Location-scoped identity and the entity union (grouping then union)

## Objective
Collapse every receiver's view of a sensor onto one location-scoped device and one entity set, without stale frames overwriting fresh values and without assuming perfectly-synced host clocks.

## Skills Required
python, home-assistant-integration

## Acceptance Criteria
- [ ] SUB-STEP A (device grouping): nested-device `DeviceInfo.identifiers` become `(DOMAIN, f"{location_entry_id}:{device_key}")` and `via_device_id` resolves from the LOCATION device; the merged device is created with `config_subentry_id=None`
- [ ] Sub-step A is sanity-checked before sub-step B lands: both receivers' entities appear under one device-registry device
- [ ] SUB-STEP B (entity union): entity `unique_id`s become `f"{location_entry_id}:{device_key}:{object_suffix}"` — receiver-agnostic, `object_suffix` byte-identical to today
- [ ] A location aggregator subscribes to every receiver-coordinator's per-device dispatch and re-emits one location-scoped, receiver-agnostic device-update signal keyed by `device_key`
- [ ] Dedup: a frame within `_MERGE_DEBOUNCE` (~2–5 s, a named constant) of the last applied one for a `(device_key, field)` is the SAME transmission and is ignored (first-applied wins); a clearly older frame is rejected as stale/backlog; a clearly newer frame is applied
- [ ] The signal-quality fields `rssi`, `snr` and `last_seen` are EXCLUDED from the union and the dedup — the aggregator partitions incoming fields into unioned sensor fields and per-receiver link fields on the way in
- [ ] One physical sensor yields exactly one device and one entity per mapped sensor field regardless of how many receivers hear it
- [ ] `uv run pytest tests/` passes and lint/format are clean

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
Fixes the unconditional value apply at `entity.py:267`. `event_time` derives from each receiver host's own decode clock, so use the debounce window rather than strict newest-wins — skew can invert a naive comparison. The dedup stays in the integration, not `pyrtl_433`: it is a Home-Assistant-side merge policy over multiple clients, not a property of one client's stream.

## Input Dependencies
Task 002's location/subentry topology and coordinators.

## Output Artifacts
The location aggregator, merged devices, and one entity per mapped field.

## Implementation Notes
Grouping precedes union as an ordered sub-step of this single task, not a separate phase — it ships to nobody (Clarification #24). See plan Component 3 and Clarifications #4, #6, #16, #22.
