---
id: 1
group: "documentation-site"
dependencies: []
status: "completed"
created: 2026-06-18
skills:
  - mkdocs
  - documentation-migration
complexity_score: 6
complexity_notes: "Migrates broad existing README/docs content into a structured MkDocs site within the integration repository."
---
# Create Integration Docs Site

## Objective
Create the integration repository's MkDocs documentation source tree and migrate long-form user documentation out of the README and supporting Markdown files into structured site pages.

## Skills Required
Requires `mkdocs` for site structure/configuration awareness and `documentation-migration` for preserving existing user guidance while reorganizing content.

## Acceptance Criteria
- [x] Root `mkdocs.yml` and documentation tooling dependencies exist for the integration repository.
- [x] `docs/` contains pages for installation, configuration, discovery, availability, diagnostics, hub entities, device library, calibration, WebSocket API, multiple servers, and screenshots.
- [x] Integration README is reduced to a concise overview with badges and a prominent full-documentation link.
- [x] Existing local documentation references and image paths are rewritten for the new site structure.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
Use Material for MkDocs and `mike`. Keep authoring Markdown-first. Include required Markdown extensions: `admonition`, `pymdownx.superfences`, `pymdownx.highlight`, `tables`, and `toc` with permalinks. Preserve existing `docs/device-library.md`, migrate `WEBSOCKET_API.md` into the site, and keep `CONTRIBUTING.md` as a repository-level contributor document.

## Input Dependencies
Plan 23, `DOCS_SITE_PLAN.md`, current `README.md`, `WEBSOCKET_API.md`, existing `docs/`, and existing screenshot/image assets.

## Output Artifacts
Integration repository `mkdocs.yml`, documentation tooling dependency file or pyproject docs extra, structured `docs/` pages, rewritten image/link references, and trimmed `README.md`.

## Implementation Notes
<details>
<summary>Execution guidance</summary>

1. Read `DOCS_SITE_PLAN.md`, `README.md`, `WEBSOCKET_API.md`, and existing files under `docs/` before moving content.
2. Add only the MkDocs tooling needed for this plan: Material for MkDocs and `mike`.
3. Build the `docs/` tree around the page list in the acceptance criteria. Prefer moving existing text over rewriting from scratch.
4. Preserve current user-facing content meaning, but remove old README anchor compatibility and redirect work from scope.
5. Keep the README useful as a repository front page, not a full manual.
6. Do not modify integration runtime code unless needed to update documentation links.

</details>

## Noteworthy Events
- [2026-06-18] The dependency helper did not resolve task ID `001`, but resolved the same task as ID `1`; execution continued against the explicit `01--create-integration-docs-site.md` task file supplied by the user.
- [2026-06-18] `uv run mkdocs --version` showed MkDocs was not installed in the current environment, so validation used `uv run --with mkdocs-material --with mike mkdocs build --strict` with transient dependencies.
