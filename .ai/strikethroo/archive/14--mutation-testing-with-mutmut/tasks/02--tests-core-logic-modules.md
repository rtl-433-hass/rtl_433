---
id: 2
group: "test-writing"
dependencies: [1]
status: "pending"
created: 2026-05-28
skills:
  - python
  - unit-testing
---
# Tests to Raise Core-Logic Modules to ≥70% Mutation Score

## Objective
Write/strengthen pytest tests so the core-logic modules below the 70% per-file floor reach it (stretch 80–90% where cheap): normalizer, mapping, calibration, coordinator, entity.

## Skills Required
- python, unit-testing (HA custom-component test harness)

## Acceptance Criteria
- [ ] Each targeted core module ≥70% mutation score (stretch higher where cheap)
- [ ] New tests assert real behavior (values, dispatched signals, attributes), not coverage-only
- [ ] No `# pragma: no mutate`, no disabled mutators, no skipped/xfail/weakened tests
- [ ] Full suite still green

## Technical Requirements
Use `mutmut results` + `mutmut show <mutant>` to see survivors; add deterministic tests under `tests/`.

## Input Dependencies
Task 1 baseline per-file scores and survivor list.

## Output Artifacts
New/updated tests under `tests/`.

## Implementation Notes
<details>
**Meaningful Test Strategy: "write a few tests, mostly integration."** Target custom business logic and critical paths; skip framework/third-party behavior. Combine related scenarios. For each surviving mutant, write the assertion that fails under the mutation. Re-run `mutmut run "custom_components.rtl_433.<module>.*"` to confirm kills. Keep tests deterministic (freezegun for time; suite already uses warnings-as-errors and asyncio auto mode).
</details>
