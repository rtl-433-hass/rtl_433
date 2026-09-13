---
id: 7
group: "union-devices-across-receivers"
dependencies: [6]
status: "completed"
created: 2026-09-12
skills:
  - javascript
  - frontend
  - home-assistant-integration
---
# Panel rework — location view, receiver cards, union add-device page

## Objective
Make the primary discovery surface present a location containing receivers and merged devices.

## Skills Required
javascript, frontend, home-assistant-integration

## Acceptance Criteria
- [ ] The panel shows a LOCATION containing receivers and merged devices
- [ ] ONE add-device page listing the union of candidates across all receivers; a sensor heard by several receivers is one row showing last-received data and which receivers heard it
- [ ] Adopting or ignoring from that page applies to the whole location
- [ ] Per-receiver status and radio controls appear on their own cards
- [ ] Merged devices show per-receiver signal detail read from aggregator state — e.g. 'heard by Attic (−62 dB) / Garage (−89 dB)' — with no diagnostic entity needing to be enabled
- [ ] The panel speaks the receiver/radio vocabulary throughout
- [ ] Save buttons live in the card's card-actions row; the FAB shape is reserved for 'add a thing'
- [ ] `uv run pytest tests/` passes and lint/format are clean

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
`frontend/rtl_433-panel.js` (3,732 lines) over the command set from task 006.

## Input Dependencies
Task 006's renamed, re-scoped commands.

## Output Artifacts
A panel that speaks the location model.

## Implementation Notes
Written once against the FINAL command set — that is why task 006 precedes it. See plan Component 6 and Clarification #12.
