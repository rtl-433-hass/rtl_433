---
id: 4
group: "validation"
dependencies: [1, 2, 3]
status: "completed"
created: 2026-06-18
skills:
  - mkdocs
  - qa-validation
---
# Validate Docs Plan Deliverables

## Objective
Validate that the documentation-site implementation satisfies Plan 23 without adding out-of-scope historical backfill, old-link redirects, landing-page implementation, or extra link-checking dependencies.

## Skills Required
Requires `mkdocs` for strict builds and local site inspection, plus `qa-validation` for checking deliverables against plan scope.

## Acceptance Criteria
- [x] Integration docs build locally with MkDocs strict mode.
- [x] Generated integration navigation, required pages, README links, and image references are manually inspected.
- [x] Tag-derived workflow logic is checked so future `vX.Y.Z` tags map to `X.Y` and update `latest`.
- [x] Add-on repository status is verified as implemented.
- [x] Org-root landing page follow-up is recorded without implementing that third repository in this plan.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
Use strict MkDocs builds and manual generated-site inspection. Do not add a separate link checker. Follow the plan's Self Validation section.

## Input Dependencies
Tasks 1, 2, and 3 outputs.

## Output Artifacts
Validation results, any small corrective documentation/workflow fixes, and execution-summary notes for blockers or follow-ups.

## Implementation Notes
<details>
<summary>Execution guidance</summary>

Write a few tests, mostly integration. Meaningful tests verify custom business logic, critical paths, and edge cases specific to this application. Test your code, not the framework or library. For this documentation migration, prefer strict MkDocs builds and workflow/tag logic checks over unit tests for third-party tooling.

1. Run the documented MkDocs strict build command for the integration site.
2. If local serving is practical, inspect the generated site pages and navigation; otherwise inspect generated files and record the limitation.
3. Validate workflow tag parsing using shell-safe local checks where possible without publishing.
4. Confirm no historical backfill, README anchor redirect map, separate link checker, or org-root implementation was added.
5. Record any unavailable external repository as a follow-up, not a silent success.

</details>

## Noteworthy Events

- [2026-06-18] Ran `uv run --with mkdocs-material --with mike mkdocs build --strict`; the integration documentation built successfully. Generated output contained the expected navigation pages for Home, Installation, Configuration, Discovery, Availability, Diagnostics, Hub Entities, Device Library, Utility-Meter Calibration, WebSocket API, Multiple Servers, and Screenshots.
- [2026-06-18] Inspected generated image assets and confirmed the four screenshot references in `docs/screenshots.md` were copied to `site/images/`. Inspected `README.md` and confirmed it remains concise with canonical full-documentation, installation, and configuration links to `https://rtl-433-hass.github.io/rtl_433/latest/`.
- [2026-06-18] Simulated the docs workflow tag regex locally: `v1.2.3` maps to `1.2` and `v10.20.30` maps to `10.20`, while malformed refs are unsupported. `.github/workflows/docs.yml` updates the `latest` alias with `mike deploy --push --update-aliases "$version" latest` and sets it as default with `mike set-default --push latest`.
- [2026-06-18] Re-ran validation after add-on docs implementation. Both repositories passed strict local builds with transient tooling: `uv run --with mkdocs-material --with mike mkdocs build --strict` in `/home/andrew.guest/github.com/rtl-433-hass/rtl_433` and `/home/andrew.guest/github.com/rtl-433-hass/rtl_433-hass-addons`.
- [2026-06-18] Verified generated page coverage in both repositories: the integration site contains the required Home, Installation, Configuration, Discovery, Availability, Diagnostics, Hub Entities, Device Library, Utility-Meter Calibration, WebSocket API, Multiple Servers, and Screenshots pages; the add-on site contains Home, Installation, Configuration, and Advanced pages covering per-radio overrides, PPM, noise floor, random serial behavior, radio replacement, SoapySDR/HackRF, logging, and migration guidance.
- [2026-06-18] Inspected add-on README entry points (`README.md`, `rtl_433/README.md`, and `rtl_433-next/README.md`) and confirmed they remain Supervisor-friendly concise pages with canonical links to `https://rtl-433-hass.github.io/rtl_433-hass-addons/latest/` and its Installation, Configuration, and Advanced pages.
- [2026-06-18] Verified both docs workflows derive future `vX.Y.Z` tags to `X.Y`, publish `dev` from `main`, update the `latest` alias with `mike deploy --push --update-aliases "$version" latest`, and set the default with `mike set-default --push latest`. Local simulation mapped `v1.2.3` to `1.2` and `v10.20.30` to `10.20`; malformed refs were unsupported.
- [2026-06-18] Confirmed out-of-scope work was not added in either repository: no historical docs backfill workflow, README-anchor redirect map, separate link-checker dependency, or organization-root landing-page implementation. The org-root landing page remains recorded as follow-up work in Plan 23.
