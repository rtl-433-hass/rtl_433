---
id: 6
group: "union-devices-across-receivers"
dependencies: [5]
status: "pending"
created: 2026-09-12
skills:
  - python
  - home-assistant-integration
---
# WebSocket API rename and location re-scoping

## Objective
Make the command layer speak the location/receiver model before the panel is written against it.

## Skills Required
python, home-assistant-integration

## Acceptance Criteria
- [ ] `rtl_433/hubs` → `rtl_433/receivers` and `rtl_433/settings/hub` → `rtl_433/settings/receiver`
- [ ] The remaining `rtl_433/devices/*` and `rtl_433/settings/*` commands are re-scoped from an `entry_id` naming a hub to a LOCATION entry id plus, where the action is receiver-specific, a receiver subentry id
- [ ] A command serving the union add-device page returns the merged candidate list from task 005
- [ ] Per-receiver signal detail (rssi/snr/last-seen per receiver) is exposed from aggregator state for the panel to render without requiring any entity to be enabled
- [ ] NO compatibility aliases are kept
- [ ] `docs/websocket-api.md` is rewritten to match (no deprecation notice or migration table — the commands are not a public API)
- [ ] `uv run pytest tests/` passes and lint/format are clean

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
`websocket_api.py`; documented surface at `docs/websocket-api.md:286-600`. 12 commands registered today.

## Input Dependencies
Task 005's location-scoped adoption and merged candidate list.

## Output Artifacts
The final command set the panel is written against.

## Implementation Notes
See plan Component 6 and Clarifications #15, #21.
