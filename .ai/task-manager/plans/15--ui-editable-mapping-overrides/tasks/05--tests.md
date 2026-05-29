---
id: 5
group: "verification"
dependencies: [2, 3, 4]
status: "pending"
created: 2026-05-28
skills:
  - python
  - pytest
---
# Tests for per-entry merge, validator, migration, and options step

## Objective
Add meaningful tests (a few, mostly integration) covering the behaviours unique to this change: per-hub override isolation, the validator's accept/reject contract, the migration's JSON-safe payload round-trip + seed-every-hub + file-retirement, and the options step storing into `entry.data` without clobbering other config.

## Skills Required
- `python`, `pytest`: existing HA custom-component test fixtures in `tests/`.

## Acceptance Criteria
- [ ] Validator: a valid object returns `[]`; a missing-`platform` entry, a bad `skip_keys` type, and an unknown platform each return a problem naming the field.
- [ ] Normalizer round-trip: an override whose `payload` uses bare `on`/`off` (parsed by PyYAML to `True`/`False`) becomes string `"on"`/`"off"` keys after `normalize_overrides`, and the merged descriptor's binary transform resolves correctly.
- [ ] Per-entry isolation: two hub entries with different `CONF_USER_MAPPINGS` produce different merged registries; an override on hub A does not change hub B's lookups.
- [ ] Migration: with a temp `rtl_433_mappings.yaml` present, `async_migrate_entry` seeds `entry.data[CONF_USER_MAPPINGS]` (normalized) for each existing entry, leaves the file on disk, and is a no-op on second run; missing file → `{}`.
- [ ] Options step: submitting a schema-invalid object re-shows the form with the error and stores nothing; submitting a valid object writes `entry.data[CONF_USER_MAPPINGS]` and leaves `entry.options`/`CONF_DEVICES` intact.
- [ ] `uv run pytest tests/` passes; coverage does not regress meaningfully.

## Technical Requirements
- Follow `tests/conftest.py` fixtures and the style of `tests/test_mapping.py`, `tests/test_config_flow.py`, `tests/test_lifecycle.py`.
- "Write a few tests, mostly integration" — do not test PyYAML/HA framework internals; test this integration's logic and wiring.

## Input Dependencies
- Tasks 2, 3, 4 (the behaviours under test).

## Output Artifacts
- Test coverage that gates POST_EXECUTION and CI.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

Read `tests/conftest.py`, `tests/test_mapping.py`, `tests/test_config_flow.py`, `tests/test_lifecycle.py` to reuse fixtures (mock config entry, hass, the library helpers).

- Put pure-function tests (validator, normalizer) in `tests/test_mapping.py` (extend it). Use small inline dicts; for the PyYAML round-trip, build the input by `yaml.safe_load("battery_ok:\n  platform: binary_sensor\n  name: B\n  object_suffix: B\n  payload:\n    on: '0'\n    off: '1'")` so you genuinely get `True/False` keys, then assert `normalize_overrides` canonicalises them, then feed through `merge_overrides` + `apply_transform`.
- Per-entry isolation + migration + options-step tests likely belong in `tests/test_lifecycle.py` / `tests/test_config_flow.py` (integration-style with the HA test harness). For migration, monkeypatch `hass.config.path` / write a temp file under the HA config dir fixture, set the entry's `minor_version` to 1, call `async_migrate_entry`, assert results; call again to assert idempotency.
- For the options step, drive the flow via the HA options-flow test helpers used in `test_config_flow.py`; assert `errors` on invalid input and `entry.data[CONF_USER_MAPPINGS]` on valid input, plus that `entry.options` and `entry.data[CONF_DEVICES]` are unchanged.
- Run `uv run pytest tests/ -q`. Fix any failures you introduced. If a pre-existing test breaks due to the consumer rewiring (Task 2), update it to read the per-entry library location.
</details>
