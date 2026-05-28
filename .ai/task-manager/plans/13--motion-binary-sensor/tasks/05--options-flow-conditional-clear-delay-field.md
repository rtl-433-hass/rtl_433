---
id: 5
group: "per-device-config"
dependencies: [1]
status: "completed"
created: "2026-05-28"
skills:
  - python
---
# Conditional clear-delay field in the options-flow device step

## Objective
Let users tune the motion clear-delay per device through the existing options-flow device step, mirroring the availability-timeout override — but shown **only** for motion-bearing devices.

## Skills Required
`python` — Home Assistant options flow (`config_flow.py`) + `selector`/voluptuous schema.

## Acceptance Criteria
- [ ] The device step (`config_flow.py` `async_step_device`) includes a clear-delay field, pre-filled from the persisted `DEVICE_MOTION_CLEAR_DELAY` override (or the default), presented like the availability timeout (positive int / `NumberSelector`).
- [ ] The field is shown **only** when the selected device has a field whose resolved descriptor carries a `clear_delay` (a motion field); non-motion devices never see it.
- [ ] On submit, the value is written to `entry.options` under the key `DEVICE_MOTION_CLEAR_DELAY` (the single shared key from Task 1 — no new `CONF_*` const); a blank submission clears the override (falls back to the descriptor default).
- [ ] The field's label/description exists in `translations/en.json` under the options-flow `step`/`data` section.
- [ ] `uvx ruff check .` and `uvx ruff format --check .` pass.

## Technical Requirements
- Device step schema is built around `config_flow.py:262-300+`; the availability timeout uses `vol.All(int, vol.Range(min=1))` / `NumberSelector` and `async_step_device` (≈`config_flow.py:334`) persists via `_write_device_record` / `_persist...`.
- Determine "motion-bearing" from the device's observed `DEVICE_FIELDS` looked up against the loaded registry (`mapping.lookup`) — include the field iff any resolved descriptor has a truthy `clear_delay`.
- Use `DEVICE_MOTION_CLEAR_DELAY` (Task 1) as the options field key; Task 3's persist block reads `child.options.get(DEVICE_MOTION_CLEAR_DELAY)`. `const.py` is owned by Task 1 — do not add a new const here.

## Input Dependencies
- Task 1: `clear_delay` attribute + constants.

## Output Artifacts
- An options-flow knob that writes the per-device clear-delay into `entry.options` (consumed by Task 3's setup-time persist into `entry.data`).

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. Use the existing `DEVICE_MOTION_CLEAR_DELAY` key (from Task 1) as the options-flow field key — do **not** add a new `CONF_*` const. (The availability timeout happens to use distinct `CONF_`/`DEVICE_` keys; for motion we deliberately use one key to keep options↔record persistence trivial. Task 3 reads the same key from `child.options`.)

2. In `async_step_device`, after the device is selected, compute whether it is motion-bearing:
   ```python
   fields = entry.data.get(CONF_DEVICES, {}).get(device_key, {}).get(DEVICE_FIELDS, [])
   model = ...  # the device's model from the record
   has_clear_delay = any(
       (d := registry_lookup(model, fk)) is not None and d.clear_delay
       for fk in fields
   )
   ```
   Use the same registry/`lookup` the flow already has access to (the loaded library). Only add the schema key when `has_clear_delay`.

3. Build the field like the availability timeout, defaulting from the persisted override:
   ```python
   clear_default = entry.data.get(CONF_DEVICES, {}).get(device_key, {}).get(
       DEVICE_MOTION_CLEAR_DELAY, DEFAULT_MOTION_CLEAR_DELAY
   )
   # vol.Optional(CONF_MOTION_CLEAR_DELAY, default=clear_default): vol.All(int, vol.Range(min=1))
   ```
   Make it Optional so a blank clears it (mirror how the timeout/override clear path works in `_write_device_record`).

4. Persist into `entry.options` on submit through the existing finish path (the same path that stores the timeout override + calibration). Do not write `entry.data` here — Task 3 copies options→data at setup.

5. Add the label to `translations/en.json` under the options flow `device` step `data` (and `data_description` if other fields have one). Reuse the wording style of the availability-timeout label, e.g. `"motion_clear_delay": "Motion clear delay (seconds)"`.

6. Run ruff. (Task 6 also edits `en.json` but in the `issues` section and in a later phase — no conflict.)
</details>
