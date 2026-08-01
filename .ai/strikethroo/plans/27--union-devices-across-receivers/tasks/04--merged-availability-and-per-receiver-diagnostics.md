---
id: 4
group: "level-2-union"
dependencies: [3]
status: "pending"
created: "2026-07-23"
skills:
  - python
  - home-assistant
---
# Level 2: merged availability + per-receiver diagnostics

## Objective
Keep a merged device available while any receiver still hears it, and preserve per-receiver coverage detail that the merge would otherwise hide, by exposing per-receiver RSSI/SNR/last-seen diagnostic entities.

## Skills Required
- `python`: availability computation over multiple receivers; diagnostic entity classes.
- `home-assistant`: `EntityCategory.DIAGNOSTIC`, availability semantics, existing `_effective_timeout` resolution.

## Acceptance Criteria
- [ ] Merged availability = "seen by **any** receiver within the effective timeout": the aggregator tracks per-`(device_key, receiver)` `last_seen`; the merged `available` uses the most-recent across receivers against the existing device-class-aware `_effective_timeout`.
- [ ] A two-receiver device stays available while one receiver is fresh and the other has gone silent past timeout; it goes unavailable only when all receivers are stale.
- [ ] Per-receiver **RSSI / SNR / last-seen** diagnostic entities are attached to the merged device, receiver-labeled, `EntityCategory.DIAGNOSTIC`, unique_id `f"{location_entry_id}:{device_key}:{receiver_id}:{diag_suffix}"`.
- [ ] SDR-control entities remain on the **receiver** device (not the merged sensor device); their runtime unique_id construction uses the new `receiver` literal (the `:hub:`→receiver rewrite of existing entities is Task 5).
- [ ] Tests: merged-availability any-receiver-fresh; diagnostics present and per-receiver.

Use your internal Todo tool to track these.

## Technical Requirements
- Files: `entity.py` (availability property now consults merged/aggregator state), `sensor.py` (per-receiver diagnostic sensors), coordinator/aggregator wiring.
- Reuse `_effective_timeout` for the device-class-aware/never-expire resolution.

## Input Dependencies
- Task 3: aggregator merged dispatch + per-receiver last-seen state.

## Output Artifacts
- Merged availability + per-receiver diagnostics (validated by Task 6 tests).

## Implementation Notes
- The per-coordinator watchdogs keep running per receiver; merged availability is computed over their union — do not remove the existing watchdog.
