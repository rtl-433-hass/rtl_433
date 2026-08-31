---
id: 4
group: "tests"
dependencies: [3]
status: "pending"
created: 2026-08-31
skills:
  - pytest
  - home-assistant-integration
---
# Test the observation/adoption routing contract

## Objective

Prove the core behavioural promise of this plan: nothing reaches the Home
Assistant device registry without an explicit user action, the pending list
routes correctly across the adopted / ignored / replay / backlog matrix, and an
adopted device is indistinguishable from one the old auto-add path created.

## Skills Required

- `pytest` with `pytest-homeassistant-custom-component`.
- `home-assistant-integration` — device/entity registry assertions and the
  coordinator's event-feeding test helpers.

## Acceptance Criteria

- [ ] Feeding a live frame for an unknown device creates **no** device-registry
      entry, **no** entities, and **no** persistent notification, and leaves a
      single pending record.
- [ ] A pending device appears in none of `coordinator.devices`,
      `last_seen`, `available`, `seen_fields`, or `device_fields`.
- [ ] Repeated frames for the same pending key bump `count`, refresh `last_seen`,
      and replace the stored event without creating a second record.
- [ ] A replayed frame (`is_replay=True`) and a pre-connection backlog frame each
      create no pending record.
- [ ] A frame for a key in `entry.data[CONF_IGNORED_DEVICES]` creates no pending
      record.
- [ ] A frame for an already-adopted key follows the existing path: runtime state
      updates and the device's entities receive the value.
- [ ] `adopt_device` on a pending key creates the device and the same entity set
      the pre-change auto-add path created, and clears the key from `pending`.
- [ ] `adopt_device` on a key that is not pending returns `None` and creates
      nothing.
- [ ] Deleting an adopted device and then feeding another frame for it puts the
      device back in `pending` rather than recreating it in the registry.
- [ ] The pending list is empty after a config-entry reload.
- [ ] `uv run pytest tests/` passes and `uv run ruff check`/`format --check` pass.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Add coverage in `tests/test_coordinator.py` and `tests/test_lifecycle.py`, or a
  new `tests/test_pending_devices.py` if those modules grow unwieldy — follow
  whichever matches the existing layout best.
- Reuse the existing fixtures in `tests/conftest.py` and the JSON payloads under
  `tests/fixtures/`; do not invent a new event-feeding harness.
- Assert the absence of notifications by checking the
  `persistent_notification` state/registry the way the existing suite does, not
  by mocking the module out.

## Input Dependencies

- Tasks 1–3: the routing, the removal, and the options flow must all be in place
  so the tests describe the final contract rather than an intermediate state.

## Output Artifacts

- Regression coverage for the behaviour both issues #128 and #131 asked for.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Test philosophy: "write a few tests, mostly integration"

**Definition.** Meaningful tests verify custom business logic, critical paths,
and edge cases specific to this application. Test *your* code, not the framework
or library.

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

Applied here: the routing decision is the custom business logic worth testing,
and it is best covered as a small table-driven set of frame-in / state-out cases
plus a couple of full-integration paths (adopt through to entities, delete back
to pending). Do not write a unit test per coordinator attribute.

### Suggested shape

Parametrise the routing matrix rather than writing six near-identical tests:

```python
@pytest.mark.parametrize(
    ("is_replay", "is_backlog", "ignored", "adopted", "expect"),
    [
        (False, False, False, False, "pending"),
        (True,  False, False, False, "dropped"),
        (False, True,  False, False, "dropped"),
        (False, False, True,  False, "dropped"),
        (False, False, False, True,  "adopted"),
    ],
)
```

`"dropped"` asserts the key is absent from `pending`, `devices`, and the device
registry. `"adopted"` asserts the existing runtime-state path ran.

### The adoption equivalence check

The highest-value test in this task: feed a frame, adopt the device, then assert
the resulting device-registry entry (identifiers, name, model, `via_device`) and
the set of entity IDs match what the integration produces for a device seeded
through `entry.data[CONF_DEVICES]` at setup. If those two paths ever diverge,
adopting a device produces a subtly different device than the old auto-add did,
which is exactly the regression this plan risks.

### Notifications

The point of issue #128 is that notifications must stop. Assert that after
feeding several unknown devices, no `persistent_notification` entity/state
exists for the `rtl_433` domain. Check how the current suite asserts on
notifications before writing this — an assertion that passes because it looks in
the wrong place is worse than no assertion.

### Reload

A test that reloads the config entry and asserts `coordinator.pending` is empty
afterwards is what pins the "in-memory, clears on restart" product decision. It
is cheap and it is the only guard against someone later adding persistence.

</details>
