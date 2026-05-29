# Device Mapping Library — Contributor Guide

The rtl_433 integration turns the JSON fields emitted by rtl_433 into Home
Assistant entities using a **data-driven device library**: a set of themed YAML
files that map each rtl_433 field name to a Home Assistant entity descriptor.

Adding or correcting support for a device is a small, reviewable YAML change —
no Python edits, and no integration-logic risk. This guide documents the schema,
where the files live, the add-a-mapping workflow, how diagnostics tell you what
is missing, and how the per-installation user override works.

> The shipped library is a faithful port of the curated `mappings` table and
> `SKIP_KEYS` from rtl_433's own `examples/rtl_433_mqtt_hass.py`. The mapping
> *semantics* (device class, unit, state class, value transform, unique-id
> suffix) are reused; the MQTT transport is discarded.

## Where the files live

```
custom_components/rtl_433/device_library/
├── _skip_keys.yaml         # fields that never become entities
├── air_quality.yaml        # pm2.5 / pm10 / co2
├── binary_states.yaml      # contacts, tamper, alarm, door state
├── events.yaml             # momentary RF: button, doorbell
├── humidity_moisture.yaml  # humidity, moisture, leak, depth
├── light_uv.yaml           # illuminance, UV
├── misc.yaml               # battery %, timestamp, signal, lightning
├── power_electrical.yaml   # power, energy, current, voltage, consumption
├── pressure.yaml           # barometric pressure
├── rain.yaml               # rain total / rate
├── temperature.yaml        # temperature variants
└── wind.yaml               # wind speed / gust / direction
```

