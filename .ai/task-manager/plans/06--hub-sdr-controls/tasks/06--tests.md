---
id: 6
group: "tests"
dependencies: [2, 3, 4, 5]
status: "completed"
created: 2026-05-27
skills:
  - pytest
  - home-assistant
complexity_score: 5
complexity_notes: "Covers the write path, adoption (3 variants), reconnect replay, Store persistence, toggle on/off entity gating + reload, and failure isolation. Combined into one test task per 'a few tests, mostly integration'; bounded by the existing harness patterns."
---
# Tests for Hub SDR Controls

## Objective
Add focused, mostly-integration tests proving Plan 6's behavior: control writes emit the
correct `/cmd` command/argument and persist to the `Store`; adoption seeds desired state
on first connect (normal, hop-mode skip of center frequency, and `/cmd`-down fallback to
unmanaged); reconnect replays all managed fields; desired state survives a reload from
the `Store`; the management toggle gates the control entities and suppresses/keeps the
Plan 3 sensors; an options change to the toggle reloads while a `timeout`/`discovery`
change does not; and a `/cmd` failure (write or enforcement) leaves the desired value
intact without disturbing the event stream. Serialized `/cmd` issuance is asserted.

## Skills Required
- `pytest` — `pytest-homeassistant-custom-component`, `aioclient_mock`, `freezegun`,
  `MockConfigEntry`, dispatcher patching.
- `home-assistant` — entity/registry assertions and config-entry reload semantics.

## Meaningful Test Strategy Guidelines
Your critical mantra for test generation is: "write a few tests, mostly integration".

**Definition of "Meaningful Tests":** Tests that verify custom business logic, critical
paths, and edge cases specific to the application. Focus on testing YOUR code, not the
framework.

**When TO Write Tests:** custom business logic; critical workflows and data
transformations; edge/error conditions for core functionality; integration points
between components; complex validation/calculation.

**When NOT to Write Tests:** third-party/framework functionality; simple CRUD without
custom logic; getters/setters; configuration/static data; obvious code that would break
immediately if wrong.

**Test Task Rules:** combine related scenarios into single tests; prefer integration and
critical-path over unit coverage; do not test each CRUD op separately; question whether
trivial functions need dedicated tests.

## Acceptance Criteria
- [ ] **Write path / command mapping:** with a mocked `/cmd`, drive a write to each
      control via the coordinator/entity and assert the exact outbound request:
      `center_frequency` (`val` Hz), `sample_rate` (`val` Hz), `ppm_error` (`val`),
      `convert` (mapped int for each label), `hop_interval` (`val` s), and `gain`
      (`arg` = dB string when Auto off; **empty** `arg` when Auto on). Assert each write
      is recorded in the coordinator's desired state / `Store`.
- [ ] **Adoption:** seed a coordinator with an empty Store + mocked getters; drive the
      connect path and assert desired state is populated and fields marked managed.
      Repeat with `meta.frequencies` length > 1 and assert center frequency is left
      unmanaged. Repeat with the getters failing (`/cmd` down) and assert nothing is
      adopted, no commands are issued, and no error escapes.
- [ ] **Reconnect enforcement:** with a non-empty desired Store, simulate two successive
      connects and assert every managed field's setter command is replayed on each.
- [ ] **Store persistence:** persist desired state, recreate/reload the coordinator, and
      assert the desired state is restored from the `Store` (not re-adopted).
- [ ] **Toggle gating + suppression (integration):** set up a hub with
      `manage_settings=True` and assert the `number`/`select`/`switch` control entities
      exist with `EntityCategory.CONFIG`, the five folded Plan 3 sensors are **absent**,
      and the center-frequency actual sensor **remains**. Set up with
      `manage_settings=False` and assert **no** control entities exist, **all six** Plan
      3 SDR sensors are present, and no `/cmd` commands are issued.
