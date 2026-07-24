---
id: 3
group: "level-2-union"
dependencies: [2]
status: "pending"
created: "2026-07-23"
skills:
  - python
  - home-assistant
---
# Level 2: location aggregator, entity union, skew-tolerant dedup

## Objective
Fan every receiver-coordinator's per-device events into one location-scoped device and one entity set, with a skew-tolerant debounce dedup so a stale/replayed frame from one receiver never overwrites a fresher value from another and near-simultaneous duplicates apply once. This narrows identity from device-level (Task 2) to entity-level (true union).

## Skills Required
- `python`: new aggregator module; dispatcher subscribe/re-emit; per-key state.
- `home-assistant`: dispatcher fan-out, entity `unique_id` uniqueness, `NormalizedEvent` fields.

## Acceptance Criteria
- [ ] New location-aggregator module subscribes to every receiver-coordinator's `signal_device_update` and re-emits one location-scoped, receiver-agnostic per-device signal keyed by `device_key`.
- [ ] Entity `unique_id`s become `f"{location_entry_id}:{device_key}:{object_suffix}"` (receiver-agnostic): one physical sensor → one device + one entity per field regardless of receiver count.
- [ ] Dedup rule (Clarification #4): frames for a `(device_key, field)` within `_MERGE_DEBOUNCE` of the last applied are one transmission (first-applied wins, near-dup ignored); a clearly-older frame is rejected (backlog replay); a clearly-newer frame is applied. Never advances last-seen/availability on `is_replay` frames.
- [ ] New-device registration deduped at the location level (a second receiver hearing a known device does not create a duplicate).
- [ ] Tests: union (one entity per field), within-window near-dup applied once, stale/replay no-regression, modest skew no flapping.

Use your internal Todo tool to track these.

## Technical Requirements
- New file: `custom_components/rtl_433/aggregator.py` (or `coordinator/_aggregate.py`).
- Consumes `NormalizedEvent.event_time` (each host's decode clock — see plan Component 4); holds per-`(device_key, field)` last-applied `(event_time, applied_at)`.
- Fixes the unconditional value apply at `entity.py:265-267` for the multi-receiver case (value application now routed via the aggregator's rule).

## Input Dependencies
- Task 2: location-scoped device identity.
- Task 1: `_MERGE_DEBOUNCE`, per-receiver coordinators.

## Output Artifacts
- Merged per-device dispatch + dedup state (consumed by Task 4 availability/diagnostics).

## Implementation Notes
- Do not assume synced clocks; the debounce window absorbs modest skew. Make the window a named constant.
- Keep the per-receiver coordinators unchanged as sources; the aggregator is a thin layer above them.
