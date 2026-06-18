---
id: 3
group: "replay-suppression"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - technical-writing
---
# Document replay suppression (AGENTS.md + README.md)

## Objective
Document the new replay-suppression behavior for both agents and users.

## Skills Required
- `technical-writing` — concise Markdown.

## Acceptance Criteria
- [ ] `AGENTS.md`: the coordinator reads raw `time` before `normalize()` and classifies each frame via two signals — a high-water mark (already-seen ⇒ replay) plus event age vs `REPLAY_STALE_THRESHOLD` (unseen-but-old ⇒ stale gap event). Replays/stale gap events seed sensor values but do NOT fire `event` entities or refresh `last_seen`/`available`; suppressed event transmissions log at INFO. Note the internal `is_replay` dispatch/`NormalizedEvent` marker (watchdog passes `is_replay=False`), the NTP clock-sync assumption, and the "timestamps disabled ⇒ events fire on replay" limitation.
- [ ] `README.md`: a brief behavior note — on reconnect/restart, momentary RF events that occurred while HA was disconnected are intentionally NOT re-fired (logged at INFO), so automations are not triggered late. No new configuration or entities.

## Technical Requirements
- Files: `AGENTS.md`, `README.md`. Match existing tone; keep additions short.

## Input Dependencies
- Task 1 (describe shipped behavior; confirm the chosen `is_replay` carrier to describe it accurately).

## Output Artifacts
- Updated `AGENTS.md`, `README.md`.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Read the implemented `_process_event` and the chosen carrier so the AGENTS.md note matches reality (NormalizedEvent field vs dispatch arg).
- Keep the README note to a couple of sentences in a sensible existing section (near Discovery or a behavior/notes area); do not invent new config docs.
</details>
