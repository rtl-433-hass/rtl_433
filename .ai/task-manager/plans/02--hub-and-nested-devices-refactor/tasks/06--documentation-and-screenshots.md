---
id: 6
group: "docs"
dependencies: [2, 3, 4]
status: "pending"
created: "2026-05-26"
skills:
  - technical-writing
  - playwright
---
# Documentation & screenshot refresh for the nested model

## Objective
Update all human- and agent-facing docs to describe the single-hub + nested-devices model and remove the Battery Notes per-device discovery narrative. Recapture the documentation screenshots via the existing harness; if the harness cannot run in this environment, deliver the prose updates and clearly flag the exact screenshots that still need recapture (plan risk G7).

## Skills Required
- `technical-writing`: rewrite README/AGENTS sections accurately.
- `playwright`: drive the screenshot harness (best-effort; non-blocking).

## Acceptance Criteria
- [ ] `README.md`: the intro/feature bullets, the **Discovery** section, the **Per-device override** section, the **Options** section, and the **architecture** section describe nested devices (auto-added, gated by the discovery toggle; removable from the device page; per-device timeout via the hub options flow). No remaining references to per-device config entries, accept/ignore discovery cards, or `SOURCE_IGNORE`.
- [ ] `AGENTS.md`: the "Repository shape" / model notes reflect one hub entry with nested devices and mention `async_remove_config_entry_device` + the `async_migrate_entry` migration. Keep the file-list accurate to the package.
- [ ] Every image referenced by `README.md` exists in `docs/images/`.
- [ ] Screenshots in `docs/images/` are recaptured to show the nested topology (device page under the hub) and the reworked options flow — OR, if the harness cannot run, the prose is updated and the specific stale images are listed with the exact `tests/integration/run-harness.sh full` invocation needed to regenerate them.
- [ ] `docs/device-library.md` is left unchanged (mapping/library system is untouched).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `README.md`, `AGENTS.md`, `docs/images/*`.
- Harness: `tests/integration/` (Docker + Node ws-bridge + Playwright); see `tests/integration/README.md`.

## Input Dependencies
- Tasks 2, 3, 4 (final behavior to document). Soft dependency on Task 5 (a green suite confirms the documented behavior).

## Output Artifacts
- Accurate README/AGENTS and current screenshots (or a precise recapture TODO).

## Implementation Notes

<details>
<summary>Detailed steps</summary>

**README.md** — rewrite the model narrative:
- Replace "Battery Notes-style discovery — each newly observed device appears as a discovery card you accept/dismiss" with: newly observed devices are **auto-added as nested devices under the hub**, gated by the per-hub **discovery toggle**; unwanted devices are **removed from their device page** (and re-appear only if discovery is on and they transmit again).
- **Discovery** section: drop the accept/ignore card workflow and `SOURCE_IGNORE`; describe the toggle gating runtime auto-add.
- **Per-device override**: now reached via **Settings → Devices & Services → rtl_433 → Configure → Device** (the hub options flow device step), not a per-device entry.
- **Options** section: hub Configure exposes a menu (Hub settings: discovery toggle + default timeout; Device settings: per-device timeout override).
- **Architecture** section: one config entry per server (the hub); RF devices are device-registry devices nested under it; deleting the hub removes them all; mention the in-place 0.1.0 migration preserves entities.
- Update any screenshot captions/links to match the new images.

**AGENTS.md** — update "Repository shape" and model notes: one hub config entry, nested device-registry devices, `async_remove_config_entry_device` for stale-device removal, `async_migrate_entry` for the 0.1.0 upgrade. Fix the stale `coordinator.py` reference to the `coordinator/` package if you touch that area, but keep edits minimal and accurate.

**Screenshots** — attempt recapture:
- Follow `tests/integration/README.md` (e.g. `cd tests/integration && ./run-harness.sh full`). This needs Docker, Node, and the bundled RF captures.
- If it runs: replace `docs/images/01-discovery-card.png` (no longer applicable) with a device-page / nested-topology image, and update the options-flow image to show the menu. Ensure README references match the produced filenames.
- If it cannot run here (no Docker, etc.): DO NOT block. Update the prose, keep README references valid (point to existing images, or add a short note), and add a clearly labelled list under the task output of which images need regeneration plus the exact command. Surface this in the final report.

**Validation:** `grep -ni "battery notes\|discovery card\|source_ignore\|per-device config entry\|accept the card" README.md` returns nothing; confirm every `docs/images/...` path referenced in `README.md` exists on disk.
</details>
