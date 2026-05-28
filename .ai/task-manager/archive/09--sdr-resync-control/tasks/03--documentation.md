---
id: 3
group: "sdr-resync"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - technical-writing
---
# Document the resync button (README + AGENTS.md)

## Objective
Reverse the "no re-adopt action by design" framing and document the new button as the supported one-click re-sync path.

## Skills Required
- `technical-writing`.

## Acceptance Criteria
- [ ] `README.md` (~`:240-248`, "Re-syncing from the rtl_433 config"): remove the "deliberately no re-adopt button/service" framing and the three-step dance as the only path; document the "Re-sync SDR settings from server" hub button. Note: it only re-adopts when the server's `/cmd` is reachable (a press while unreachable is a safe no-op, no data loss); it is always shown; in hop mode the center frequency stays unmanaged.
- [ ] `AGENTS.md` (~`:255-260`): update the "HA is the authority; no re-adopt action — by design" bullet to state the re-adopt **button** now exists (`button.py`, coordinator `async_resync_sdr()`); describe its composition (refresh meta → guard on empty meta → clear → adopt → enforce; no outer `_cmd_lock`), gating (`manage_settings` on; always available), and that it does not churn the config entry. Add `Platform.BUTTON` to any platform inventory listed.

## Technical Requirements
- Files: `README.md`, `AGENTS.md`. No `translations/en.json` change (button name via `_attr_name`).

## Input Dependencies
- Task 1.

## Output Artifacts
- Updated `README.md`, `AGENTS.md`.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Search for the relevant sections (line numbers may have drifted, especially after plan 07/08 edits to these files). Replace the "only way is the dance" wording precisely; don't leave contradictory text.
- Keep additions concise and consistent with existing tone.
</details>
