---
id: 3
group: "tests"
dependencies: [2]
status: "pending"
created: 2026-09-01
skills:
  - pytest
  - websocket-api
---
# Test the WebSocket API and panel registration

## Objective

Cover the new API end to end through a real WebSocket client, prove the
subscription pushes on change without flooding, and prove the panel and its
static path register exactly once.

## Skills Required

- `pytest` with `pytest-homeassistant-custom-component` and `hass_ws_client`.
- `websocket-api` — subscription lifecycle and error responses.

## Acceptance Criteria

- [ ] `rtl_433/devices/pending` returns the heard-but-not-added devices with key,
      model, count, signal level, ISO timestamps, and latest values.
- [ ] `rtl_433/devices/add` creates exactly the requested devices and reports
      applied vs skipped; a key that is not pending is reported skipped, not an
      error.
- [ ] `rtl_433/devices/ignore` and `.../unignore` round-trip through
      `entry.data[CONF_IGNORED_DEVICES]` and the live coordinator.
- [ ] `rtl_433/hubs` lists the loaded hubs.
- [ ] Every command rejects a non-admin user.
- [ ] An unknown `entry_id` and a not-loaded entry each return a websocket error
      rather than raising.
- [ ] The subscription sends the current list on subscribe, pushes when a new
      candidate appears, and pushes after an add or an ignore.
- [ ] **Feeding many frames for one already-known candidate does not produce one
      message per frame** — this is the flooding guard and the most important
      test in the task.
- [ ] Unsubscribing stops the pushes.
- [ ] Adopting via the WebSocket API produces the same device and entities as
      adopting via the options flow.
- [ ] The panel and its static path register exactly once when two hub entries
      are set up.
- [ ] `uv run pytest tests/` exits 0; ruff check and format clean.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- New `tests/test_websocket_api.py`; register it in
  `scripts/mutation_targets.py` (`EXPLICIT_TEST_SOURCES`) or
  `tests/test_mutation_targets.py::test_no_test_file_silently_escalates` fails.
- Use the `hass_ws_client` fixture; for admin gating, see how core tests build a
  non-admin client (`hass_admin_user.is_admin = False` before connecting).
- Build pending state by feeding real frames through the production seam, as
  `tests/test_pending_devices.py` does — never by assigning `coordinator.pending`.

## Input Dependencies

- Tasks 1 and 2.

## Output Artifacts

- Regression coverage for the API and the panel registration.

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

Applied here: drive the commands through `hass_ws_client` as a client would, and
resist writing a test per command per error code. Admin gating and bad-`entry_id`
handling are each one parametrised test across the commands, not five each.

### The flooding guard

The single most valuable test in this task, because it guards a failure that only
appears under the load of a real urban receiver and would never surface in
ordinary use:

```python
# Subscribe, let the first candidate register, drain, then feed 20 more frames
# for that SAME key and assert the client did not receive 20 messages.
```

Count messages actually delivered to the client, and assert a small bound rather
than an exact number — the point is "not one per frame", and pinning an exact
count would make the test brittle against a legitimate change to the throttle.
Advance time with `freezegun` if the throttle is time-based, and say in the
docstring what regression this guards.

### Adoption equivalence

Mirror the check that already exists for the options flow in
`tests/test_pending_devices.py`: adopt one device over the WebSocket, adopt an
equivalent device through the options flow, and assert both produce the same
device metadata and entity set. Three surfaces (live sighting, options flow,
WebSocket) now reach one registration path, and this is what keeps them honest.

### Panel registration

Set up two hub entries and assert the panel exists once — `hass.data` frontend
panels keyed by `rtl_433` — and that setting up the second entry neither raises
nor re-registers. Check how the frontend stores panels in the installed package
before asserting on internals.

</details>
