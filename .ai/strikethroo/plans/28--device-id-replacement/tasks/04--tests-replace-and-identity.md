---
id: 4
group: "quality"
dependencies: [1, 2, 3]
status: "pending"
created: 2026-08-31
skills:
  - pytest
  - home-assistant-integration
---
# Tests for the re-key helper, the replace flow, and the serial number

## Objective

Prove the properties users actually depend on: that a replace preserves
`entity_id` (and therefore recorder history), moves the stored per-device
settings, removes the duplicate, and leaves the frozen ABI templates intact —
and that the guard paths reject bad input.

## Skills Required

`pytest` with `pytest-homeassistant-custom-component`.
`home-assistant-integration` — driving an options flow and asserting registry
state in tests.

## Acceptance Criteria

- [ ] A happy-path test asserts, across `async_replace_device`: every surviving
      entity keeps its **`entity_id`** and its **registry row id**; each
      `unique_id` moved from `…:{old_key}:{suffix}` to `…:{new_key}:{suffix}`
      with the suffix unchanged; the device row is the *same row* now carrying
      `(DOMAIN, f"{entry_id}:{new_key}")`.
- [ ] A test covers the realistic collision case: the new device **already has a
      full set of entities** before the replace, and they are removed rather than
      blocking the rewrite.
- [ ] A test asserts the record fold: `timeout_override`, `calibration`,
      `motion_clear_delay` and `event_types` survive under the new key, `fields`
      is the union of both records, and the old key is gone.
- [ ] A test covers adopting a `new_key` that has **no** record in the devices map
      (the discovery-disabled case).
- [ ] Guard tests assert `DeviceReplaceError` for: unknown `old_key`,
      `old_key == new_key`, and an empty key.
- [ ] A flow test drives the options flow end to end (`init` -> `replace` ->
      `replace_target`) and asserts the resulting registry/data state, plus the
      `no_devices` abort on an empty devices map.
- [ ] A test asserts `DeviceInfo["serial_number"]` for a key with a suffix and
      its absence for a model-only key.
- [ ] `tests/test_migration_roundtrip.py` passes **unmodified**
      (`git diff --stat origin/main -- tests/test_migration_roundtrip.py` empty).
- [ ] `uv run pytest tests/` green; the mutation floor
      (`uv run python scripts/mutation_ratchet.py --mode floor`) not regressed.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Tests run as `uv run pytest tests/` on Python 3.14 via `uv`; the system Python
  cannot import the test stack.
- Existing config/options-flow tests live in `tests/test_config_flow.py`;
  `tests/conftest.py` holds the shared fixtures — reuse the existing hub-entry
  and coordinator fixtures rather than building new ones.
- CI enforces a mutation floor via `scripts/mutation_ratchet.py`, so guard
  branches and each fold branch need direct assertions, not incidental coverage.

## Input Dependencies

- Task 1: `_device_identity` and the `serial_number` on `DeviceInfo`.
- Task 2: `async_replace_device` / `DeviceReplaceError`.
- Task 3: the `replace` / `replace_target` options-flow steps.

## Output Artifacts

- Tests for the helper (new `tests/test_device_replace.py` or an addition to an
  existing module) and for the flow (in `tests/test_config_flow.py`).

## Implementation Notes

<details>
<summary>Test philosophy — apply this while writing</summary>

**Write a few tests, mostly integration.**

Meaningful tests verify custom business logic, critical paths, and edge cases
specific to this application. Test *your* code, not the framework or library.

**When TO write tests:**
- Custom business logic and algorithms.
- Critical user workflows and data transformations.
- Edge cases and error conditions for core functionality.
- Integration points between components.
- Complex validation logic or calculations.

**When NOT to write tests:**
- Third-party library functionality.
- Framework features.
- Simple CRUD operations without custom logic.
- Trivial getters/setters or static configuration.
- Obvious functionality that would break immediately if incorrect.

**Test task creation rules:**
- Combine related test scenarios into a single task (e.g. "Test user
  authentication flow" not separate tasks for login, logout, validation).
- Favor integration and critical-path coverage over per-method unit tests.
- Avoid one test task per CRUD operation.
- Question whether simple functions need a dedicated test task.

Concretely here: do **not** test that Home Assistant's registry stores what you
told it to. Test the ordering, the fold, the guards, and the
`entity_id`-stability property — those are this integration's logic.
</details>

<details>
<summary>Step-by-step implementation</summary>

1. Read `tests/conftest.py` and `tests/test_config_flow.py` first and reuse the
   existing fixtures for a set-up hub entry. Do not hand-roll a config entry if a
   fixture exists.

2. **Set up the two-device state.** Build an entry whose
   `entry.data[CONF_DEVICES]` holds:
   - `Acurite-986-1a2b` — the original, with `DEVICE_FIELDS` `["temperature_C"]`,
     a `DEVICE_TIMEOUT_OVERRIDE`, and a `DEVICE_CALIBRATION`;
   - `Acurite-986-9f3c` — the replacement, with `DEVICE_FIELDS`
     `["temperature_C", "battery_ok"]` (deliberately a superset, so the union is
     observable).

   Set the entry up so both devices' entities exist in the registry.

3. **The central assertion.** Capture before the replace:

   ```python
   before = {
       e.unique_id: (e.entity_id, e.id)
       for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
       if e.unique_id.startswith(f"{entry.entry_id}:Acurite-986-1a2b:")
   }
   ```

   After `async_replace_device(...)`, assert for each survivor that the row with
   the *new* unique_id has the **same `entity_id` and the same `e.id`** as the
   old one. `e.id` is the immutable registry row id: identical `e.id` is what
   proves the row was updated in place rather than recreated, which is the real
   guarantee behind "history is preserved". Asserting only `entity_id` is weaker
   — a recreated row can coincidentally reclaim a freed `entity_id`.

4. **Collision case.** The state in step 2 already has the new device's entities
   present, so the happy-path test covers it. Add an explicit assertion that no
   row with a `…:Acurite-986-9f3c:…` unique_id predating the replace survives
   (compare registry row ids, not just counts).

5. **Fold assertions.** After the replace:
   `entry.data[CONF_DEVICES]` has no `Acurite-986-1a2b`; the `Acurite-986-9f3c`
   record carries the old `timeout_override` and `calibration`; its
   `DEVICE_FIELDS` equals `["battery_ok", "temperature_C"]` (sorted union).

6. **No-record adoption.** Repeat with the replacement absent from the devices
   map entirely and assert the old record transfers wholesale.

7. **Guards.** `pytest.raises(DeviceReplaceError)` for unknown `old_key`,
   `old_key == new_key`, and an empty key. Three small tests or one
   parametrized — parametrize, to keep the mutation ratchet happy without
   duplication.

8. **Flow test.** Drive `hass.config_entries.options.async_init(entry.entry_id)`,
   then `async_configure` through `init` -> `replace` -> `replace_target`,
   asserting the final registry/data state matches the helper tests. Add the
   `no_devices` abort case with an empty devices map.

9. **Serial number.** Assert `entity.device_info["serial_number"] == "00c50f"`
   for `Fineoffset-WH51-00c50f`, and that a model-only device has no
   serial number (`.get("serial_number") is None`). If task 1 already added these
   two assertions, extend rather than duplicate them.

10. Run `uv run pytest tests/`, then
    `uv run python scripts/mutation_stats.py > /tmp/stats.json` and
    `uv run python scripts/mutation_ratchet.py --mode floor --stats /tmp/stats.json`.
    If the floor regresses, add assertions targeting the surviving mutants
    (`uv run mutmut results` / `uv run mutmut show <name>`) rather than lowering
    the floor.
</details>
