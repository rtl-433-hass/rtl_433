---
id: 5
group: "ci"
dependencies: [4]
status: "pending"
created: 2026-05-28
skills:
  - github-actions
---
# CI Mutation Workflow (full run, floor on PR / strict on main)

## Objective
Add a `.github/workflows/mutation.yml` that runs the full mutation pass on PRs and `main`, with pytest-xdist parallelism, a hard timeout, and mutmut-state caching, then invokes the comparator (floor on PR, strict on main).

## Skills Required
- github-actions

## Acceptance Criteria
- [ ] New workflow separate from `test.yml`
- [ ] Runs full `mutmut run` under `uv` on PR + push to main
- [ ] pytest-xdist parallelism + hard `timeout-minutes`
- [ ] `actions/cache` for `mutants/`/`.mutmut-cache` keyed on source hash
- [ ] PR job runs comparator floor mode; main job runs strict mode
- [ ] Pinned action SHAs consistent with existing workflows

## Technical Requirements
Mirror `test.yml` setup (setup-uv, Python 3.14, `uv pip install -r requirements_test.txt` + mutmut). Use `astral-sh/setup-uv` and `actions/checkout` SHAs already used in the repo.

## Input Dependencies
Task 4 (comparator + baseline).

## Output Artifacts
`.github/workflows/mutation.yml`.

## Implementation Notes
<details>
Use the same pinned SHAs as existing workflows. `permissions: {}` baseline; add `contents: read`. Provide a hard timeout so a runaway run fails loudly.
</details>
