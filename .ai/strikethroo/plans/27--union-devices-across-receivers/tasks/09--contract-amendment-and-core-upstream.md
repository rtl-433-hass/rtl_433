---
id: 9
group: "union-devices-across-receivers"
dependencies: [8]
status: "pending"
created: 2026-09-12
skills:
  - technical-writing
  - home-assistant-integration
---
# COMPATIBILITY_CONTRACT revision and CORE_UPSTREAM realignment

## Objective
Make the ABI break legitimate, coordinated and non-downgrading, as the contract requires.

## Skills Required
technical-writing, home-assistant-integration

## Acceptance Criteria
- [ ] `COMPATIBILITY_CONTRACT.md` is revised to a NEW revision: §1 restated with `VERSION = 3` and the v2(minor 8)→v3 step and the raised `> 3` future-schema guard, non-downgrade rule intact
- [ ] §2 restated with the location-scoped device-field template and the new four-segment receiver-control and per-receiver-signal templates
- [ ] §3 restated with the location device, the receiver device, and the location-scoped nested-device tuple plus its `via_device_id` link
- [ ] §4's invariant (`hub_entry_id == entry.entry_id`) is REPLACED by the two-level invariant: the location entry id is the identity scope and the receiver is identified by its subentry id
- [ ] A registry-ownership section records that a device has one owner and that merged devices carry `config_subentry_id = None`
- [ ] `CORE_UPSTREAM.md` scopes the Bronze PR1 slice to the NEW templates from the start, records the sequencing decision, and flags the `> 3` guard as a hard prerequisite for the Core build
- [ ] The documented templates are diffed against the actual construction sites and agree byte-for-byte

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
`COMPATIBILITY_CONTRACT.md` (status FROZEN), `CORE_UPSTREAM.md`, `AGENTS.md:50-68`.

## Input Dependencies
Task 008's shipped templates and version ladder.

## Output Artifacts
A contract that matches what the code actually writes.

## Implementation Notes
Before starting, confirm PR1's actual upstream state; if it is already in review, the sequencing decision must be revisited. See plan Component 7 and Clarification #9.
