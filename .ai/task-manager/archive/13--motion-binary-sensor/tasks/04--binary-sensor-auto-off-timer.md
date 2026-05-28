---
id: 4
group: "binary-sensor-runtime"
dependencies: [1, 3]
status: "completed"
created: "2026-05-28"
skills:
  - python
complexity_score: 5
complexity_notes: "Stateful timer lifecycle (schedule/reschedule/cancel) + restore-state interaction in a single entity class; the most intricate runtime piece."
---
# Synthesized auto-off clear-delay timer in `Rtl433BinarySensor`

## Objective
Make `Rtl433BinarySensor` behave like a Z2M occupancy sensor for `clear_delay` descriptors: turn on per detection, schedule a synthesized off after the effective delay, reschedule on every retrigger, cancel on removal, and never restore a stale `on`.

## Skills Required
`python` — Home Assistant entity lifecycle + `async_call_later` timers.

## Acceptance Criteria
- [ ] When a value maps to `on` and the descriptor has a `clear_delay`, the entity sets `is_on = True` and schedules a one-shot clear via `homeassistant.helpers.event.async_call_later` using the **effective** delay (from `effective_clear_delay_resolver`, falling back to the descriptor `clear_delay`).
- [ ] Each new detection **cancels and reschedules** the pending timer (window restarts on retrigger).
- [ ] When the timer fires, the entity sets `is_on = False` and writes state.
- [ ] The pending timer is cancelled in `async_will_remove_from_hass` (no late state write after removal).
- [ ] For `clear_delay` descriptors, a stale `on` is **not** restored on startup (comes back off/unknown until the next detection); non-`clear_delay` binary sensors are unchanged.
- [ ] `uvx ruff check .` and `uvx ruff format --check .` pass.

## Technical Requirements
- `binary_sensor.py` → `Rtl433BinarySensor` (`_apply_value`, `_async_restore_state`, init seeding from coordinator last event).
- Effective delay via `coordinator.effective_clear_delay_resolver(device_key)` (Task 3); fallback `descriptor.clear_delay`.
- `async_call_later(hass, delay, callback)` returns an unsub callable; store it and cancel before rescheduling and on removal.

## Input Dependencies
- Task 1: `clear_delay` attribute.
- Task 3: `effective_clear_delay_resolver` wired on the coordinator.

## Output Artifacts
- A motion binary_sensor that auto-clears — the core runtime behaviour the plan exists to deliver.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. Add a `self._clear_unsub: CALLBACK_TYPE | None = None` (or plain `Callable | None`) to `Rtl433BinarySensor.__init__`.

2. Factor the on/off handling so that whenever the entity transitions/refreshes to `on` for a descriptor with a `clear_delay`, it (re)starts the timer. Concretely, after `self._attr_is_on = apply_transform(...)` in `_apply_value` (and after the init seeding that calls `_apply_value`), if `self._descriptor.clear_delay` is set and `self._attr_is_on is True`, call a helper `self._schedule_clear()`.

3. `_schedule_clear()`:
   ```python
   def _schedule_clear(self) -> None:
       self._cancel_clear()
       delay = self._effective_clear_delay()
       self._clear_unsub = async_call_later(self.hass, delay, self._clear)
   ```
   Guard against `self.hass` being None before `async_added_to_hass` (seeding in `__init__` happens before add); if hass is not yet available, defer scheduling to `async_added_to_hass` based on current `is_on`.

4. `_effective_clear_delay()`: prefer `self.coordinator.effective_clear_delay_resolver(self._device_key)` if present, else `self._descriptor.clear_delay`. Match how the entity reaches the coordinator/device_key elsewhere in the class/base.

5. `_clear(now)` is an `@callback`:
   ```python
   @callback
   def _clear(self, _now) -> None:
       self._clear_unsub = None
       self._attr_is_on = False
       self.async_write_ha_state()
   ```

6. `_cancel_clear()` calls and clears `self._clear_unsub` if set.

7. Override `async_will_remove_from_hass` to call `self._cancel_clear()` (call super if it exists).

8. In `_async_restore_state`, short-circuit for `clear_delay` descriptors: if `self._descriptor.clear_delay` is set, do **not** restore a prior `on` (return without restoring, leaving state off/unknown). Keep the existing live-seed-wins behaviour for the normal path.

9. Import `async_call_later` from `homeassistant.helpers.event` and `callback` from `homeassistant.core`. Run ruff.
</details>
