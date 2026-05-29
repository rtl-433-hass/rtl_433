---
id: 4
group: "ratchet"
dependencies: [2, 3]
status: "pending"
created: 2026-05-28
skills:
  - python
---
# Per-File Baseline File and Ratchet Comparator Script

## Objective
Capture the post-hardening per-file mutation results into a committed baseline JSON and add a dependency-light comparator script with PR (per-file floor) and main (per-file strict) modes.

## Skills Required
- python

## Acceptance Criteria
- [ ] Committed baseline file (per file: killed, total, score)
- [ ] Comparator script under `scripts/` with `--mode floor` and `--mode strict`
- [ ] floor mode: non-zero exit if any file's score < baseline; prints regressed file + new survivors
- [ ] strict mode: non-zero exit if current results differ from baseline for any file
- [ ] Both modes runnable locally via the venv

## Technical Requirements
Parse mutmut results (prefer `mutmut export-cicd-stats` or `mutmut results`) into per-file scores; compare to baseline JSON; stdlib only.

## Input Dependencies
Tasks 2,3 (final scores), Task 1 (results format).

## Output Artifacts
`scripts/mutation_ratchet.py`, baseline JSON (e.g. `scripts/mutation_baseline.json`).

## Implementation Notes
<details>
Ratchet on per-file score (killed/total), not mutant IDs. floor allows improvements; strict requires exact match. Exit codes drive CI. No external deps.
</details>
