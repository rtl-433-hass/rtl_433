---
id: 2
group: "device-library"
dependencies: [1]
status: "pending"
created: "2026-05-28"
skills:
  - yaml
---
# Reclassify `motion` as an occupancy binary_sensor in the library

## Objective
Move the `motion` field mapping out of the event platform and onto the binary side as an `occupancy` binary_sensor carrying a `clear_delay`, verifying the raw detection value so the `payload.on` token actually matches decoder output.

## Skills Required
`yaml` — device-library mapping files; reading test fixtures to confirm the raw value.

## Acceptance Criteria
- [ ] The `motion` entry is **removed** from `custom_components/rtl_433/device_library/events.yaml`.
- [ ] A `motion` entry is added to `custom_components/rtl_433/device_library/misc.yaml` with: `platform: binary_sensor`, `device_class: occupancy`, `name: Motion`, `object_suffix: motion`, `payload: { on: <verified detection value> }`, `clear_delay: 90`.
- [ ] The chosen `payload.on` token is confirmed against a real motion-decoder sample (test fixture or upstream rtl_433 output), and the confirmation source is noted in a YAML comment.
- [ ] Loading the registry yields `lookup(..., "motion").platform == "binary_sensor"`, `device_class == "occupancy"`, `clear_delay == 90`, and no `motion` event mapping remains.

## Technical Requirements
- `events.yaml` currently holds the `motion` entry (`platform: event`, `device_class: motion`).
- `misc.yaml` is the right home (not `binary_states.yaml`, whose entries are all `device_class: safety` / diagnostic). Place under a clear "occupancy / motion" comment.
- `clear_delay` is the descriptor attribute added in Task 1.

## Input Dependencies
- Task 1: `clear_delay` descriptor attribute must exist (else the loader ignores the key).

## Output Artifacts
- `motion` resolves as an occupancy binary_sensor with a 90 s default clear-delay.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. **Verify the raw value first.** rtl_433 motion decoders are not in the canonical mqtt-hass mapping; confirm the actual JSON value for `motion`. Check `tests/integration/rtl_433_tests/` capture outputs or existing fixtures, or upstream rtl_433 decoder docs (Interlogix / Risco Agility / Kerui). Common forms: `"motion": 1`. Pick the `on` token that matches the observed value (string form, since `apply_transform`/`_normalize_payload` compares stringified tokens). Record the source in a comment.

2. **Remove** the `motion:` block from `events.yaml` (and tidy any now-stale comment referencing motion there).

3. **Add** to `misc.yaml`:
   ```yaml
   # Occupancy / motion. PIR / occupancy decoders (Interlogix, Risco Agility,
   # Kerui, ...) transmit only ON DETECTION and never send an off; the
   # binary_sensor synthesizes the off via clear_delay (see Rtl433BinarySensor).
   # Raw value confirmed against <source>.
   motion:
     platform: binary_sensor
     device_class: occupancy
     name: Motion
     payload: { on: "1" }   # adjust to the verified detection value
     clear_delay: 90
     object_suffix: motion
   ```
   Only an `on` token is needed — the device never sends an off, so omit `off` (a non-matching value yields unknown, and the timer drives the off).

4. Confirm with a quick load, e.g. `python -c` that constructs the registry via the loader and asserts the three descriptor fields above and the absence of a `motion` event mapping.

5. `uvx ruff check .` is not needed for YAML, but run the existing `check yaml` pre-commit hook if convenient.
</details>
