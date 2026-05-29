---
id: 3
group: "test-writing"
dependencies: [1]
status: "pending"
created: 2026-05-28
skills:
  - python
  - unit-testing
---
# Tests to Raise Platform/Flow Modules to ≥70% Mutation Score

## Objective
Write/strengthen pytest tests so the platform and flow modules below the floor reach ≥70% (stretch where cheap): config_flow, __init__ (setup/migration), sensor, binary_sensor, number, select, switch, event, device_trigger, diagnostics, repairs, sdr_settings.

## Skills Required
- python, unit-testing (HA custom-component test harness)

## Acceptance Criteria
- [ ] Each targeted module ≥70% mutation score (stretch higher where cheap)
- [ ] New tests assert real behavior (entity attrs/availability, flow steps, redaction, migration)
- [ ] No `# pragma: no mutate`, no disabled mutators, no skipped/xfail/weakened tests
- [ ] Full suite still green

## Technical Requirements
Use `mutmut results` + `mutmut show <mutant>`; add deterministic tests under `tests/`.

## Input Dependencies
Task 1 baseline per-file scores and survivor list.

## Output Artifacts
New/updated tests under `tests/`.

## Implementation Notes
<details>
**Meaningful Test Strategy: "write a few tests, mostly integration."** Prefer integration-style tests that drive the platform/flow via the HA harness (MockConfigEntry, hub_entry_builder, event fixtures). Combine related scenarios. Re-run per-module mutmut to confirm kills. Deterministic only.
</details>
