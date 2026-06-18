---
id: 2
group: "meter-units"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - python
  - home-assistant
complexity_score: 6
complexity_notes: "Spans options flow, sensor wiring/precedence overlay, update-listener reload + coordinator snapshot, and translations; kept together because the calibration record shape, sensor overlay, and reload trigger must agree."
---
# Component B — per-device calibration (commodity + base unit + scale)

## Objective
Add a per-device calibration to the options-flow device step so a user can turn a unitless consumption sensor into an Energy-dashboard-eligible sensor (`device_class` from commodity, convertible base unit, `state_class: total_increasing`, scale). Applies only to the known consumption field keys (`consumption` / `consumption_data`). Pre-fill commodity from decoded `MeterType`/`ert_type`. Apply via reload detected in `_async_update_listener`.

## Skills Required
- `python`, `home-assistant` — options flow (`SelectSelector`), per-device `entry.data[CONF_DEVICES]`, sensor descriptor precedence, update-listener reload pattern, coordinator snapshot.

## Acceptance Criteria
- [ ] `async_step_device` (`config_flow.py`) additionally collects **commodity** (none/energy/gas/water), **base unit** (constrained to HA-convertible units for the commodity's device_class), and **scale** (number), writing them into the chosen device's `entry.data[CONF_DEVICES][device_key]` record alongside `timeout_override`. Reuses the existing device select / `no_devices` abort / `async_update_entry` mechanics.
- [ ] **Commodity pre-fill**: reads the device's last event from the running coordinator (`coordinator.devices[device_key].fields`); maps `MeterType` (`Electric`→energy, `Gas`→gas, `Water`→water, else none) or the `ert_type` low nibble to the default; best-effort (never raises), defaults to none.
- [ ] **Sensor wiring (precedence #1)**: for a calibrated device's consumption field (`consumption`/`consumption_data`), the `Rtl433Sensor` takes `device_class` (from commodity), native unit (base unit), `state_class: total_increasing`, and scale from the calibration — overriding the library/global descriptor. Scale reuses the existing `value_transform` scale path. `commodity = none` clears the calibration (falls back to library descriptor).
- [ ] **Apply via reload in the listener (one path)**: `_async_update_listener` (`__init__.py`) detects a calibration change vs a coordinator-held **calibration snapshot captured at setup** and `await async_reload`s. The flow step does NOT call `async_schedule_reload`. Routine idempotent devices-map upserts (`async_upsert_device`/`async_upsert_event_types`) must NOT trigger a reload (snapshot unchanged).
- [ ] A named constant set holds the calibratable consumption field keys (`consumption`, `consumption_data`).
- [ ] `translations/en.json` gains `options.step.device` labels/descriptions for commodity/base unit/scale (+ any select option labels). Valid JSON.
- [ ] `ruff check`/`ruff format --check` clean; existing tests pass.

## Technical Requirements
- Files: `custom_components/rtl_433/config_flow.py`, `custom_components/rtl_433/entity.py` and/or `sensor.py`, `custom_components/rtl_433/__init__.py`, `custom_components/rtl_433/coordinator/base.py`, `custom_components/rtl_433/const.py`, `custom_components/rtl_433/translations/en.json`.
- Build on task 1's model-aware `lookup` (overlay calibration onto the looked-up base descriptor).

## Input Dependencies
- Task 1 (model-aware lookup + registry shape).

## Output Artifacts
- The calibration flow, storage record shape, sensor overlay, and reload trigger — consumed by tests (task 3) and docs (task 4).

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Read `config_flow.py` `async_step_device` (~:191-241): device select, `no_devices` abort, `async_update_entry(entry, data=...)`. Read `__init__.py` `_async_update_listener` (~:232-264) and the `manage_settings` reload (~:253-257). Read `coordinator/base.py` for `self.devices` (~:201) and where `manage_settings` snapshot lives. Read `sensor.py` `Rtl433Sensor.__init__` (~:45-71) and `entity.py` `_descriptor_for`/`_build` (~:425-449). Read `const.py` per-device keys (~:67-82). Confirm line numbers.
- **Commodity → device_class / base units**: energy → `SensorDeviceClass.ENERGY` (units e.g. Wh/kWh/MWh); gas → `GAS` (volume m³/ft³/CCF); water → `WATER` (volume L/gal/m³/ft³/CCF). Use HA's recognized convertible unit enums so the Energy dashboard + display-unit conversion work. Constrain the base-unit selector to the chosen commodity's valid units.
- **Calibration record**: store under the device record, e.g. `calibration: {commodity, unit, scale}`. Define a constant key. `commodity == none` (or absence) ⇒ no calibration.
- **Overlay**: cleanest is to compute an effective descriptor for the consumption field at construction: take the base descriptor from `lookup(field_key, model, registry)` and, when a calibration exists for the device AND `field_key` is in the consumption constant set, override `device_class`/`unit_of_measurement`/`state_class` and merge `value_transform.scale`. Note base `consumption`/`consumption_data` descriptors carry `value_transform: {int: true}`; adding `scale` makes it float — correct for energy/volume. Do this in `_descriptor_for`/`_build` (entity.py) or the sensor constructor — keep the precedence chain: calibration > model-scoped > global.
- **Reload trigger**: in `_async_update_listener`, compute the current per-device calibration map and compare to `coordinator.<snapshot>` (capture it at setup like `coordinator.manage_settings`); if changed, `await hass.config_entries.async_reload(entry.entry_id)` and return. Do NOT add `async_schedule_reload` in the flow. Ensure routine upserts don't change the snapshot-relevant data (only the calibration sub-record matters).
- **Commodity pre-fill**: in the form-render path, `coordinator = hass.data[DOMAIN][entry.entry_id]`; `ev = coordinator.devices.get(device_key)`; read `ev.fields.get("MeterType")` / `ev.fields.get("ert_type")`. Guard everything.
- Verify: `uvx ruff check .`; `uvx ruff format --check .`; `python -m pytest tests/test_config_flow.py tests/test_lifecycle.py -q` (don't break existing).
</details>
