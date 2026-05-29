---
id: 7
group: "validation"
dependencies: [5, 6]
status: "pending"
created: 2026-05-28
skills:
  - github-actions
  - bash
---
# Babysit the Mutation Workflow on a Draft PR

## Objective
Push the branch, open a draft PR, and watch the mutation job via `gh` until green — tuning timeout/xdist, validating caching, stabilizing CI-only flakiness, and demonstrating the floor gate fails in CI on a deliberate regression (then reverting).

## Skills Required
- github-actions, bash (gh CLI)

## Acceptance Criteria
- [ ] Draft PR opened
- [ ] Mutation workflow reliably green on the PR
- [ ] Full run completes within the hard timeout with headroom
- [ ] Floor gate demonstrated to fail in CI on a deliberate regression, then reverted
- [ ] PR ready for review only after the above

## Technical Requirements
`gh pr create --draft`, `gh run watch`/`gh pr checks`. User has authorized push + PR.

## Input Dependencies
Tasks 5 (workflow), 6 (docs).

## Output Artifacts
A green draft PR; any CI-driven tuning committed.

## Implementation Notes
<details>
Never fix CI flakiness by skipping/loosening tests. If runtime is too high, tune xdist worker count and timeout. Confirm cache populates on first run and reuses on a follow-up push.
</details>
