---
id: 5
group: "tests"
dependencies: [3]
status: "completed"
created: 2026-08-31
skills:
  - pytest
  - home-assistant-config-flow
---
# Test the add/ignore options flow and the toggle-stripping migration

## Objective

Cover the user-facing half of the plan: the add / ignore / un-ignore round trip
through the options flow, and the minor-version migration that strips
`discovery_enabled` while leaving every already-adopted device intact.

## Skills Required

- `pytest` with `pytest-homeassistant-custom-component`.
- `home-assistant-config-flow` — driving options flows in tests
  (`hass.config_entries.options.async_init` / `async_configure`) and asserting
  on `entry.data`.

## Acceptance Criteria

- [ ] The options menu offers `add_devices` and `ignored_devices`.
- [ ] With pending devices present, `add_devices` renders one option per pending
      device, most recently seen first, and each label carries the model, key,
      and sighting count.
- [ ] Selecting devices to add creates exactly those devices, writes them into
      `entry.data[CONF_DEVICES]`, and leaves the unselected ones pending.
- [ ] Selecting devices to ignore writes them into
      `entry.data[CONF_IGNORED_DEVICES]`, removes them from `pending`, and
      creates no device.
- [ ] An ignored device stays ignored across a config-entry reload and does not
      re-enter `pending` on a subsequent frame.
- [ ] Selecting the same key to both add and ignore is rejected with the
      conflict error and changes nothing.
- [ ] `add_devices` aborts cleanly when nothing is pending, and
      `ignored_devices` aborts cleanly when nothing is ignored.
- [ ] `ignored_devices` un-ignores a selected key in both `entry.data` and the
      running coordinator.
- [ ] A `MINOR_VERSION` 7 entry carrying `discovery_enabled` in `data` and in
      `options`, plus several adopted devices with per-device overrides and a
      calibration, migrates to 8 with the key gone from both and every device
      record byte-identical.
- [ ] The migration is idempotent: running it against an already-migrated entry
      changes nothing.
- [ ] Every abort reason and error key used by the flow exists in
      `translations/en.json` (guard against a flow that renders a raw key).
- [ ] `uv run pytest tests/` passes and `uv run ruff check`/`format --check` pass.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Extend `tests/test_config_flow.py` (options-flow coverage lives there today)
  and `tests/test_migration_roundtrip.py` (migration coverage lives there).
- Populate `coordinator.pending` for flow tests by feeding real frames through
  the existing coordinator fixtures rather than by assigning the dict directly —
  a flow test that hand-builds pending state would not catch a routing change.

## Input Dependencies

- Task 3: the two options-flow steps and their translation keys.
- Task 2: the `MINOR_VERSION = 8` migration.

## Output Artifacts

- Regression coverage for the approval UI and the upgrade path.

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

Applied here: drive the flow end to end (menu → form → submit → assert on the
resulting devices and `entry.data`) instead of unit-testing the label builder and
the schema separately. One test that walks a user from "three devices heard" to
"two added, one ignored" is worth more than six narrow ones.

### Driving the options flow

Follow the existing pattern in `tests/test_config_flow.py`:

```python
result = await hass.config_entries.options.async_init(entry.entry_id)
result = await hass.config_entries.options.async_configure(
    result["flow_id"], {"next_step_id": "add_devices"}
)
result = await hass.config_entries.options.async_configure(
    result["flow_id"], {"add": ["Acurite-Tower_A_1234"], "ignore": ["Nexus-TH_1_55"]}
)
await hass.async_block_till_done()
```

Read the rendered options out of `result["data_schema"]` to assert on ordering
and label content — that is what pins the "most recently seen first" and
"label carries the sighting count" requirements.

### The migration test

Build the "before" entry to look like a real upgrade, not a minimal one:
`minor_version=7`, `discovery_enabled` present in **both** `data` and `options`,
and `CONF_DEVICES` holding two or three devices with a timeout override, a motion
clear delay, and a calibration between them. After
`hass.config_entries.async_setup(entry.entry_id)`, assert `minor_version == 8`,
the key is absent from both mappings, and `entry.data[CONF_DEVICES]` compares
equal to the original dict. The equality assertion is the important one: it is
what proves the upgrade does not disturb existing users' devices.

### Translation-key guard

A small test that loads `translations/en.json` and asserts every abort reason and
error key the flow can emit is present catches the most common failure mode for
new flow steps — a form that renders `no_pending_devices` as literal text. Check
whether the repo already has such a guard before adding a second one.

</details>
