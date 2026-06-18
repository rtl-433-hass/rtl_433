---
id: 2
group: "documentation-publishing"
dependencies: [1]
status: "completed"
created: 2026-06-18
skills:
  - github-actions
  - mkdocs
complexity_score: 5
complexity_notes: "Adds release-aware documentation publishing and validates tag-to-version behavior."
---
# Add Docs Publishing Workflow

## Objective
Add GitHub Actions publishing for the integration documentation site so main publishes a development version and future release tags publish stable `MAJOR.MINOR` versions with `latest` alias support.

## Skills Required
Requires `github-actions` for workflow implementation and `mkdocs` for `mike` deployment behavior.

## Acceptance Criteria
- [x] `.github/workflows/docs.yml` builds the documentation site on relevant pushes/pull requests.
- [x] Pushes to `main` publish a `dev` documentation version through `mike`.
- [x] Future `vX.Y.Z` tags derive an `X.Y` documentation version, update the `latest` alias, and set `latest` as default.
- [x] Workflow uses `contents: write` where publishing is required and fetches full tag history.
- [x] No historical documentation backfill task or workflow step is introduced.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
Use `mike deploy --push dev` for development docs and `mike deploy --push --update-aliases <X.Y> latest` followed by `mike set-default --push latest` for release tags. Keep branch management on `gh-pages`. Do not introduce separate hosting or versioning systems.

## Input Dependencies
Task 1's MkDocs configuration and tooling files.

## Output Artifacts
Documentation GitHub Actions workflow and any small supporting scripts or comments needed for maintainability.

## Implementation Notes
<details>
<summary>Execution guidance</summary>

1. Inspect existing workflows before adding a new one to match repository conventions.
2. Keep the workflow narrow: build validation and publish behavior only.
3. Ensure release version derivation handles tags shaped like `v0.17.0` and produces `0.17`.
4. Avoid a backfill job. The first stable site version should come from the next normal release tag.
5. Add a concise generated-output warning if needed so maintainers know not to hand-edit `gh-pages`.

</details>

## Noteworthy Events
- [2026-06-18] Added a narrow docs workflow with read-only build validation for docs changes and publishing limited to `push` events on `main` or `v*.*.*` tags. The publishing job checks out full history, grants `contents: write`, deploys `main` as `dev`, derives release docs versions such as `0.17` from tags such as `v0.17.0`, updates the `latest` alias, and sets `latest` as the mike default. No historical backfill step was added.
