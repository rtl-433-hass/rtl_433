---
id: 5
group: "testing"
dependencies: [4]
status: "pending"
created: 2026-07-04
skills:
  - pytest
  - mutation-testing
complexity_score: 7
complexity_notes: "Dozens of test files import now-deleted transport internals; the mutmut ratchet targets extracted source. Must separate behavioral tests (keep) from transport-internal tests (retire) and rescope source_paths without leaving coverage holes in the new local adapters."
---
# Realign the pytest suite and rescope the mutmut ratchet

## Objective
Keep user-facing behavior verified while removing tests that only asserted the now-deleted
transport internals, and rescope the `mutmut` mutation-testing ratchet to code that still lives
in the integration. Cover the new local adapters (`_safe_token`, SDR reconciliation).

## Skills Required
- **pytest**: `pytest-homeassistant-custom-component` fixtures, coordinator/config-flow tests.
- **mutation-testing**: `mutmut` `source_paths` and the repo's ratchet/floor kit.

## Acceptance Criteria
- [ ] The full pytest suite passes under Python 3.14 (`uv` env), 0 failures, with `filterwarnings=["error"]` respected.
- [ ] Tests importing deleted internals (`.base._SdrStore` where removed, `_send_cmd`/`_fetch_cmd`/`_build_ws_url`/`_unwrap_result` local defs, local `CannotConnect`, local `classify_replay`/`normalize`/SDR registry) are rewritten to the new seam or retired if they only asserted removed transport internals.
- [ ] Behavioral coverage stays intact: config flow, repairs, diagnostics, entity generation, availability transitions, SDR controls, and replay/normalization *as observed through the coordinator*.
- [ ] `mutmut` `source_paths` in `pyproject.toml` no longer references the deleted transport/helper files; the extracted-code shard tests (`test_mut_coordinator_base*`, `test_mut_sdr_settings_floor`, `test_mut_normalizer`-portions targeting moved code, etc.) are retired or rescoped.
- [ ] New local logic — `_safe_token` and the SDR adapter — has direct coverage.
- [ ] The mutmut ratchet check the project uses passes with the rescoped configuration.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Heaviest consumers (from the map): `test_mut_coordinator_base.py`, `test_mut_coordinator_base_floor.py`,
  `test_mut_sdr_settings_floor.py`, `test_sdr_controls.py`, `test_diagnostics_repairs.py`,
  `test_config_flow.py`, `test_mut_switch.py`, `test_mut_number.py`, `test_mut_repairs_floor.py`,
  `test_normalizer.py`, `test_event_trace.py`, plus the many `Rtl433Coordinator(...)`
  instantiation sites.
- `mutmut` config lives in `pyproject.toml` `[tool.mutmut]` (`source_paths=["custom_components/rtl_433/"]`).
- The extracted transport/helpers now carry their own mutation ratchet inside `pyrtl_433`; do
  not attempt to mutation-test dependency code from this repo.

## Test philosophy (apply verbatim)
Write a few tests, mostly integration. Meaningful tests verify custom business logic, critical
paths, and edge cases specific to this application — test *your* code, not the framework or a
dependency. **Write tests for**: custom business logic, critical user workflows, data
transformations, edge/error conditions for core functionality, integration points, complex
validation. **Do not write tests for**: third-party library functionality, framework features,
simple CRUD without custom logic, trivial getters/setters or static config, obvious code that
would break immediately if wrong. Combine related scenarios into single tasks; favor
integration/critical-path coverage over per-method unit tests; don't add one test per CRUD op;
question whether simple functions need a dedicated test.

Concretely here: do **not** re-test `pyrtl_433`'s normalize/classify/transport (covered
upstream). **Do** test the integration's *adaptation* — that events flowing through the client
callback produce the right dispatcher signals and entity states, that the SDR adapter yields the
right entities/`/cmd` args, and that `_safe_token` slugs are stable.

## Input Dependencies
- Task 4: the re-architected coordinator (defines the new seam tests target).

## Output Artifacts
- A green, behavior-focused test suite and a rescoped, passing mutmut ratchet.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

1. **Triage** every failing/broken test into: (a) behavioral — keep, adapt imports/seam to the
   new client-owning coordinator; (b) transport-internal — asserted `_build_ws_url`,
   `_unwrap_result`, raw frame parsing, the connect loop, etc.; retire these (that logic is now
   `pyrtl_433`'s and is tested there).
2. For behavioral tests that construct `Rtl433Coordinator(...)` and previously injected fake WS
   frames, switch to driving the coordinator through the client's `on_event` callback (invoke
   the callback with a `pyrtl_433` `NormalizedEvent`) or a fake/mock `Rtl433Client`, then assert
   dispatcher signals / entity state. Preserve the user-facing assertions.
3. **CannotConnect / validate_connection** tests: point at the library's `CannotConnect` and the
   delegated `validate_connection`; keep the config-flow/repairs behavior assertions.
4. **mutmut**: edit `[tool.mutmut] source_paths` so it covers only code still in the repo (the
   coordinator adapter, entity/platform code, config flow, repairs, the local adapters). Remove
   references to deleted files. Retire the shard test files that exist solely to hit mutants in
   the extracted transport/helpers. If the repo has per-module mutation floors, drop the floors
   for removed modules and add modest floors for the new adapter logic.
5. Run the full suite; then run the project's mutmut ratchet command. Both must pass. Capture the
   pytest summary line and the mutmut result.
6. Keep new tests minimal and integration-flavored per the philosophy above — cover the adapter
   seam and `_safe_token`, not re-covered library internals.
</details>
