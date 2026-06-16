---
id: 2
group: "event-trace-instrumentation"
dependencies: []
status: "completed"
created: 2026-06-15
skills:
  - python
---
# Add firing / dedupe DEBUG trace in the `event` entity

## Objective
Make the `event` entity's decision observable at DEBUG: log when it actually fires a Home Assistant event (e.g. doorbell `ring`) and when it dedupes a watchdog re-dispatch (cached event, same object identity → no re-fire). Leave the existing INFO suppressed-replay line unchanged.

## Skills Required
- `python` — Home Assistant entity / dispatcher conventions, editing `event.py`.

## Acceptance Criteria
- [ ] In `event.py::Rtl433Event._handle_dispatch`, a `LOGGER.debug(...)` line is emitted when a genuine live transmission fires an HA event, including the `device_key`, the field key, the raw value, and the resolved `event_type`.
- [ ] A distinct `LOGGER.debug(...)` line is emitted when the watchdog re-dispatch is deduped by object identity (the `event is self._last_fired_event` branch) — no re-fire.
- [ ] The existing `LOGGER.info("rtl_433 suppressed replayed/stale ...")` line in the replay branch is unchanged (no new DEBUG added in that branch to avoid double logging).
- [ ] Log calls use lazy `%`-style args and cannot raise; the shared `LOGGER` (already imported in `event.py`) is reused.
- [ ] No change to firing behavior, event types, persistence, or signatures.

## Technical Requirements
- File: `custom_components/rtl_433/event.py`, method `_handle_dispatch` (around line 128).
- `LOGGER` is already imported from `.const` in this module.
- Relevant locals: `field_key = self._descriptor.field_key`, `self._device_key`, `event.fields[field_key]` (raw value), `event_type` (resolved around line 180), `self._last_fired_event`.

## Input Dependencies
None (independent file from Task 1).

## Output Artifacts
- DEBUG `fired ... event_type=` and watchdog-dedupe trace lines whose textual contract is consumed by Task 3 (tests) and described by Task 4 (docs).

## Implementation Notes
<details>

Current structure of `_handle_dispatch`:
1. `if event.is_replay:` → logs INFO suppressed (KEEP AS-IS), writes state, returns.
2. `if event is self._last_fired_event:` → watchdog re-paint of cached event; writes state, returns. **Add a DEBUG line here**, e.g.:
   ```python
   LOGGER.debug(
       "rtl_433 skipped watchdog re-paint for %s (no re-fire)",
       self._device_key,
   )
   ```
3. `if field_key in event.fields:` → resolves `event_type` and fires. **Add a DEBUG line right after `event_type` is resolved / before or after firing**, e.g.:
   ```python
   LOGGER.debug(
       "rtl_433 fired %s for %s field=%s value=%s -> event_type=%s",
       event_type,
       self._device_key,
       field_key,
       raw,
       event_type,
   )
   ```
   (Keep it to one line; you may trim redundant args — the key data are `device_key`, `field_key`, raw `value`, and resolved `event_type`.)

Notes:
- Do not alter the `_attr_event_types` append, the `async_upsert_event_types` scheduling, or the `self._trigger_event(...)`/`async_write_ha_state()` calls — only add logging around them.
- Match the existing `rtl_433 ...` message prefix and `%`-arg style used by the INFO line directly above.
- Confirm no f-strings are introduced (lazy logging).
- Run the repo's lint/format pass for consistency.
</details>
