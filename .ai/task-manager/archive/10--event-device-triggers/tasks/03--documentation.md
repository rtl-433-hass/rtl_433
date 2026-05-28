---
id: 3
group: "device-triggers"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - technical-writing
---
# Document device triggers (AGENTS.md)

## Objective
Document the durable device-trigger contract for contributors. AGENTS.md only (no README change — Clarification #9).

## Skills Required
- `technical-writing`.

## Acceptance Criteria
- [ ] `AGENTS.md` gains a short "Device triggers (`device_trigger.py`)" subsection (mirroring the existing "Event platform" section) covering: discovered by file presence (NOT a `PLATFORMS` entry); triggers-only (no conditions/actions); per-event-entity granularity with optional `event_type` subtype sourced from persisted `DEVICE_EVENT_TYPES`; and the **split firing mechanism** — base trigger delegates to the core `state` trigger (match_all), subtyped trigger uses a custom `async_track_state_change_event` listener that fires on every matching transmission (because the core state trigger's `attribute`/`to` filter dedupes same-value presses, `triggers/state.py:158`).
- [ ] No README change; no `docs/device-library.md` change.

## Technical Requirements
- File: `AGENTS.md`.

## Input Dependencies
- Task 1.

## Output Artifacts
- Updated `AGENTS.md`.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Read the implemented `device_trigger.py` so the note matches reality.
- Keep it contributor-facing and concise; place near the "Event platform" section. Search for it (line numbers drift after earlier plan edits).
</details>
