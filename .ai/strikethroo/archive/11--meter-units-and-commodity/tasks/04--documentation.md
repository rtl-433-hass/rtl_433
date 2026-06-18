---
id: 4
group: "meter-units"
dependencies: [1, 2]
status: "completed"
created: 2026-05-28
skills:
  - technical-writing
---
# Document model-scoped library + calibration

## Objective
Document the `models:` schema, the specificity-first precedence, the per-device calibration step, and the statistics caveat, including the illustrative non-real-model worked example.

## Skills Required
- `technical-writing`.

## Acceptance Criteria
- [ ] `docs/device-library.md`: document the `models:` model-scoped schema (structure, additive/optional, same per-field attribute schema), the model-aware lookup resolution order, and the **specificity-first** precedence (model-scoped beats global regardless of source; user file beats shipped within each tier). Document the user-override file's `models:` support. Include the **illustrative, clearly-labeled non-real-model worked example** here (consumption → `device_class: energy` + convertible base unit + `total_increasing` + scale).
- [ ] `README.md`: add/extend a utility-meter section: the per-device calibration step (commodity + base unit + scale), how it makes the consumption sensor Energy-dashboard-eligible, that HA does its own display-unit conversion once a convertible base unit is set, and the caveat that recalibration orphans prior long-term statistics.
- [ ] `AGENTS.md`: short note pointing to the `models:` block, the calibration sub-record in `entry.data[CONF_DEVICES]`, the specificity-first precedence, and that calibration reloads via `_async_update_listener`; defer detail to `docs/device-library.md`.

## Technical Requirements
- Files: `docs/device-library.md`, `README.md`, `AGENTS.md`. (Translations are handled in task 2.)

## Input Dependencies
- Tasks 1 and 2 (describe shipped behavior accurately).

## Output Artifacts
- Updated docs.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Read the implemented `mapping.py` lookup + the calibration record shape so docs match reality. Search for sections (files edited by earlier plans on this branch).
- Make the worked example UNMISTAKABLY illustrative ("not a real meter model") so no one copies it as a live mapping.
- Keep README user-facing and concise; keep AGENTS.md contributor-facing and brief.
</details>
