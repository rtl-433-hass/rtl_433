---
id: 4
group: "mapping-data"
dependencies: []
status: "completed"
created: 2026-05-25
skills:
  - yaml
  - python
---
# Device Mapping YAML Library (themed files + contributor schema)

## Objective
Create the data-driven device-mapping library as multiple themed YAML files seeded by porting the `rtl_433_mqtt_hass.py` `mappings` table and `SKIP_KEYS`, plus a documented YAML schema and contributor guide. This is pure data + docs — no integration code — so it is file-disjoint from Task 3 and runnable in parallel.

## Skills Required
- `yaml` — schema design, data authoring
- `python` — understanding the source `mappings` dict semantics to port faithfully

## Acceptance Criteria
- [ ] A `custom_components/rtl_433/device_library/` directory contains themed YAML files: at least `temperature.yaml`, `humidity_moisture.yaml`, `pressure.yaml`, `wind.yaml`, `rain.yaml`, `power_electrical.yaml`, `air_quality.yaml`, `light_uv.yaml`, `binary_states.yaml`, and `misc.yaml`.
- [ ] A `_skip_keys.yaml` (or equivalent) lists the `SKIP_KEYS` (`type`, `model`, `subtype`, `channel`, `id`, `mic`, `mod`, `freq`, `sequence_num`, `time`, etc.) so the loader excludes them.
- [ ] Every field entry from the upstream `mappings` table is ported, keyed by rtl_433 field name (e.g. `temperature_C`, `temperature_1_C`..`temperature_4_C`, `temperature_F`, `humidity`, `moisture`, `pressure_hPa`/`pressure_kPa`, `wind_avg_km_h`, `wind_max_m_s`, `wind_dir_deg`, gusts, `rain_mm`, `rain_rate_mm_h`, `power_W`, `energy_kWh`, `current_A`, `voltage_V`, `battery_mV`, `pm2_5_ug_m3`, `co2_ppm`, `lux`, `uv`, `uvi`, and binary states `battery_ok`, `tamper`, `reed_open`, `contact_open`, `alarm`, `closed`, `detect_wet`).
- [ ] Each entry carries: `platform` (`sensor`|`binary_sensor`), `device_class` (nullable), `unit_of_measurement` (nullable), `state_class` (nullable), `name` (display), optional `value_transform`/`rounding`, optional `payload` mapping for binary on/off values, and `object_suffix` (unique-id suffix).
- [ ] A documented schema is captured in `docs/device-library.md` (the contributor guide): one entry per field key with each attribute explained, where themed files live, how to add/correct a mapping, how to read diagnostics' unmatched-keys to find gaps, and how the user-override file works.
- [ ] Every YAML file is valid (`python3 -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('custom_components/rtl_433/device_library/*.yaml')]"`).
- [ ] A single conventional commit (e.g. `feat: add data-driven device mapping library`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Faithfully port the semantics (device_class, unit, state_class, value_template intent, object_suffix) from the upstream `mappings` table. Translate Jinja `value_template` logic into a declarative `value_transform` where possible (e.g. round, scale, lookup); record any template that cannot be expressed declaratively as a note for the loader (Task 5) to handle, but prefer simple declarative transforms.
- The schema must be consumable by a thin Python loader (Task 5) — keep it simple and regular. Agree the field names with what `const.py` (Task 3) and the loader expect; since Task 3 runs in the same phase, define the schema here authoritatively and document it; the loader will conform to it.
- Do NOT put executable Python in the library files. Data only.

## Input Dependencies
None (Phase 1). Writes only under `custom_components/rtl_433/device_library/` and `docs/` — disjoint from Task 3.

## Output Artifacts
- The themed YAML library + `_skip_keys.yaml` consumed by the loader (Task 5) and tests (Task 10); `docs/device-library.md` consumed by Task 12 docs.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. **Fetch the seed source** with `WebFetch`:
   `https://raw.githubusercontent.com/merbanan/rtl_433/master/examples/rtl_433_mqtt_hass.py`
   (If `master` 404s, try `main`.) Read its `mappings` dict and `SKIP_KEYS` set. Each `mappings[key]` has `device_type` (sensor/binary_sensor), `object_suffix`, and `config` (`device_class`, `name`, `unit_of_measurement`, `value_template`, `state_class`, sometimes `payload_on`/`payload_off`).
2. **Map source → YAML schema** per field:
   ```yaml
   temperature_C:
     platform: sensor
     device_class: temperature
     unit_of_measurement: "°C"
     state_class: measurement
     name: Temperature
     value_transform: { round: 1 }   # from value_template intent
     object_suffix: T
   battery_ok:
     platform: binary_sensor
     device_class: battery
     name: Battery
     payload: { on: 0, off: 1 }       # battery_ok==0 means low battery -> problem
     object_suffix: B
   ```
   Group entries into the themed files by domain. Keep `name` human-readable.
3. **value_template translation**: most upstream templates are simple (`{{ value|float|round(1) }}`, unit conversions, or `{{ value_json.battery_ok }}`). Express rounding/scaling declaratively (`value_transform: {round: N}` / `{scale: X}`). For `battery_ok` the HA `battery` binary_sensor device_class is `on == problem`, and rtl_433 `battery_ok: 1` means OK; encode the inversion in `payload`. Document any non-trivial transform in `docs/device-library.md`.
4. **`_skip_keys.yaml`**: a flat list:
   ```yaml
   skip_keys: [time, model, id, channel, subtype, type, mic, mod, freq, freq1, freq2, rssi, snr, noise, sequence_num, protocol, ...]
   ```
   Include all identity/diagnostic keys from upstream `SKIP_KEYS` plus obvious non-measurement keys.
5. **`docs/device-library.md`**: explain the schema, the themed-file layout, the add-a-mapping workflow, the diagnostics unmatched-keys feedback loop, and the user-override file (`<config>/rtl_433_mappings.yaml` layered on top — final path defined with Task 5; document the intent here).
6. Validate all YAML with the one-liner above. Commit with `feat:`.
7. This task only writes `device_library/*.yaml` and `docs/device-library.md` — keep strictly to those paths to stay disjoint from Task 3.
</details>
