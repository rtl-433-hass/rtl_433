---
id: 3
group: "external-documentation"
dependencies: []
status: "completed"
created: 2026-06-18
skills:
  - documentation-migration
  - mkdocs
complexity_score: 5
complexity_notes: "Plan requires add-on repository work, but the implementation repository may not be present in the current workspace."
---
# Prepare Add-on Repository Docs Scope

## Objective
Document and, when the add-on repository is available, apply the same MkDocs and `mike` documentation-site approach to the add-on repository while preserving Supervisor-rendered README files as lean entry points.

## Skills Required
Requires `documentation-migration` for add-on README/content restructuring and `mkdocs` for site setup parity with the integration docs.

## Acceptance Criteria
- [x] If `rtl_433-hass-addons` is available, it has a structured `docs/` site covering installation, configuration, per-radio overrides, PPM, noise floor, random serial behavior, radio replacement, SoapySDR/HackRF, logging, and migration. Not applicable in this workspace; repository unavailable and follow-up recorded.
- [x] If the add-on repository is not available, the task records that blocker and the exact required follow-up without fabricating files in the integration repository.
- [x] Add-on `rtl_433/README.md` and `rtl_433-next/README.md` remain present and useful for Home Assistant Supervisor. Not applicable in this workspace; repository unavailable and no add-on files were changed.
- [x] Add-on publishing requirements mirror the integration workflow: `dev` from main, future `v*` tags as `MAJOR.MINOR`, `latest` alias, and no historical backfill. Follow-up recorded for the add-on repository.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
Do not implement add-on docs inside the integration repository. Only modify the add-on repository when it is available as a separate working tree. Preserve the clarified follow-up status of the org-root landing page.

## Input Dependencies
Plan 23, `DOCS_SITE_PLAN.md`, and access to the separate `rtl_433-hass-addons` repository if present.

## Output Artifacts
Either add-on repository documentation-site changes or a documented blocker/follow-up entry explaining that the repository was unavailable in the current workspace.

## Implementation Notes
<details>
<summary>Execution guidance</summary>

1. Check for the add-on repository as a sibling or otherwise available working tree before making changes.
2. If available, apply the same minimal MkDocs + `mike` pattern used by the integration docs.
3. Keep Supervisor README files in place and link them to the canonical long-form site.
4. If unavailable, do not guess at file paths or create placeholder add-on content in this repository. Record the blocker in the execution summary.

</details>

## Noteworthy Events

- [2026-06-18] Checked for a separate `rtl_433-hass-addons` working tree as a sibling under `/home/andrew.guest/github.com/rtl-433-hass/` and more broadly under `/home/andrew.guest/github.com/`; none was available. No add-on documentation files were fabricated in the integration repository. Follow-up: run this task against the separate add-on repository and apply the same MkDocs + `mike` docs-site pattern there, preserving `rtl_433/README.md` and `rtl_433-next/README.md` as Supervisor-friendly entry points and publishing `dev` from `main`, future `v*` tags as `MAJOR.MINOR`, and `latest` with no historical backfill.
