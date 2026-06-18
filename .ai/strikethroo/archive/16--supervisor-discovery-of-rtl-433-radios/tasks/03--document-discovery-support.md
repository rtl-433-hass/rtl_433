---
id: 3
group: "config-flow-discovery"
dependencies: [1]
status: "completed"
created: 2026-06-01
skills:
  - technical-writing
---
# Document Supervisor discovery support (README + AGENTS.md)

## Objective
Document the new Supervisor discovery capability for both human users (`README.md`) and future contributors (`AGENTS.md`): that radios published by the rtl_433 add-on are now auto-discovered and added with one click, and that discovered/adopted entries are keyed by the add-on's stable per-radio `unique_id` while manual adds keep the `hub:{host}:{port}` scheme.

## Skills Required
- `technical-writing` — clear, concise user- and contributor-facing documentation matching the existing tone.

## Acceptance Criteria
- [ ] `README.md` setup/installation section explains that, under Home Assistant OS with the rtl_433 add-on installed, radios are auto-discovered and appear as cards in **Settings → Devices & Services**, addable in one click after a confirmation prompt.
- [ ] `README.md` notes that discovered radios stay stable across restarts and port reassignments because the add-on advertises a stable per-radio `unique_id` (and that manual setup via host/port is still supported).
- [ ] `AGENTS.md` documents the supported config-flow sources (now including `hassio` discovery) and the dual identity scheme (`hub:{host}:{port}` for manual adds; the add-on's stable radio `unique_id` for discovered/adopted entries) and why both coexist (adoption/migration on host:port match).
- [ ] No code or version/changelog files are touched (release-please-managed); documentation only.

## Technical Requirements
- Match the existing Markdown style/heading structure of `README.md` and `AGENTS.md`.
- Keep claims accurate to the implementation from Task 1 (confirmation step required; adoption re-keys an existing matching entry; reconfigure preserves a stable id).

## Input Dependencies
- Task 1: the implemented discovery behaviour (to describe it accurately).

## Output Artifacts
- Updated `README.md` and `AGENTS.md`.

## Implementation Notes

<details>
<summary>What to write and where</summary>

**README.md** — locate the existing installation/setup section (where manual host/port setup is described). Add a short subsection, e.g. "Automatic discovery (Home Assistant OS add-on)":
- When the [rtl_433 add-on](https://github.com/rtl-433-hass/rtl_433-hass-addons) is installed and running, each detected radio is published to Home Assistant's Supervisor discovery and shows up under **Settings → Devices & Services** as a discovered "rtl_433" card. Click **Add**, confirm, and the radio is configured automatically.
- Each radio keeps the same Home Assistant config entry (and its device history) across add-on restarts and port reassignments, because the add-on advertises a stable per-radio identifier. Multi-dongle stability is best when each dongle stays in a fixed USB port or is flashed with a unique serial.
- Manual setup (entering host/port yourself) remains fully supported for non-add-on / remote rtl_433 servers; a radio is never added twice if it was already discovered.

Keep it brief (a short paragraph or a few bullets); do not duplicate the add-on README's hardware-identity details — link to the add-on for that.

**AGENTS.md** — find where the config flow / config entries are described (or the architecture/overview section). Add a concise note:
- Config-flow sources supported: `user` (manual), `reconfigure`, `hassio` (Supervisor add-on discovery), plus the options flow.
- Identity schemes: manually-added hubs use `unique_id = hub:{host}:{port}`; radios discovered via the add-on use the add-on's advertised stable per-radio `unique_id` (`serial:…` / `usbpath:…` / `template:…`). On a discovery message that matches an existing entry by `host:port`, that entry is adopted/re-keyed onto the stable id (so manual and discovered never duplicate, and history is preserved). `reconfigure` preserves a stable id rather than recomputing `hub:{host}:{port}`.

Do not modify `manifest.json`, `CHANGELOG.md`, or any version field.
</details>
