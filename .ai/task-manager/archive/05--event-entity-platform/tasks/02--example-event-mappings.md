---
id: 2
group: "device-library"
dependencies: []
status: "completed"
created: "2026-05-26"
skills:
  - yaml
  - home-assistant
---
# Three shipped `platform: event` example mappings

## Objective
Add a new themed device-library file
`custom_components/rtl_433/device_library/events.yaml` containing **three**
`platform: event` mappings, one per `EventDeviceClass`:

1. a **button/remote** field — `device_class: button` (multi-value: the value is
   a button code/name, so distinct presses auto-populate several `event_types`);
2. a **motion/PIR** field — `device_class: motion` (single-value momentary case);
3. a **doorbell**-press field — `device_class: doorbell` (single-value).

Each must be a **real** rtl_433 field that is currently **neither mapped** in any
`device_library/*.yaml` **nor listed** in `_skip_keys.yaml`, so no existing
behavior changes. The loader globs `*.yaml` and skips only underscore-prefixed
files, so the new file is picked up automatically — **no loader code change is
required** (the loader is permissive about the `platform` string and tolerates
the `device_class` value).

## Skills Required
- `yaml` — authoring a themed device-library file in the established schema.
- `home-assistant` — knowing valid `EventDeviceClass` members
  (`button`, `motion`, `doorbell`) and the `event` platform's value-as-type model.

## Acceptance Criteria
- [ ] New file `custom_components/rtl_433/device_library/events.yaml` exists with a header comment explaining it holds `platform: event` (momentary, fire-and-forget) mappings.
- [ ] It contains exactly three top-level field entries, each with `platform: event`, a `device_class` that is a valid `EventDeviceClass` member (`button`, `motion`, `doorbell` respectively), a `name`, and a unique `object_suffix`.
- [ ] The entries declare **no** `event_types` (auto-populated), **no** `payload`, and **no** `value_transform` (the value is stringified directly).
- [ ] None of the three chosen field keys appears in any other `device_library/*.yaml` mapping table, and none appears in `device_library/_skip_keys.yaml`. (Verify against the lists in the notes below.)
- [ ] The three field keys are real rtl_433 output fields (verified against rtl_433's documented field set / decoder output, not invented).
- [ ] `uv run python -c "from custom_components.rtl_433.mapping import load_library, lookup; r,_=load_library(); [print(k, lookup(k,r).platform, lookup(k,r).device_class) for k in (<the three keys>)]"` prints `event` and the expected device_class for each.
- [ ] `uv run ruff check custom_components/rtl_433` stays clean (YAML-only change; nothing to lint, but confirm nothing else regressed).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `custom_components/rtl_433/device_library/events.yaml`.
- Schema reference: `docs/device-library.md` and the existing themed files
  (e.g. `binary_states.yaml`) for entry shape. Required attributes are
  `platform`, `name`, `object_suffix`; optional here is `device_class` (the
  `EventDeviceClass`), plus `entity_category`/`enabled_by_default`/`icon` if
  wanted.
- `object_suffix` must not collide with existing suffixes (e.g. avoid `B`,
  `last_seen`, `opening`, `T`, etc.).

## Input Dependencies
None — independent of the platform code (Task 1). The loader already resolves
`platform: event` descriptors.

## Output Artifacts
- `device_library/events.yaml` with three event mappings, consumed by Task 3's
  schema assertions and documented by Task 4.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Choosing the three real, unmapped, non-skipped fields
**Already-mapped field keys (do NOT reuse any of these):**
`alarm, battery_mV, battery_ok, closed, co2_ppm, consumption, consumption_data,
contact_open, current_A, depth_cm, detect_wet, energy_kWh,
estimated_pm10_0_ug_m3, gust_speed_km_h, gust_speed_m_s, humidity, humidity_1,
humidity_2, light_lux, lux, moisture, noise, pm10_ug_m3, pm2_5_ug_m3, power_W,
pressure_hPa, pressure_kPa, rain_in, rain_mm, rain_rate_in_h, rain_rate_mm_h,
reed_open, rssi, snr, storm_dist, storm_dist_km, strike_count, strike_distance,
supercap_V, tamper, temperature_1_C..4_C, temperature_C, temperature_F, time,
uv, uvi, voltage_V, wind_* (all variants), wind_dir_deg`.

**Skip-keys (do NOT reuse):** `type, model, subtype, channel, id, mic, mod,
freq, freq1, freq2, sequence_num, message_type, exception, raw_msg, protocol`.

**Suggested candidates (verify each is a real rtl_433 field before committing):**
- **button** → `device_class: button`. `button` is a real rtl_433 field emitted
  by many remotes / key fobs (and is present in the local sample capture
  `tests/integration/rtl_433_tests/tests/acurite/Acurite_606TX/...` as
  `"button": 0`). It is **not** mapped and **not** skipped. Its value is the
  pressed-button code, so multiple distinct values auto-populate `event_types`.
- **motion** → `device_class: motion`. `motion` is a real rtl_433 field emitted
  by PIR/occupancy decoders. Not mapped, not skipped. Typically a single value,
  demonstrating the momentary single-type case.
- **doorbell** → `device_class: doorbell`. Pick a real doorbell-press field that
  is unmapped and unskipped. If no field is literally named `doorbell`, choose a
  real momentary field emitted by a doorbell/chime decoder (verify the field
  name against rtl_433 output) and map it with `device_class: doorbell`.

If you cannot confirm a field name from the local repo, use WebSearch/WebFetch
against the upstream rtl_433 documentation or decoder source to confirm the exact
JSON key a decoder emits. The key correctness bar: the field is a **real**
rtl_433 output key, **absent** from the mapped + skip lists above.

### Example file shape
```yaml
# Event field mappings (momentary, fire-and-forget RF transmissions).
#
# These use `platform: event`: each transmission fires an HA Event whose
# event_type is the stringified field value. event_types are auto-populated from
# observed values (not declared here). device_class is an EventDeviceClass.
# See docs/device-library.md for the schema reference.

button:
  platform: event
  device_class: button
  name: Button
  object_suffix: button

motion:
  platform: event
  device_class: motion
  name: Motion
  object_suffix: motion

doorbell:           # replace with the verified real doorbell field key
  platform: event
  device_class: doorbell
  name: Doorbell
  object_suffix: doorbell
```

### Verify before finishing
Run the one-liner in the acceptance criteria to confirm all three resolve with
`platform == "event"` and the right `device_class`. Also grep to be sure none of
the three keys already appears in another library file or in `_skip_keys.yaml`.
</details>
