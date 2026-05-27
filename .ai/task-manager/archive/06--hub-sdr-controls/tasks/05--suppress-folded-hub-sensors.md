---
id: 5
group: "control-entities"
dependencies: [2]
status: "completed"
created: 2026-05-27
skills:
  - home-assistant
  - python
---
# Suppress the five folded Plan 3 hub sensors in managed mode

## Objective
Avoid two entities per concept. In **managed mode** the five SDR fields that become
controls — sample rate, frequency correction (ppm), gain, conversion mode, hop interval
— must be represented **only** by their control entities, so Plan 3's standalone
diagnostic sensors for those five are skipped. **Center frequency keeps its Plan 3
"actual" sensor** (actual ≠ desired under hopping), and all server-statistics sensors
are always kept. In **unmanaged mode**, all six SDR sensors remain (nothing changes).

## Skills Required
- `home-assistant` — the hub diagnostic sensor setup in `sensor.py`.
- `python` — a small guarded filter.

## Acceptance Criteria
- [ ] `sensor.py`'s `async_setup_entry` skips the `HubSensorDesc` entries whose
      `suffix` is in the folded set `{ "sample_rate", "ppm_error", "gain",
      "conversion_mode", "hop_interval" }` **only when**
      `coordinator.manage_settings` is True.
- [ ] The `center_frequency` SDR sensor and all server-statistics sensors
      (`decoded_events`, `ook_frames`, `fsk_frames`, `enabled_decoders`) are **always**
      created, in both modes.
- [ ] In unmanaged mode (`manage_settings` False) **all six** SDR sensors (including the
      five folded ones) are created exactly as before this task.
- [ ] The suppression is a localized, readable change (a single filter at the
      `async_add_entities(...)` call site); no `HubSensorDesc` definitions are deleted.
- [ ] `uv run ruff check custom_components/rtl_433` passes;
      `uv run pytest tests/test_lifecycle.py` passes (existing hub-sensor assertions in
      the default `manage_settings=True` path may need the suppression accounted for —
      coordinate with Task 6 if an existing assertion counts those sensors).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Read the managed flag from the coordinator (`coordinator.manage_settings`), the single
  source of truth set by Task 2/Task 3 — do **not** re-resolve the option from the entry
  here.
- Keep the folded-suffix set defined as a small module-level constant in `sensor.py`
  (e.g. `_FOLDED_HUB_SENSOR_SUFFIXES`) so it reads as intentional and is easy to audit
  against the registry's controllable fields.
- This is the **only** change Plan 6 makes to Plan 3's hub-sensor builder; do not alter
  the stats sensors, the center-frequency attributes (`frequencies`/`hop_times`), or any
  per-device sensor logic.

## Input Dependencies
- Task 2: `coordinator.manage_settings`.

## Output Artifacts
- `sensor.py` with the guarded skip.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

In `sensor.py` add the constant and filter the generator in `async_setup_entry`:
```python
# SDR sensors whose concept is folded into a Plan 6 control in managed mode; their
# diagnostic sensor is suppressed so each concept has exactly one entity. Center
# frequency is intentionally NOT folded (its actual can diverge from the desired
# value under hopping), and the server-stats sensors are never folded.
_FOLDED_HUB_SENSOR_SUFFIXES = frozenset(
    {"sample_rate", "ppm_error", "gain", "conversion_mode", "hop_interval"}
)
```
```python
async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    managed = coordinator.manage_settings
    async_add_entities(
        Rtl433HubSensor(coordinator, entry.entry_id, desc)
        for desc in HUB_SENSORS
        if not (managed and desc.suffix in _FOLDED_HUB_SENSOR_SUFFIXES)
    )
    await async_setup_hub_platform(...)   # unchanged
```
Leave the `HUB_SENSORS` tuple and every `HubSensorDesc` intact; only the iteration is
filtered.

Run `uv run ruff check custom_components/rtl_433` and
`uv run pytest tests/test_lifecycle.py` before finishing. If an existing lifecycle test
asserts the presence/count of the now-suppressed sensors under the default
(`manage_settings=True`) setup, note it for Task 6 rather than weakening this guard.
</details>
