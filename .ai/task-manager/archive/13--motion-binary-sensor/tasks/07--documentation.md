---
id: 7
group: "docs-and-tests"
dependencies: [1, 2, 3, 4, 5, 6]
status: "completed"
created: "2026-05-28"
skills:
  - technical-writing
---
# Documentation: device-library schema, AGENTS.md, README note

## Objective
Update contributor and user docs to reflect the new `clear_delay` attribute, motion's reclassification as an occupancy binary_sensor, the per-device override, the runtime timer behaviour, the migration, and the entity_id change.

## Skills Required
`technical-writing` — Markdown docs in this repo.

## Acceptance Criteria
- [ ] `docs/device-library.md`: a `clear_delay` row is added to the attributes table; `motion` is moved out of the "Event entities" section into the binary section; a short "Motion / occupancy" note explains the synthesized-off and the per-device override.
- [ ] `AGENTS.md`: documents the `clear_delay` descriptor attribute, the `Rtl433BinarySensor` timer behaviour (reschedule-on-retrigger, cancel-on-remove, no stale restore), the `DEVICE_MOTION_CLEAR_DELAY` per-device override + `effective_clear_delay_resolver`, and the event→binary migration (repairs issue + cleanup).
- [ ] `README.md`: a brief upgrade note that `event.*_motion` becomes `binary_sensor.*_motion` (a BC break).
- [ ] Docs match the as-built code (device_class `occupancy`, default 90 s, options-flow override).

## Technical Requirements
- `docs/device-library.md` attributes table is around lines 63-84; "Event entities" section around 137-177; "Binary payloads" around 112-133.
- `AGENTS.md` has an existing "Event platform" / device-library section to mirror in tone.

## Input Dependencies
- Tasks 1-6 (documents the finished behaviour; reads the as-built defaults and key names).

## Output Artifacts
- Accurate human- and agent-facing documentation.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. `docs/device-library.md`:
   - Add a `clear_delay` row to the attributes table: `no` / `int` / "For `binary_sensor` only: seconds after a detection to synthesize an off (the device sends no off). Reschedules on each detection; per-device override via the options flow."
   - Remove `motion` from the event-entities table/notes; add a short "Motion / occupancy" subsection on the binary side explaining detect-only hardware, the synthesized off, default 90 s, and the per-device override.

2. `AGENTS.md`: add a subsection (near the binary_sensor / device-library notes) covering the four bullets in the acceptance criteria. Keep it contributor-facing and concise, matching the existing "Event platform" section style.

3. `README.md`: one short upgrade/release note line under whatever "changes"/"notes" area exists (or a brief inline note) — `event.*_motion` → `binary_sensor.*_motion`, automations must be updated; a repairs issue flags it on upgrade.

4. Verify names/defaults against the merged code from Tasks 1-6 (`occupancy`, `90`, `DEVICE_MOTION_CLEAR_DELAY`, option label).
</details>
