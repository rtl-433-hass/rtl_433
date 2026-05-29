---
id: 6
group: "docs"
dependencies: [4]
status: "pending"
created: 2026-05-28
skills:
  - technical-writing
---
# Documentation: mutation testing workflow

## Objective
Document the mutation-testing workflow for AI agents (AGENTS.md) and humans (CONTRIBUTING.md): how to run mutmut, read survivors, add tests to kill them, the 70% floor, PR-floor/main-strict gates, and the upward-only baseline rule.

## Skills Required
- technical-writing

## Acceptance Criteria
- [ ] AGENTS.md has a mutation-testing section
- [ ] CONTRIBUTING.md has a short contributor note
- [ ] Mentions: no `# pragma: no mutate`, no disabled mutators, baseline ratchets upward only

## Technical Requirements
Match existing doc tone/structure.

## Input Dependencies
Task 4 (final tooling/commands).

## Output Artifacts
Updated AGENTS.md, CONTRIBUTING.md.

## Implementation Notes
<details>
Include concrete commands: `uv run mutmut run`, `uv run mutmut results`, `scripts/mutation_ratchet.py`.
</details>