- [ ] **Reload-on-toggle:** assert that changing `CONF_MANAGE_SETTINGS` in options
      triggers `async_reload`, whereas changing only `availability_timeout` /
      `discovery_enabled` does **not** reload (applied live).
- [ ] **Failure isolation + serialization:** inject a `/cmd` failure during a write and
      during enforcement; assert the desired value is retained and a normal device event
      fed to the coordinator is still processed (event stream undisturbed). Assert all
      `/cmd` issuance goes through the single lock (e.g. the lock exists and is taken;
      or a write + enforcement cannot interleave).
- [ ] `uv run pytest tests/` passes in full;
      `uv run ruff check custom_components/rtl_433` reports no new violations.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Follow the existing harness: `tests/conftest.py` `build_hub_entry` /
  `hub_entry_builder`, the `_mock_cmd(aioclient_mock, ...)` pattern and `_META_BODY` /
  `_STATS_BODY` shapes in `tests/test_coordinator.py`, the `DISPATCH` patch constant, and
  the lifecycle pattern in `tests/test_lifecycle.py` (autouse `_no_socket` stubbing
  `_connect_loop`, `_feed`, `_setup_hub`). Reuse, don't reinvent.
- `aioclient_mock` matches a registered response when every query component in the matcher
  is present; register a stub per `cmd` (and assert on `aioclient_mock.mock_calls` to read
  back the exact `params` sent, including `val`/`arg`).
- The Store can be exercised via `pytest-homeassistant-custom-component`'s storage mocking
  (`hass_storage`) or by calling the coordinator's load/persist directly; pick whichever
  fits the existing style and keeps tests deterministic.
- Put new tests in a new `tests/test_sdr_controls.py` (and/or extend
  `tests/test_coordinator.py` / `tests/test_lifecycle.py` where an existing fixture makes
  a scenario cheaper). Do not duplicate scenarios across files.

## Input Dependencies
- Tasks 2–5: the coordinator API, the toggle/flow/listener, the control platforms, and the
  sensor suppression must all be in place.

## Output Artifacts
- New/extended test modules under `tests/`, all passing under `uv run pytest tests/`.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

- For command-mapping tests, the most direct route is to construct a coordinator
  (`coordinator` fixture style), set `connected = True`, register `aioclient_mock` GET
  stubs for the setter commands (they return `{"result": "Ok"}`), call
  `await coordinator.set_sdr("center_frequency", 868000000)`, then assert the last
  `aioclient_mock` call carried `params={"cmd": "center_frequency", "val": "868000000"}`.
  For gain: `set_sdr("gain_auto", True)` then assert the `gain` call sent `arg=""`;
  `set_sdr("gain_auto", False)` + `set_sdr("gain", 32.8)` then assert `arg="32.8"`.
- For adoption/enforcement, exercise the real connect path or call the coordinator's
  adoption/enforcement helpers directly after populating `coordinator.meta` (e.g. via
  `_refresh_meta` against `aioclient_mock`, or by setting `coordinator.meta` and calling
  the adopt/enforce helpers). The hop-mode guard uses `meta["frequencies"]` length > 1
  (the `_META_RESULT` fixture already has two frequencies — handy for the hop-mode case;
  use a single-frequency meta for the normal case).
- For entity-presence assertions, use `entity_registry`/`hass.states` after `_setup_hub`
  with `manage_settings` toggled via `hub_entry_builder(... data includes manage_settings)`
  or `options=`. The lifecycle `_no_socket` fixture means adoption/enforcement won't run
  (no real connect), which is fine for pure entity-gating assertions; for adoption
  assertions, drive the relevant coordinator method directly.
- For reload-on-toggle, use `hass.config_entries.async_update_entry(entry, options=...)`
  and `await hass.async_block_till_done()`, patching/spying `async_reload` to assert it is
  (not) called.

Run the full suite (`uv run pytest tests/`) and ruff before finishing.
</details>
