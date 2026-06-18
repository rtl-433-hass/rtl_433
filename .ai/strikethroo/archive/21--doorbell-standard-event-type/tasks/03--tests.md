---
id: 3
group: "testing"
dependencies: [1, 2]
status: "completed"
created: 2026-06-04
skills:
  - pytest
---
# Tests: doorbell value mapping, RING compliance, and migration

## Objective
Add focused, meaningful tests covering the new doorbell behavior: value→type firing (`ring`/`secret_knock`), the deprecation-compliance invariant (`ring` present at construction), and the migration of persisted `event_types`. Update existing mapping tests for the new `secret_knock` descriptor shape.

## Skills Required
- `pytest` (pytest-homeassistant-custom-component, Python 3.14 via `uv`)

## Acceptance Criteria
- [ ] A test asserts a `secret_knock=0` frame makes the doorbell event entity fire `event_type == "ring"`, and a `secret_knock=1` frame fires `event_type == "secret_knock"`.
- [ ] A test asserts the shipped doorbell entity's `event_types` contains `"ring"` immediately on construction (before any transmission), so HA's doorbell deprecation check passes (no "does not support the 'ring' event type" warning when added to hass).
- [ ] A test asserts the `minor_version=5` migration rewrites persisted `secret_knock` `event_types` `["0", "1"]` → `["ring", "secret_knock"]`, and that re-running it is a no-op (idempotent).
- [ ] Existing `tests/test_mapping.py` assertions for `secret_knock` are updated to reflect `device_class == "doorbell"` plus the new `event_map`.
- [ ] A regression assertion confirms a non-doorbell button event (no `event_map`) still fires the stringified raw value.
- [ ] `uv run pytest` passes with no failures and emits no doorbell `ring` deprecation warning for the doorbell entity.

## Meaningful Test Strategy Guidelines
Mantra: "write a few tests, mostly integration." Test custom business logic and critical paths — the value mapping, the RING invariant, and the migration — not framework behavior. Do NOT add tests for HA's `EventEntity` internals, trivial getters, or each persisted-value permutation. Combine related scenarios into single tests where natural.

## Technical Requirements
- Test toolchain: Python 3.14 via `uv` (system Python is 3.13 and lacks the test stack). Run `uv run pytest`.
- Reuse existing fixtures/patterns: `tests/fixtures/doorbell_event.json`, the `_doorbell_frame(...)` helper and coordinator setup in `tests/test_coordinator.py`, and the entity/hub setup helpers used across the suite.

## Input Dependencies
- Task 1 (descriptor `event_map` + entity firing/seeding).
- Task 2 (migration + `minor_version=5`).

## Output Artifacts
- New/updated tests proving the success criteria.

## Implementation Notes

<details>
<summary>Step-by-step implementation</summary>

**Firing + invariant tests.** There is no `tests/test_event.py` today; event behavior is exercised via `tests/test_coordinator.py` (the `_doorbell_frame` helper builds `{"model": "Honeywell-Doorbell", "id": 7, "secret_knock": value}`). Add a focused test (either a new `tests/test_event.py` or alongside the existing doorbell coordinator tests):
- Set up the hub + coordinator so a `secret_knock` event entity exists (follow the existing doorbell setup in `test_coordinator.py`).
- Dispatch a frame with `secret_knock=0`; assert the entity state's `event_type` attribute (`homeassistant.components.event.const.ATTR_EVENT_TYPE`) is `"ring"`.
- Dispatch a fresh frame with `secret_knock=1`; assert `event_type == "secret_knock"`. (Use distinct `time` values so the coordinator treats them as genuine transmissions, per the existing helper usage; the entity dedupes by object identity, so distinct frames each fire.)
- Assert `entity.event_types` (or the `event_types` capability) contains `"ring"` right after construction / add — i.e. before dispatching anything. Optionally use `caplog` to assert the HA warning string `"does not support the 'ring' event type"` is NOT present after the entity is added to hass.

**Button regression.** Add (or extend) a test that a `button` event field (no `event_map`) still fires `str(value)` — e.g. a frame with `button=14` fires `event_type == "14"`. Keep it minimal.

**Migration test.** Mirror the existing migration test style (look for tests covering `async_migrate_entry` / minor-version bumps — likely in a `test_migration.py` or within `test_mut_init.py`; search for `minor_version` in tests). Build a config entry at `version=2, minor_version=4` whose `data[CONF_DEVICES][<device_key>][DEVICE_EVENT_TYPES]["secret_knock"] == ["0", "1"]`, run `async_migrate_entry`, and assert the persisted list becomes `["ring", "secret_knock"]` and `minor_version == 5`. Run the migration a second time and assert the list is unchanged (idempotent) and no spurious write occurs.

**Update `test_mapping.py`.** Around lines 82-108, the test asserts `secret_knock` maps to `device_class == "doorbell"`. Add an assertion that the descriptor's `event_map == {"0": "ring", "1": "secret_knock"}`. Do not weaken the existing `button`/`secret_knock` device_class assertions.

**Mutation tests.** If `test_mut_*` files assert on the `secret_knock` descriptor or event firing and now fail due to the new behavior, update those assertions to match (e.g. `test_mut_device_trigger.py` already uses `"secret_knock": ["ring"]`). Keep changes minimal and behavior-faithful.
</details>
