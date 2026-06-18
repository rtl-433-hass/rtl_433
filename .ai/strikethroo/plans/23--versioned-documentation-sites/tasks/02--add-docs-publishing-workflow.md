---
id: 2
group: "documentation-publishing"
dependencies: [1]
status: "pending"
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
- [ ] `.github/workflows/docs.yml` builds the documentation site on relevant pushes/pull requests.
- [ ] Pushes to `main` publish a `dev` documentation version through `mike`.
- [ ] Future `vX.Y.Z` tags derive an `X.Y` documentation version, update the `latest` alias, and set `latest` as default.
- [ ] Workflow uses `contents: write` where publishing is required and fetches full tag history.
- [ ] No historical documentation backfill task or workflow step is introduced.

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
