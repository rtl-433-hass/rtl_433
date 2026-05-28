---
id: 8
group: "docs-and-tests"
dependencies: [2, 3, 4, 5, 6]
status: "pending"
created: "2026-05-28"
skills:
  - python
  - pytest
---
# Tests: motion binary_sensor timer, override, and migration

## Objective
Lock the new behaviour with a focused set of integration-style tests using the existing harness — a few tests, mostly integration.

## Skills Required
`python`, `pytest` — `pytest-homeassistant-custom-component`, the existing hub/lifecycle fixtures.

## Acceptance Criteria
- [ ] **Detection on / auto-off**: feeding a motion detection turns `binary_sensor.*_motion` `on`; advancing time past the effective delay (`async_fire_time_changed`) turns it `off`.
- [ ] **Reschedule on retrigger**: two detections within the window keep it `on`; it goes `off` only after a full quiet window elapses from the last detection.
- [ ] **Per-device override**: with `DEVICE_MOTION_CLEAR_DELAY` set for the device, the auto-off honours the overridden delay, not the default.
- [ ] **Cancel on remove**: removing the entity with a timer pending produces no late state write / error.
- [ ] **Migration**: a seeded `event.*_motion` registry entry is removed at setup, `motion` is dropped from persisted `DEVICE_EVENT_TYPES`, a repairs issue with the new translation_key exists, and no motion **event** entity is (re)created.
- [ ] `uv run pytest tests/` is green; `uvx ruff check .` / `uvx ruff format --check .` clean.

## Meaningful Test Strategy Guidelines
Your critical mantra: "write a few tests, mostly integration." Test YOUR logic (the timer lifecycle, the override resolution, the migration sweep), not HA framework internals. Combine related scenarios into single test functions; do not write per-CRUD or framework-feature tests.

## Technical Requirements
- Reuse `tests/conftest.py` `hub_entry_builder` and the lifecycle harness (`_setup_hub`/`_feed` are module-local in `tests/test_lifecycle.py` — import them or promote to conftest, as other test files do).
- Use `freezegun` / `async_fire_time_changed` for timer advancement (see `tests/test_coordinator.py` for the freeze pattern).
- Issue registry: `homeassistant.helpers.issue_registry.async_get(hass)`.

## Input Dependencies
- Tasks 2-6 (the as-built mapping, resolver, timer, options field, migration).

## Output Artifacts
- `tests/test_binary_sensor_motion.py` (or similarly named) covering the criteria above.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. Create `tests/test_binary_sensor_motion.py`. Set up a hub with a device that has a `motion` field and a known model, feed a detection through the coordinator (`_feed`), `await hass.async_block_till_done()`, and assert the `binary_sensor.*_motion` state.

2. **Auto-off**: after the detection asserts `on`, `async_fire_time_changed(hass, utcnow + timedelta(seconds=DEFAULT+1))`, block, assert `off`.

3. **Reschedule**: feed detection, advance < delay, feed again, advance < delay (but > delay since first), assert still `on`; then advance > delay since last, assert `off`.

4. **Override**: seed `entry.data[CONF_DEVICES][device_key][DEVICE_MOTION_CLEAR_DELAY]` (or set via options + reload) to a small value; assert the off fires on that value, not the default.

5. **Cancel on remove**: with a timer pending, remove the entity (`registry.async_remove` / unload) and advance time; assert no exception and no further state write (e.g. patch `async_write_ha_state` or assert state unchanged/removed).

6. **Migration**: pre-seed an entity registry `event` entry with unique_id `f"{hub}:{device_key}:motion"` for the config entry, run setup, then assert: that entity_id is gone; `motion` absent from `DEVICE_EVENT_TYPES`; `ir.async_get(hass).issues` contains the `motion_moved_to_binary_sensor` key; and no `event.*_motion` entity exists.

7. Keep it to one file, ~5-6 test functions. Run `uv run pytest tests/` and ruff.
</details>
