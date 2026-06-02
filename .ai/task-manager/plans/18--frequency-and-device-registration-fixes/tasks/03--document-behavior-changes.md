---
id: 3
group: "documentation"
dependencies: [1, 2]
status: "pending"
created: 2026-06-01
skills:
  - technical-writing
---
# Document the registration-gate and initial-frequency behavior changes

## Objective
Document the two behavior changes so users and assistants understand the new setup-time semantics: (1) only devices seen after connection are auto-registered (with the clock-sync assumption and grace window), and (2) the setup-time initial frequency authoritatively overrides adopted SDR settings once.

## Skills Required
- `technical-writing`

## Acceptance Criteria
- [ ] `AGENTS.md` describes the post-connection device-registration gate, including the server/HA clock-sync assumption and the grace window, and that backlog devices register on their first live message.
- [ ] `AGENTS.md` (and the README section if it covers initial frequency) states that the setup-time initial frequency overrides adopted SDR settings exactly once.
- [ ] Wording is consistent with the implemented constants/method names from Tasks 1 and 2.
- [ ] No stale claims contradicting the new behavior remain in the touched docs.

## Technical Requirements
- Files: `AGENTS.md`, and `README.md` only if it already documents initial-frequency / device-registration behavior.

## Input Dependencies
- Task 1 (registration gate) and Task 2 (initial-frequency seed) must be implemented so docs match the actual constants/methods.

## Output Artifacts
- Updated documentation.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

- Grep `AGENTS.md` and `README.md` for existing sections on device discovery/registration and on `initial_frequency` / Center frequency / manage settings; update those sections in place rather than appending new ones where one already exists.
- For registration: state that on (re)connect the integration auto-registers a previously-unknown device only when it sees a message timestamped at/after the connection time (minus a small grace window), so a server's pre-connection backlog no longer floods the device list; note this relies on the rtl_433 server and Home Assistant clocks being roughly in sync, and that a backlogged device still registers on its first live transmission.
- For initial frequency: state that when managing settings, the frequency entered during setup overrides the server's adopted/current frequency once at first connect and is then user-owned (later changes via the Center Frequency control are preserved).
- Keep the CHANGELOG to the normal release-please commit convention (handled by commit messages, not a manual CHANGELOG edit) — do not hand-edit `CHANGELOG.md`.
- Match the existing documentation tone/format; do not add code examples unless the surrounding section already uses them.
</details>
