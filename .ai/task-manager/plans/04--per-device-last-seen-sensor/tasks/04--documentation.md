---
id: 4
group: "documentation"
dependencies: [1, 2]
status: "completed"
created: "2026-05-26"
skills:
  - technical-writing
---
# Document the per-device "Last seen" sensor

## Objective
Document the new synthetic per-device "Last seen" diagnostic timestamp sensor in
the user-facing README and the contributor-facing AGENTS.md, capturing both its
user value (staleness alerting) and the two design invariants future refactors
must preserve. `WEBSOCKET_API.md` needs no change (this plan adds no API usage).

## Skills Required
- `technical-writing` — concise, accurate user- and contributor-facing docs that match the repo's existing tone.

## Acceptance Criteria
- [ ] **README.md** — in the feature/availability section, mention the per-device "Last seen" diagnostic sensor (`device_class=timestamp`), noting it is created for **every** device, is enabled by default, and **stays available after a device goes silent** (unlike the measurement sensors), so it can drive "last_seen older than X" staleness automations.
- [ ] **AGENTS.md** — note that the Last-seen sensor is a synthetic, non-field-driven per-device entity built from a synthetic `FieldDescriptor` + a dedicated `Rtl433LastSeenSensor` subclass, created unconditionally on the **sensor** platform (via the `per_device_factory` hook in `async_setup_hub_platform`), with an always-available override and a value sourced from `coordinator.last_seen` guarded by `coordinator.devices` presence (so it never shows the base `async_added_to_hass` startup baseline). State that future refactors of `async_setup_hub_platform` and the base `async_added_to_hass` baseline must preserve this.
- [ ] **WEBSOCKET_API.md** — confirmed no change required (do not edit).
- [ ] Wording is consistent with the existing docs' style and accurate to what Tasks 1–2 actually implemented.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `README.md`, `AGENTS.md`.
- Match the existing heading structure and tone; do not restructure unrelated
  sections.

## Input Dependencies
- Task 1 and Task 2: the implemented behavior the docs describe (entity design,
  `per_device_factory` wiring, always-available override, restore-not-baseline).

## Output Artifacts
- Updated README.md and AGENTS.md (Plan "Documentation" section satisfied).

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### README.md
Find where the integration's entities / availability behavior is described
(near the per-device sensors and the availability-timeout discussion) and add a
short paragraph or bullet such as:

> **Last seen** — every device also gets a diagnostic `timestamp` sensor named
> "Last seen" that reports when the device was last heard from. Unlike the
> measurement sensors (which become *unavailable* after the availability timeout
> elapses with no transmission), the Last seen sensor stays available and keeps
> showing the last-heard time, so you can build "no signal for N minutes"
> staleness alerts and dashboards. It restores its previous value across
> restarts.

Keep it brief and consistent with the surrounding prose.

### AGENTS.md
Add a note in the entities/architecture area capturing the invariants (a future
maintainer reading AGENTS.md must not regress these):

> The per-device **Last seen** sensor (`Rtl433LastSeenSensor`, `sensor.py`) is
> synthetic — it is not driven by a device-library field. It is built from a
> small synthetic `FieldDescriptor` (sentinel `field_key`, `object_suffix=
> "last_seen"`, `device_class=timestamp`, diagnostic, enabled) and created once
> per device on the **sensor** platform via the `per_device_factory` hook in
> `async_setup_hub_platform` (the `binary_sensor` platform passes no factory). It
> holds its **own** `native_value`: seeded from `coordinator.last_seen` only when
> `coordinator.devices[device_key]` exists (a real event), restored as a tz-aware
> datetime otherwise, and updated from `coordinator.last_seen` on dispatch — it
> must never display the base `async_added_to_hass` "baseline last_seen = now",
> or it would show "now" after every restart. It overrides `available` to be
> true whenever it has a value, so it stays readable after the device goes
> silent. Refactors of `async_setup_hub_platform` (the per-device factory hook)
> and of the base baseline must preserve both invariants.

### WEBSOCKET_API.md
No change. Do not edit it; this plan adds no new WebSocket/HTTP API usage.

### Gotchas
- Describe only what Tasks 1–2 implemented; if an implementation detail changed
  during those tasks (e.g. the exact parameter name), reflect the real names.
- Don't duplicate the same paragraph into both files verbatim — README is for
  users (behavior/value), AGENTS.md is for maintainers (invariants/why).
</details>
