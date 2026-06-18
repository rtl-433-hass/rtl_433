---
id: 2
group: "ui"
dependencies: [1]
status: "completed"
created: "2026-05-31"
skills: ["python"]
---

# Config flow + translations: allow 0 = never

## Objective

Let users enter `0` (never expire) in both the hub availability-timeout field and the per-device override field, and update the user-facing strings to explain the new behavior.

## Skills Required

- `python` (Home Assistant config flow / voluptuous + NumberSelector)

## Output

Edits to:
- `custom_components/rtl_433/config_flow.py` — lower the `NumberSelector` minimum from `1` to `0` for both the hub `availability_timeout` field and the per-device `timeout_override` field.
- `custom_components/rtl_433/translations/en.json` — update labels/descriptions for `availability_timeout` and `timeout_override`.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

**Verified current state:**
- `config_flow.py` has a hub options step and a per-device step. Each uses a `NumberSelector(NumberSelectorConfig(min=1, max=..., step=1, unit_of_measurement="seconds", mode=NumberSelectorMode.BOX))` for the timeout field. The `min=1` is what currently prevents `0`.
- `translations/en.json` contains, under the options steps:
  - `availability_timeout` label: "Availability timeout (seconds)" and description: "Mark a device unavailable after this many seconds without an event."
  - `timeout_override` label: "Availability timeout override (seconds)" and description: "Mark this device unavailable after this many seconds without an event. Leave blank to use the hub default."

**Steps:**

1. In `config_flow.py`, find BOTH `NumberSelectorConfig(...)` blocks used for the availability timeout (hub step) and the timeout override (device step). Change `min=1` to `min=0` in both. Leave `max`, `step`, `unit_of_measurement`, and `mode` unchanged. If the field uses a different validator (e.g. `vol.Coerce(int)` + `vol.Range(min=1)`), change that `min` to `0` as well. Do not change which keys are Optional/Required or the `suggested_value` wiring.

2. In `translations/en.json`, update the wording so users understand `0` and the new class-aware default:
   - Hub `data_description.availability_timeout`: something like "Mark a device unavailable after this many seconds without an event. Use 0 to never mark devices unavailable. Devices that send rare events (door, window, motion) automatically get a longer default."
   - Device `data_description.timeout_override`: something like "Mark this device unavailable after this many seconds without an event. Use 0 to never mark it unavailable. Leave blank to use the automatic default for this device's type (longer for door/window/motion sensors)."
   - Keep labels concise; you may keep the existing labels or append "(0 = never)".
   - Preserve valid JSON (commas, quoting). Do not reformat unrelated keys.

3. If other locale files exist under `translations/` mirroring these keys, you may leave them (English is the source); only `en.json` is required by this task.

**Dependency:** Depends on task 1 only in that the feature must exist; this task references no new constants directly but should land after/with task 1. Do not implement resolution logic here.

**Validation:** Ensure `en.json` is valid JSON (e.g. `python3 -m json.tool` is fine for JSON validity — that does not depend on the 3.14 stack). Full behavioral tests are in task 4.

</details>
