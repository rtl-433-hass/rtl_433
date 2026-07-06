---
id: 5
group: "core-scaffold"
dependencies: [4]
status: "completed"
created: 2026-07-06
skills:
  - pytest
  - home-assistant
---
# Add Core Tests and Validate the Minimal Integration

## Objective
Add tests under `tests/components/rtl_433/` in the core fork using `syrupy` snapshots per core conventions, and validate the scaffolded integration passes core's local checks (hassfest / manifest / quality-scale validators and the integration test suite).

## Skills Required
- **pytest**: core's test harness, fixtures, `syrupy` snapshot testing.
- **home-assistant**: core testing conventions, hassfest, quality-scale validation.

## Acceptance Criteria
- [ ] `tests/components/rtl_433/` exists with at least: `__init__.py`, `conftest.py` (fixtures/mocked pyrtl_433 client), a config-flow test, and a sensor/setup test using `syrupy` snapshots.
- [ ] `python -m pytest tests/components/rtl_433 -q` passes in the fork.
- [ ] hassfest (`python -m script.hassfest`) passes for the `rtl_433` integration (manifest, quality scale, requirements consistent).
- [ ] `grep -R "custom_components" homeassistant/components/rtl_433` returns nothing (imports fully relative).
- [ ] Snapshot files are generated and committed to the working tree.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Mock the `pyrtl_433` async client so tests do not require a live rtl_433 host; simulate a normalized event to drive the sensor platform.
- Test philosophy: "write a few tests, mostly integration." Cover the config-flow happy path + one failure (cannot-connect), and a setup-to-sensor-state integration path with a snapshot. Do not write per-method unit tests for framework plumbing or trivial getters. Combine related scenarios; favor critical-path integration coverage.

## Input Dependencies
- Task 4: the scaffolded `homeassistant/components/rtl_433/` package.

## Output Artifacts
- `tests/components/rtl_433/` test suite + syrupy snapshots; a green local validation run recorded in the task output.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

- Model the test module structure on a recent, similarly-shaped core integration (a local_push hub with a coordinator) so fixtures and snapshot usage match current conventions.
- `conftest.py`: provide a `mock_pyrtl_433` fixture patching the client used by the coordinator/config flow, plus a `MockConfigEntry` factory at the contract's `version`/`minor_version`.
- Config-flow tests: assert the user step creates an entry on success and shows `cannot_connect` (or equivalent) on a simulated connection failure; assert single-instance/unique-id handling if present.
- Setup/sensor test: set up the entry with a mocked event stream, then snapshot the created entities/states with `syrupy` (`assert state == snapshot`). Generate snapshots with `pytest --snapshot-update` then re-run without the flag to confirm stability.
- Run `python -m script.hassfest` and fix any manifest/quality-scale/requirements findings for `rtl_433` (this is where an `aiohttp` pin conflict or a missing quality-scale rule would surface).
- Confirm zero `custom_components` references remain.
- Report the exact commands run and their pass/fail output in the task result. Do not open a PR; leave the tree ready for human review/submission.
</details>
