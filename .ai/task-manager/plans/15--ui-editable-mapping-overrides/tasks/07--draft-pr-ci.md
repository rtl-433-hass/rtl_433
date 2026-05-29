---
id: 7
group: "delivery"
dependencies: [5, 6]
status: "pending"
created: 2026-05-28
skills:
  - git
  - github-actions
---
# Open draft PR and drive CI to green

## Objective
Push the feature branch, open a **draft** PR against `main` with a Conventional-Commits-compliant title, then watch CI and fix any failures until every required check is green. Leave the PR in draft for human review.

## Skills Required
- `git`, `github-actions`: branch/push, `gh` CLI, reading CI logs.

## Acceptance Criteria
- [ ] All phase commits are present; branch pushed to `origin`.
- [ ] A **draft** PR is open against `main` with a conventional title (e.g. `feat(rtl_433): edit mapping overrides in the UI`) and a body summarising the change, the BC break (file → per-hub UI storage), and the one-time import migration.
- [ ] CI checks Test, Lint, Validate, Conventional Commits, and CodeQL all report success.
- [ ] Any CI-only failures are fixed on the branch and pushed until green.
- [ ] PR remains in **draft** (not merged, not marked ready).

## Technical Requirements
- `gh pr create --draft --base main`.
- `gh pr checks --watch` to monitor; `gh run view --log-failed` to read failures.

## Input Dependencies
- Tasks 5, 6 complete (code + tests + docs).

## Output Artifacts
- A green, draft PR.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

- Ensure the working tree is clean and all phase commits exist (`git status`, `git log --oneline main..HEAD`).
- `git push -u origin feature/15--ui-editable-mapping-overrides`.
- Create the PR:
  `gh pr create --draft --base main --title "feat(rtl_433): edit mapping overrides in the UI" --body "<summary>"`.
  Body must mention: in-UI YAML editor for per-hub mapping overrides, validation before save, automatic reload (no HA restart), the BC break (global file → per-hub `entry.data` storage), and the one-time `rtl_433_mappings.yaml` import migration (file left on disk, then ignored).
- `gh pr checks --watch` (or poll `gh pr checks`). The Conventional Commits check validates the PR title — the title above complies.
- For any failure, `gh run view <run-id> --log-failed`, fix on the branch, commit (conventional message), push, re-watch.
  - Likely CI-only issues: hassfest complaining about the manifest/version, a Python-version-specific test, ruff/format, or markdownlint. Reproduce locally where possible (`uv run pytest`, `ruff check`, `pre-commit run -a`).
- Do NOT mark ready-for-review or merge. Report the PR URL and final check status.
</details>
