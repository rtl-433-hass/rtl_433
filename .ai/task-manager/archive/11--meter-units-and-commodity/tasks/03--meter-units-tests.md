---
id: 3
group: "meter-units"
dependencies: [1, 2]
status: "completed"
created: 2026-05-28
skills:
  - python
  - pytest
---
# Tests for model-scoped lookup + per-device calibration

## Objective
Lock in Component A (model-scoped lookup + specificity-first precedence) and Component B (calibration flow, sensor wiring, reload behavior, commodity pre-fill).

## Skills Required
- `python`, `pytest` — `tests/test_mapping.py`, `tests/test_config_flow.py`, lifecycle harness.

## Acceptance Criteria
- [ ] **Model-scoped lookup** (`test_mapping.py`): `lookup(field, model, merged)` returns the model-scoped descriptor for a matching model and the global descriptor for other models; a non-meter field is unaffected.
- [ ] **Precedence (specificity-first)**: user-model > shipped-model; **shipped-model > user-global** for a matching model (the decisive case); user-global for non-matching model; per-device calibration overlay wins over all for the calibrated device.
- [ ] **Existing flat behavior unchanged**: an existing themed file still loads identically (regression).
- [ ] **Calibration flow** (`test_config_flow.py`): driving `async_step_device` with `{commodity: water, base unit: <convertible volume unit>, scale: 0.1}` writes the calibration into `entry.data[CONF_DEVICES][device_key]`.
- [ ] **Reload semantics**: a calibration change causes `_async_update_listener` to reload the hub entry (snapshot differs); an unrelated devices-map upsert does NOT reload.
- [ ] **Sensor wiring**: after (re)build, the consumption `Rtl433Sensor` reports `device_class == water`, the chosen native unit, `state_class == total_increasing`, and value == `raw × 0.1`.
- [ ] **Commodity pre-fill**: last event with `MeterType: "Gas"` (and separately an `ert_type` whose low nibble = gas) → form commodity default `gas`; no hint → `none`.
- [ ] `python -m pytest -q` passes; `uvx ruff check tests/` + `uvx ruff format --check tests/` clean.

## Technical Requirements
- Files: `tests/test_mapping.py`, `tests/test_config_flow.py` (+ lifecycle/sensor as needed). Use existing fixtures (`hub_entry_builder`, flow drivers, `_setup_hub`/`_feed` imported from `test_lifecycle` if needed).

## Input Dependencies
- Tasks 1 and 2.

## Output Artifacts
- New/updated tests.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- For precedence tests, build a base registry via `load_library()` then `merge_overrides` with synthetic shipped/user `models:` and user flat entries; assert each tier per the plan's Self Validation #1–#2. Use an illustrative (non-real) model string.
- For the reload test, spy on `hass.config_entries.async_reload` (or assert the coordinator is reconstructed) and confirm an unrelated `async_upsert_device` call does not trigger it.
- For sensor wiring, build the device's entities after seeding a calibration and assert the sensor attrs + transformed value; mirror existing sensor-build tests.
- For commodity pre-fill, seed `coordinator.devices[device_key]` with a last event carrying `MeterType`/`ert_type` and assert the rendered form default.

### Meaningful Test Strategy Guidelines
"write a few tests, mostly integration". Test the lookup precedence, the calibration round-trip + sensor result, the reload gating, and pre-fill — not HA's selector/flow framework itself. Combine related assertions.
</details>