The loader (see `mapping.py`) reads **every** `*.yaml` file in this directory at
startup, merges all entries into one lookup table keyed by field name, and reads
`_skip_keys.yaml` separately as the exclusion list. Grouping is purely
organizational: put a new field in whichever themed file fits its domain, or in
`misc.yaml` if nothing fits. A user-supplied override file (see
[User overrides](#user-overrides)) is layered on top of the merged result.

Files whose name starts with `_` (currently only `_skip_keys.yaml`) are treated
specially by the loader and are not parsed as field-mapping tables.

## Mapping entry schema

Each themed file is a YAML mapping whose **top-level keys are rtl_433 field
names** exactly as they appear in the JSON event (e.g. `temperature_C`,
`wind_avg_km_h`, `battery_ok`). Each value is an entry with the attributes
below.

```yaml
temperature_C:
  platform: sensor
  device_class: temperature
  unit_of_measurement: "°C"
  state_class: measurement
  name: Temperature
  value_transform: { round: 1 }
  object_suffix: T
```

### Attributes

| Attribute             | Required | Type            | Meaning |
|-----------------------|----------|-----------------|---------|
| `platform`            | yes      | `sensor` \| `binary_sensor` \| `event` | Which Home Assistant platform creates the entity. See [Event entities](#event-entities) for `event`. |
| `device_class`        | yes (nullable) | string \| `null` | Home Assistant device class (e.g. `temperature`, `humidity`, `safety`). Use `null` when the field has no appropriate device class. For `event` entries it is an [`EventDeviceClass`](https://www.home-assistant.io/integrations/event/#device-class) (`button`, `doorbell`). |
| `unit_of_measurement` | yes (nullable) | string \| `null` | Unit shown by the entity. `null` for unitless or binary fields. |
| `state_class`         | yes (nullable) | `measurement` \| `total` \| `total_increasing` \| `null` | Long-term-statistics class. `null` for binary fields and non-numeric sensors. |
| `name`                | yes      | string          | Human-readable entity name (suffixed to the device name by HA). |
| `object_suffix`       | yes      | string          | Short, stable token appended to the device key to form the entity's unique id. **Must be stable** — changing it orphans existing entities. |
| `value_transform`     | no       | mapping         | Declarative numeric transform applied before the value is stored. See [Value transforms](#value-transforms). Omit for binary fields. |
| `payload`             | no       | `{ on: <raw>, off: <raw> }` | For `binary_sensor` only: maps the raw rtl_433 value to the HA on/off state. See [Binary payloads](#binary-payloads). |
| `clear_delay`         | no       | int (seconds)   | For `binary_sensor` only: seconds after a detection to **synthesize** an off, for detect-only hardware that sends no off (e.g. motion/PIR). Reschedules on each detection; per-device override via the options flow. See [Motion / occupancy](#motion--occupancy). |
| `force_update`        | no       | bool            | Mirrors upstream `force_update`; write state even when the value is unchanged. Defaults to false. |
| `entity_category`     | no       | `diagnostic` \| `config` \| `null` | Categorizes the entity in the HA UI. Diagnostic fields (battery, signal, tamper) use `diagnostic`. |
| `enabled_by_default`  | no       | bool            | Set `false` to register the entity disabled (the user can enable it). Defaults to true. |
| `icon`                | no       | string          | Optional `mdi:` icon override. |

`null` is written explicitly (YAML `null`) rather than omitted for the three
"required (nullable)" attributes, so every entry is uniform and the loader never
has to guess intent. Optional attributes may simply be omitted.

### Value transforms

`value_transform` declares how to convert the raw JSON value into the stored
state. It replaces the Jinja `value_template` strings used by the upstream MQTT
example. Supported keys (applied in this order):

| Key      | Effect | Upstream template it replaces |
|----------|--------|-------------------------------|
| `float`  | Coerce to float. | `{{ value\|float }}` |
| `int`    | Coerce to int. | `{{ value\|int }}` |
| `scale`  | Multiply by the given number. | unit conversions, e.g. m/s → km/h (`* 3.6`) |
| `offset` | Add the given number (after `scale`). | additive offsets |
| `round`  | Round to N decimal places (the value of the key). | `\|round(N)` |

Keys combine. The application order is: coerce (`float`/`int`) → `scale` →
`offset` → `round`. Examples drawn from the shipped library:

| rtl_433 field   | Upstream `value_template`                       | `value_transform`                  |
|-----------------|-------------------------------------------------|------------------------------------|
| `temperature_C` | `{{ value\|float\|round(1) }}`                  | `{ round: 1 }`                     |
| `humidity`      | `{{ value\|float }}`                            | `{ float: true }`                  |
| `lux`           | `{{ value\|int }}`                              | `{ int: true }`                    |
| `wind_avg_m_s`  | `{{ (float(value) * 3.6) \| round(2) }}`        | `{ scale: 3.6, round: 2 }`         |
| `battery_ok`    | `{{ ((float(value) * 99)\|round(0)) + 1 }}`     | `{ scale: 99, offset: 1, round: 0 }` |

`round` implies float coercion, so `{ round: 1 }` is equivalent to
`{ float: true, round: 1 }` and the shorter form is preferred.

### Binary payloads

`binary_sensor` entries use `payload` instead of `value_transform`. It maps the
raw rtl_433 value to Home Assistant's on/off state:

```yaml
detect_wet:
  platform: binary_sensor
  device_class: moisture
  payload: { on: "1", off: "0" }   # 1 == wet == on
```

Note the direction matters for some fields. The upstream `closed` field is
**inverted** — a value of `0` means the contact is open — so its payload is
`{ on: "0", off: "1" }`. Raw values are quoted strings to match how rtl_433
emits them.

> **`battery_ok` note.** rtl_433's `battery_ok` is boolean-ish (`1` = OK,
> `0` = low). The upstream example does *not* model it as a binary sensor; it
> converts it to a battery **percentage** sensor (`0` → 1 %, `1` → 100 %) so it
> displays on the standard HA battery card. The shipped library preserves that:
> `battery_ok` is a `sensor` with `device_class: battery`, `unit: "%"`, and
> `value_transform: { scale: 99, offset: 1, round: 0 }`. If you prefer a
> low-battery binary problem sensor, that is a candidate for a user override.

### Motion / occupancy

PIR / occupancy decoders (Interlogix, Risco Agility, Kerui, …) emit `motion`
**only on detection** (raw value `1`) and **never send an off** — the hardware is
detect-only. So `motion` is a `binary_sensor` (device class `occupancy`) whose
`payload` declares only an `on` token; the off state is **synthesized** by a
timer rather than received:

```yaml
motion:
  platform: binary_sensor
  device_class: occupancy
  name: Motion
  payload: { on: "1" }   # detect-only: no off token
  clear_delay: 90        # synthesize off 90 s after the last detection
  object_suffix: motion
```

The `clear_delay` attribute (seconds) drives the synthesized off: the sensor
turns `on` on each detection and is auto-cleared to off after the delay elapses
with no re-detection. Every fresh detection **reschedules** the timer, so the
off window restarts on each retrigger. The shipped default is **90 s**.

A stale `on` is never restored across a restart (there would be no live timer to
clear it): the sensor comes back off/unknown until the next detection.

**Per-device override.** The delay can be tuned per device in the options flow —
**Settings → Devices & Services → rtl_433 → Configure → (device step)** exposes a
*Motion clear delay (seconds)* field, shown only for motion-bearing devices.
Leave it blank to use the 90 s default. The override is resolved at runtime
(per-device value, else the descriptor default).

### Event entities

`platform: event` is for **momentary, fire-and-forget** RF fields — a remote
button, a doorbell press — that have no steady "on" / "off"
state to track. Each genuine transmission fires **one** Home Assistant
[event](https://www.home-assistant.io/integrations/event/), and the entity
stays available between presses (no faked "off"). Event entries live in their
own themed file, `device_library/events.yaml`:

```yaml
button:
  platform: event
  device_class: button     # an EventDeviceClass
  name: Button
  object_suffix: button
```

How event entries differ from `sensor` / `binary_sensor`:

- **The fired `event_type` is the stringified field value** (`str(value)`).
  There is **no `payload` and no `value_transform`** — the raw value is
  stringified directly.
- **`event_types` are auto-populated, not declared.** Each newly observed value
  is recorded as a valid type the first time it is seen and **persisted per
  device**, so after a restart the entity rebuilds knowing the types it has
  seen before. You never list them in the YAML.
- A field that only ever emits **one distinct value** (a doorbell press) is a
  **momentary single-type trigger** — it fires that one type on every
  transmission. A field whose value varies (a remote that reports which button
  was pressed) auto-populates several types.
- **The fired event carries no extra attributes** — the type is the only
  payload.
- `device_class` is an `EventDeviceClass` (`button`, `doorbell`).

The shipped `events.yaml` has two examples:

| Field | `device_class` | Notes |
|-------|----------------|-------|
| `button` | `button` | Remote / key-fob button code; the value is the pressed code, so distinct presses auto-populate several types. |
| `secret_knock` | `doorbell` | Honeywell ActivLink doorbell press; a single momentary value. |

> **`motion` is not an event entity.** PIR / occupancy decoders emit `motion`
> on detection but never send an off, so it is modelled as a detect-only
> `binary_sensor` (device class `occupancy`) with a synthesized off — see
> [Motion / occupancy](#motion--occupancy). (Earlier versions exposed it as an
> `event`; the entity_id changed `event.*_motion` → `binary_sensor.*_motion`.)

## Model-scoped mappings (`models:`)

The top-level keys above are the **global** defaults: a `temperature_C` entry
applies to *every* device that emits `temperature_C`. Some fields, though, need
a different descriptor depending on the **device model** — most notably the
utility-meter consumption counters (`consumption`, `consumption_data`), whose
unit and scale are *not* carried in the RF signal and differ between meter
models. For those, a file may carry an optional top-level **`models:`** block
that overrides the global descriptor for one specific rtl_433 `model` string.

`models:` is keyed by the exact rtl_433 `model` value, and each model maps to a
table of `field_key → descriptor` using the **same per-field attribute schema**
as the global entries (`platform`, `device_class`, `unit_of_measurement`,
`state_class`, `name`, `object_suffix`, `value_transform`, …):

```yaml
# top-level global defaults (unchanged) live here ...
temperature_C:
  platform: sensor
  device_class: temperature
  unit_of_measurement: "°C"
  state_class: measurement
  name: Temperature
  value_transform: { round: 1 }
  object_suffix: T

# ... and an optional model-scoped block sits alongside them:
models:
  Some-Model-Name:            # an exact rtl_433 `model` string
    consumption_data:
      platform: sensor
      device_class: energy
      unit_of_measurement: kWh
      state_class: total_increasing
      name: Consumption
      value_transform: { scale: 1 }
      object_suffix: consumption
```

The `models:` block is **additive and optional**: every existing themed file
parses exactly as before, and the flat top-level keys remain the global default.
It may appear in any themed file (most naturally `power_electrical.yaml`) and in
the [user-override file](#user-overrides). `models` is a reserved top-level key —
the loader intercepts it, so you cannot have a *field* literally named `models`.

### Lookup resolution order

When the integration builds an entity for a field on a device, it resolves the
descriptor **most-specific first**:

1. The **model-scoped** entry for `(model, field_key)`, if the device's model has
   a `models:` block with that field.
2. Otherwise the **global** flat entry for `field_key`.
3. Otherwise the field is unmapped → no entity.

So a `models:` entry only affects the model it names; every other model keeps the
global descriptor for that same field.

### Precedence (specificity-first)

Combined with [user overrides](#user-overrides) and the per-device meter
calibration (the options-flow *Device settings* step), the full precedence for a
single field on a single device is, **highest to lowest**:

1. **Per-device calibration** (commodity + base unit + scale, set in the options
   flow) — applies only to the consumption field(s) of the one calibrated device.
2. **Model-scoped** entry — user-override `models:` entry, else shipped
   `models:` entry.
3. **Global** flat entry — user-override flat key, else shipped flat key.
4. Unmapped → no entity.

The rule is **specificity-first**: a model-scoped entry always beats a global one
*regardless of source*. In particular a **shipped** `models:` entry outranks a
**user-override global** entry for a matching model. Within each tier the user
file beats the shipped library. (This falls out naturally from the merge: the
user override replaces the shipped entry *within* a tier, and the lookup checks
the model tier before the global tier.)

> **No speculative real-meter mappings ship.** Because a meter's consumption
> unit/scale is not knowable from the signal, the shipped library does **not**
> carry a guessed `models:` consumption mapping for any real model — a wrong
> scale would silently corrupt real Energy data. The example below is purely
> illustrative; for a real meter use the per-device calibration step in the
> options flow (see the README) until a model's unit/scale is authoritatively
> known.

### Worked example (ILLUSTRATIVE — not a real meter mapping)

> ⚠️ **This is a made-up model name and does not match any real device.** Do
> **not** copy it into a live library expecting it to scale a real meter
> correctly. It exists only to show the `models:` schema end-to-end; for a real
> meter, calibrate per device in the options flow instead.

The example below makes a consumption counter Energy-dashboard-eligible for one
illustrative model by attaching a real `device_class`, a convertible base unit,
`state_class: total_increasing`, and a `scale`:

```yaml
models:
  ACME-NotAReal-Meter-9000:     # ILLUSTRATIVE model string — not a real device
    consumption_data:
      platform: sensor
      device_class: energy           # makes it Energy-dashboard-eligible
      unit_of_measurement: kWh       # a convertible base unit for `energy`
      state_class: total_increasing  # required for the Energy dashboard
      name: Consumption
      value_transform: { scale: 0.01 }   # raw counter × 0.01 (illustrative)
      object_suffix: consumption
```

For any device whose model is *not* `ACME-NotAReal-Meter-9000`, `consumption_data`
keeps the shipped global descriptor (the unitless `total_increasing` counter), and
unrelated fields like `temperature_C` are unaffected on every model.

## The skip-keys file

`_skip_keys.yaml` lists fields that must never produce an entity — device
identity (`model`, `id`, `channel`, `subtype`, `type`), message bookkeeping
(`mic`, `mod`, `sequence_num`, `message_type`, `exception`, `raw_msg`), and
radio-tuning fields (`freq`, `freq1`, `freq2`, `protocol`):

```yaml
skip_keys:
  - type
  - model
  - id
  # ...
```

The loader checks a field against this list *before* attempting a mapping
lookup. Identity keys (`model` + `id`/`channel`/`subtype`) are consumed by the
event normalizer to derive the device key, which is why they are skipped here
rather than mapped to entities.

## Add-a-mapping workflow

1. **Find the field name.** Watch your rtl_433 stream or check the integration's
   diagnostics for the exact JSON key (see
   [Diagnostics feedback loop](#diagnostics-feedback-loop)). rtl_433 field
   names are case-sensitive and unit-suffixed (`temperature_C`, not
   `temperature`).
2. **Pick the themed file** that matches the field's domain, or `misc.yaml`.
3. **Add an entry** keyed by the exact field name, filling in the required
   attributes. Copy a similar existing entry as a template.
   - For a numeric reading: choose `platform: sensor`, the closest
     [HA device class][hadc], the unit rtl_433 reports, a `state_class`
     (`measurement` for instantaneous readings, `total_increasing` for
     monotonic counters like rain or energy), and a `value_transform`.
   - For a boolean: choose `platform: binary_sensor`, a device class, and a
     `payload` mapping. Leave `unit_of_measurement` / `state_class` `null`.
   - Choose a short, **stable** `object_suffix` that is unique among the fields
     a single device emits.
4. **Validate** the YAML:
   ```bash
   python3 -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('custom_components/rtl_433/device_library/*.yaml')]; print('ok')"
   ```
5. **Add a fixture/test** if you are contributing upstream (see the project test
   suite) and open a PR with a `feat:` conventional commit.

[hadc]: https://www.home-assistant.io/integrations/sensor/#device-class

## Diagnostics feedback loop

The integration's diagnostics export records, per hub, the **unmatched field
keys** it has seen — JSON keys that are neither in `_skip_keys.yaml` nor present
in the mapping library. This is the canonical way to discover what a device
emits that the library does not yet cover:

1. Download diagnostics for the hub (Settings → Devices & Services → the rtl_433
   integration → ⋮ → *Download diagnostics*).
2. Look at the `unmatched_keys` section — each entry is a field name your
   hardware sent that produced no entity.
3. For each key you care about, add a mapping (above) or, if it is genuinely
   noise/identity, add it to `_skip_keys.yaml` to silence it.

This closes the loop: missing support shows up as concrete field names, and each
is fixed by a one-line YAML addition.

## User overrides

You can extend or correct the shipped library **without editing the integration
files** by dropping a YAML file at:

```
<config>/rtl_433_mappings.yaml
```

(`<config>` is your Home Assistant configuration directory — the one containing
`configuration.yaml`.) This file uses the **same schema** as the themed library
files: top-level keys are rtl_433 field names, values are entry mappings. It may
optionally include a `skip_keys:` list to add extra skip entries, and an optional
[`models:` block](#model-scoped-mappings-models) to add or override model-scoped
descriptors.

The loader layers this file **on top of** the shipped library:

- A field present in both the override and the shipped library: the **override
  wins** (full entry replacement, not a deep merge), so you can correct a unit,
  device class, or transform.
- A field present only in the override: it is **added** as a new mapping.
- `skip_keys` entries in the override are **unioned** with the shipped skip
  list.
- A `models:` block in the override is **merged per `(model, field_key)`**: an
  override model-scoped entry replaces the shipped one for the same model and
  field, while other shipped model fields are preserved. Per the
  [precedence rules](#precedence-specificity-first), a model-scoped entry (from
  either source) always beats a global one — so a **shipped** `models:` entry
  outranks a **user-override global** entry for a matching model.

Example override that adds an unmapped field and re-classifies `battery_ok` as a
low-battery binary problem sensor:

```yaml
# <config>/rtl_433_mappings.yaml
custom_field_C:
  platform: sensor
  device_class: temperature
  unit_of_measurement: "°C"
  state_class: measurement
  name: Custom Probe
  value_transform: { round: 1 }
  object_suffix: TC

battery_ok:
  platform: binary_sensor
  device_class: battery     # HA "battery": on == problem (low)
  unit_of_measurement: null
  state_class: null
  name: Battery
  payload: { on: "0", off: "1" }   # battery_ok == 0 means low -> problem
  entity_category: diagnostic
  object_suffix: B
```

Changes to the override file are picked up on the next reload of the
integration (or HA restart). A full graphical mapping editor is intentionally
out of scope; the drop-in override file covers the "users can add their own
mappings" need.

## Notes on fields that cannot be expressed declaratively

The upstream `mappings` table includes two `device_automation` entries —
`channel` and `button` — that publish MQTT **device triggers** (e.g.
`button_short_release`) rather than entities. These have no `sensor` /
`binary_sensor` equivalent in this schema:

- `channel` is already a device-identity key and lives in `_skip_keys.yaml`.
- `button` is modelled as an [event entity](#event-entities) instead of an MQTT
  device trigger — see `device_library/events.yaml`.

Everything else from the upstream table is ported faithfully.
