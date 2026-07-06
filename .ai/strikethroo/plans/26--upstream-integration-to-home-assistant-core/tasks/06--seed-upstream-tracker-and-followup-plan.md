---
id: 6
group: "documentation"
dependencies: []
status: "pending"
created: 2026-07-06
skills:
  - technical-writing
  - home-assistant
---
# Seed CORE_UPSTREAM.md Tracker and Ordered Follow-Up PR Plan

## Objective
Create `CORE_UPSTREAM.md` at the root of this repository: a per-module ledger of upstreamed-vs-HACS-only status plus landing commit/PR, AND an ordered follow-up PR sequence for the remaining modules, sequenced by the quality-scale tier each unlocks rather than by feature preference. This prevents the long-lived core branch from silently drifting over a months-long review.

## Skills Required
- **technical-writing**: a clear, maintainable tracking document.
- **home-assistant**: understanding which modules map to which quality-scale tiers/platforms.

## Acceptance Criteria
- [ ] `CORE_UPSTREAM.md` exists at the repo root.
- [ ] It contains a per-module table covering every module in `custom_components/rtl_433/` with columns: module, status (upstreamed / HACS-only / in-PR), and landing commit/PR (blank until known).
- [ ] `sensor`, `manifest`, `config_flow`, `const`, `__init__`/coordinator are marked as PR1 (Bronze) scope.
- [ ] An ordered follow-up sequence lists: `binary_sensor`, `event`, `device_trigger`, `number`, `select`, `switch`, `repairs`, `calibration`, `device_library`, `mapping`, `diagnostics`, `options_flow`, `hub_settings`, `sdr_settings` — each with a one-line rationale for its position and the quality tier it targets.
- [ ] The document notes that opening PRs, the docs PR (home-assistant.io), and the brands PR are out of scope for this run.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Derive the module inventory from the actual contents of `custom_components/rtl_433/`.
- This is a documentation deliverable; it writes no integration code.

## Input Dependencies
None hard. (Soft: reflects the PR1 scope defined by Task 4, but the ledger can be authored from the plan and the current module list.)

## Output Artifacts
- `CORE_UPSTREAM.md` at the repo root — the ongoing delta tracker and upstreaming roadmap.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

- List the current modules: `ls custom_components/rtl_433/*.py` plus the `coordinator/`, `mapping/`, `device_library/` subpackages. Include every one in the table so nothing is silently omitted.
- Suggested ordering rationale (adjust to real quality-scale rule dependencies):
  1. PR1 (Bronze): `sensor` + config flow + coordinator (already scaffolded).
  2. `binary_sensor` — second platform, low risk, reinforces platform patterns.
  3. `diagnostics` — Silver-tier requirement; cheap and expected early.
  4. `event` — adds the event platform (core to rtl_433's push model).
  5. `device_trigger` — depends on `event`.
  6. `number` / `select` / `switch` — SDR control surfaces; group by shared `hub_settings`/`sdr_settings` dependency.
  7. `repairs` — Silver/Gold issue-registry surface.
  8. `options_flow` — configuration UX once platforms exist.
  9. `calibration`, `device_library`, `mapping` — richer normalization/UX; later tiers.
- Table format example:

  | Module | Status | Landing PR/commit |
  | --- | --- | --- |
  | sensor | in-PR (PR1, Bronze) | |
  | binary_sensor | HACS-only | |
  | event | HACS-only | |
  | ... | ... | |

- Add a short header explaining the single-shared-domain strategy and that this file is the source of truth for upstreaming progress; link it from `AGENTS.md`/`CLAUDE.md` if that note is added.
- Keep it terse and update-friendly — it will be edited every time a PR lands.
</details>
