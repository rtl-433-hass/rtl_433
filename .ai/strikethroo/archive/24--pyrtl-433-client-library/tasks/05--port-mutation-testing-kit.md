---
id: 5
group: "mutation-testing"
dependencies: [1]
status: "completed"
created: 2026-07-04
skills:
  - python
  - mutmut
---
# Port the mutation-testing kit (scripts and config)

## Objective
Re-home the parent project's mutmut ratchet/shard/targets/stats/timings tooling
into `pyrtl_433/`, re-parameterized to the new package, so the library can
enforce the same "mutation coverage never regresses" gate. This task ports the
tooling only; task 6 generates the baseline and runs the gate.

## Skills Required
- **python**: porting the stdlib scripts and adjusting the hard-coded package
  constants.
- **mutmut**: understanding the mutmut 3.6.0 config and the scripts' use of
  mutmut's internal API.

## Acceptance Criteria
- [ ] `scripts/mutation_ratchet.py`, `scripts/mutation_shards.py`, `scripts/mutation_targets.py`, `scripts/mutation_stats.py`, `scripts/mutation_timings.py` exist under `../pyrtl_433/scripts/`.
- [ ] Every hard-coded `PKG = "custom_components/rtl_433"` / `PKG_DOTTED = "custom_components.rtl_433"` constant (in `mutation_targets.py`, `mutation_shards.py`, `mutation_stats.py`) is updated to `pyrtl_433` / `pyrtl_433`.
- [ ] `EXPLICIT_TEST_SOURCES` / `FULL_RUN_TRIGGERS` in `mutation_targets.py` are updated to the library's actual test→module map (e.g. `tests/test_client.py`, `test_mut_client*.py` → `client.py`; drop entries for modules that no longer exist).
- [ ] The `[tool.mutmut]` block in `pyproject.toml` is correct for a **regular** package (`source_paths = ["pyrtl_433/"]`, `pytest_add_cli_args_test_selection = ["tests/"]`, **no** namespace `also_copy`).
- [ ] The meta-tests `tests/test_mutation_shards.py` and `tests/test_mutation_targets.py` are ported and self-skip inside the `mutants/` sandbox (guard on the script file being absent), and pass under `uv run pytest -q`.
- [ ] `uv run python scripts/mutation_targets.py pyrtl_433/client.py` produces sane output (module maps to itself).

## Technical Requirements
- Sources (read-only): `scripts/mutation_ratchet.py`, `scripts/mutation_shards.py`,
  `scripts/mutation_targets.py`, `scripts/mutation_stats.py`,
  `scripts/mutation_timings.py`, and `tests/test_mutation_shards.py`,
  `tests/test_mutation_targets.py` in the `rtl_433` checkout.
- The scripts are stdlib-only except the three that import from
  `mutmut.__main__` (`walk_mutatable_files`, `SourceFileMutationData`,
  `status_by_exit_code`) — keep `mutmut==3.6.0` pinned so that internal API
  matches.
- No CI workflow is required (the work order does not ask for CI); the local
  command loop is sufficient. Do not add a GitHub Actions workflow unless trivial.

## Input Dependencies
- Task 1: the scaffold, `scripts/` dir, `pyproject.toml` `[tool.mutmut]` block.

## Output Artifacts
- The five `scripts/mutation_*.py` files (re-parameterized) and the two ported
  meta-tests. Consumed by task 6.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

Copy the five scripts and change the package constants. In `mutation_targets.py`
(lines ~46-47 in the source), `mutation_shards.py` (~51-52), and
`mutation_stats.py`, set `PKG = "pyrtl_433"` and `PKG_DOTTED = "pyrtl_433"`.

Rewrite `EXPLICIT_TEST_SOURCES` for the library's layout. The naming convention
`test[_mut]_<name>.py -> <name>.py` already auto-resolves
`test_normalizer.py -> normalizer.py`, `test_client.py -> client.py`,
`test_replay.py -> replay.py`, `test_sdr.py -> sdr.py`, `test_urls.py ->
_urls.py`? Note `_urls.py` has a leading underscore — either name the test
`test__urls.py` or add an explicit `EXPLICIT_TEST_SOURCES` entry mapping
`tests/test_urls.py -> ["_urls.py"]`. Add explicit entries for the `_floor`
tests (they don't auto-resolve): `tests/test_mut_client_floor.py ->
["client.py"]`, etc. Keep `FULL_RUN_TRIGGERS` pointing at this repo's
`pyproject.toml`, `requirements_test.txt`, `tests/conftest.py`, and the
`scripts/mutation_*.py` files.

Port the two meta-tests verbatim, updating the `_SCRIPT` path they guard on so
they still self-skip when `scripts/` is not copied into `mutants/`.

Confirm `[tool.mutmut]` in `pyproject.toml` (from task 1) has no `also_copy`
namespace line — `pyrtl_433` is a regular package.

Smoke-test: `uv run python scripts/mutation_targets.py pyrtl_433/normalizer.py`
should print `scoped` and map to `normalizer.py`. Do not run a full `mutmut run`
here — that is task 6.
</details>
