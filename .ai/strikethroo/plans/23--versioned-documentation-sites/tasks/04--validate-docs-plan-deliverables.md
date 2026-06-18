---
id: 4
group: "validation"
dependencies: [1, 2, 3]
status: "pending"
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
- [ ] Integration docs build locally with MkDocs strict mode.
- [ ] Generated integration navigation, required pages, README links, and image references are manually inspected.
- [ ] Tag-derived workflow logic is checked so future `vX.Y.Z` tags map to `X.Y` and update `latest`.
- [ ] Add-on repository status is verified as implemented or documented as blocked/follow-up.
- [ ] Org-root landing page follow-up is recorded without implementing that third repository in this plan.

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
