---
id: 6
group: "mutation-testing"
dependencies: [2, 4, 5]
status: "completed"
created: 2026-07-04
skills:
  - mutmut
  - pytest
complexity_score: 7
complexity_notes: "Running the full mutmut suite is slow, and driving each migrated module to its source-derived floor may require iterating the task 2/4 tests to kill survivors."
---
# Generate the mutation baseline and enforce per-module floors

## Objective
Run a full `mutmut` pass over the migrated `pyrtl_433` modules, seed the
per-module `mutation_baseline.json`, and confirm each migrated module's mutation
score meets or exceeds its source module's current floor — the binding proof that
the library carries "the same mutation testing coverage" as the original.

## Skills Required
- **mutmut**: running mutmut, reading results/surviving mutants, seeding and
  ratcheting the baseline.
- **pytest**: strengthening the migrated test files to kill any surviving mutants
  that keep a module below its floor.

## Acceptance Criteria
- [ ] `uv run mutmut run` completes over all `pyrtl_433/` modules.
- [ ] `../pyrtl_433/scripts/mutation_baseline.json` is seeded (via `mutation_stats.py` → `mutation_ratchet.py --mode floor --stats stats.json --update`) with a `files` map, `floor`, and tolerance fields mirroring the parent's format.
- [ ] `uv run python scripts/mutation_stats.py > stats.json && uv run python scripts/mutation_ratchet.py --mode floor --stats stats.json` exits 0.
- [ ] Each migrated module's score is **≥ its source module's current baseline** for the migrated logic: `normalizer.py` ≥ 0.935, `sdr.py` ≥ 0.971 (the `sdr_settings.py` figure), `replay.py` ≥ the `_events.py` classifier portion (~0.857 overall source), and `client.py` ≥ 0.788 (the `coordinator/base.py` figure). Any genuinely equivalent surviving mutant is documented, not suppressed.
- [ ] No source file was edited solely to kill a mutant; no `# pragma: no mutate` was added; the baseline only ratchets upward.

## Technical Requirements
- The mutmut kit (task 5), all migrated modules (task 2), the client (task 3),
  and all tests (task 2 + task 4) must be in place first.
- Follow the parent's hard rules (from `AGENTS.md`): never edit source to kill a
  mutant, never add `# pragma: no mutate` or disable a mutator, baseline ratchets
  upward only.
- Full runs are slow; use `mutmut run "pyrtl_433.<module>.*"` to iterate a single
  module while closing survivors.

## Input Dependencies
- Task 2 (pure modules + tests), task 4 (client + tests), task 5 (mutation kit).

## Output Artifacts
- `../pyrtl_433/scripts/mutation_baseline.json` (seeded), a passing ratchet run,
  and any test strengthening committed into the task 2/4 test files. Consumed by
  task 7 (validation/commit).

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

The local loop (mirrors the parent's documented workflow):
```
cd ../pyrtl_433
uv run mutmut run                                   # full run -> mutants/
uv run mutmut results                               # list survivors
uv run mutmut show <mutant_name>                    # inspect a survivor's diff
uv run python scripts/mutation_stats.py > stats.json
uv run python scripts/mutation_ratchet.py --mode floor --stats stats.json --update   # seed baseline
```

To lift a module that is below its source floor, read each surviving mutant with
`mutmut show`, then add a `test_mut_<module>[_floor].py` case (in the task 2 or
task 4 files) that asserts the exact value / both branches the mutant breaks.
Re-run just that module: `uv run mutmut run "pyrtl_433.<module>.*"`. Repeat until
the module meets its floor. Then finalize the baseline with `--update`.

**Test philosophy (mandatory restatement).** Meaningful tests verify custom
business logic, critical paths, and edge cases — test *your* code, not the
framework. Prefer integration/critical-path coverage; combine related scenarios.
Do not test framework/library features or trivial code. **Exception governing this
plan:** the work order requires the *same mutation testing coverage*, so killing
survivors with exact-value/both-branch assertions is the explicit acceptance bar
here — that is a direct requirement, not gold-plating. Where a survivor is a
genuinely equivalent mutant (no behavioral difference), document it (as the parent
does for its equivalent mutants) rather than contorting a test or the source.

Acceptance: the floor-mode ratchet exits 0 and every migrated module meets or
exceeds the target derived from the source baseline (`scripts/mutation_baseline.json`
in the `rtl_433` checkout: `normalizer.py` 0.935, `sdr_settings.py` 0.971,
`coordinator/_events.py` 0.857, `coordinator/base.py` 0.788).
</details>
