---
id: 3
group: "new-device-notification"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - technical-writing
---
# Document the new-device notification (README + AGENTS.md)

## Objective
Document the in-app notification and its restart-safe behavior.

## Skills Required
- `technical-writing`.

## Acceptance Criteria
- [ ] `README.md` "Discovery" section: one sentence that adopting a genuinely-new device raises an in-app persistent notification (stable per-device id, so a re-appearing deleted device replaces rather than duplicates), and that **restarting HA does not re-notify** for already-known devices.
- [ ] `AGENTS.md` (near the new-device dispatcher description): note that `new_device_callback` (`__init__.py`) also raises a `persistent_notification` with a stable per-device `notification_id`, **gated on `entry.data[CONF_DEVICES]`** (not the coordinator's per-session `is_new`) so restarts/reloads don't re-notify; in-app only.

## Technical Requirements
- Files: `README.md`, `AGENTS.md`.

## Input Dependencies
- Task 1.

## Output Artifacts
- Updated docs.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Search for the README "Discovery" section and the AGENTS.md new-device dispatcher description (line numbers drifted after earlier plans). Keep additions short, matching tone.
</details>
