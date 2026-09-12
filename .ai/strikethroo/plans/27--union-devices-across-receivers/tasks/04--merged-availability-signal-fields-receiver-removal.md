---
id: 4
group: "union-devices-across-receivers"
dependencies: [3]
status: "completed"
created: 2026-09-12
skills:
  - python
  - home-assistant-integration
---
# Merged two-gate availability, per-receiver signal fields, and receiver removal

## Objective
Keep a merged device available while any receiver can still vouch for it, preserve per-receiver coverage detail without merging it, and define what removing a receiver does.

## Skills Required
python, home-assistant-integration

## Acceptance Criteria
- [ ] A receiver VOUCHES for a device when it is connected AND heard the device within the effective timeout; the merged device is available when at least one receiver vouches
- [ ] The cross-product is correct: connected+stale A with offline+fresh B ⇒ UNAVAILABLE; connected+fresh A with offline B ⇒ available; both silent ⇒ unavailable
- [ ] The aggregator tracks per-`(device_key, receiver)` `last_seen` alongside each receiver's live transport state, reusing `_effective_timeout` and the never-expire exemption semantics unchanged
- [ ] `rssi` / `snr` / `last_seen` yield ONE entity per (sensor × receiver) on the merged device, reusing the existing `pyrtl_433.library` field mappings with a receiver segment — not a new diagnostic-entity class
- [ ] Each such entity carries its receiver in `_attr_name` (e.g. `RSSI Attic`), so no `entity_id` gains a `_2` suffix
- [ ] Those entities are added with NO `config_subentry_id`, and no subentry-move deprecation warning is emitted once both receivers' platforms have loaded
- [ ] `rssi`/`snr` keep `enabled_by_default: false`; the per-receiver comparison is served from aggregator state for the panel to render
- [ ] Removing a receiver subentry drops that receiver's device, radio controls, noise sensors, connectivity entity and its per-receiver signal entities on every merged device; merged devices and their history SURVIVE and availability is recomputed. A device only the removed receiver heard becomes unavailable, NOT deleted
- [ ] `uv run pytest tests/` passes and lint/format are clean

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
Availability is two gates (`entity.py:187-215`): the transport gate (`hub_available` — false the instant the WebSocket drops, no grace window, overriding even never-expire devices) AND the per-device silence gate. OR-ing them independently is a correctness bug. Per-coordinator watchdogs keep running per receiver; the merged value is computed over their union.

## Input Dependencies
Task 003's aggregator and merged device identity.

## Output Artifacts
Merged availability, per-receiver signal entities, and receiver-removal semantics.

## Implementation Notes
See plan Component 4 and Clarifications #14, #17, #20, #22, #23.

## Follow-up decided after this task ran (Clarification #25)

Deleting a location's **last** receiver subentry must be **blocked** — the user
deletes the location entry instead. Task 002 left a receiver-less entry that
`async_setup_entry` refuses with `ConfigEntryError`; blocking makes that state
unreachable rather than merely recoverable. Removing a *non-final* receiver is
unchanged (drop its own entities and its link entities, keep merged devices).

Implement in the subentry removal flow, with a test asserting the last receiver
cannot be removed and that removing one of two still works.
