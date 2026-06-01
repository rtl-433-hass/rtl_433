---
id: 4
group: "tests"
dependencies: [1, 2, 3]
status: "completed"
created: "2026-06-01"
skills:
  - python
  - pytest
---
# Tests for MHz conversion, Store migration, add flows, and frequency seeding

## Objective
Lock in the custom behaviour introduced by this plan with a small, integration-leaning test set: the SDR Store Hz→MHz migration, the Center-frequency MHz↔Hz round-trip, both add flows persisting the new fields, and the coordinator's one-shot frequency seeding. Update any existing tests that asserted Hz center-frequency values.

## Skills Required
- `python`, `pytest` — `pytest-homeassistant-custom-component` config-flow and coordinator test patterns.

## Acceptance Criteria
- [ ] A test feeds a synthetic version-1 SDR Store payload (`{"values": {"center_frequency": 433920000, ...}, "managed": [...]}`) and asserts that after the coordinator loads desired state, `get_desired("center_frequency") == 433.92`; a separate case asserts an already-MHz value is unchanged and a payload without `center_frequency` is unaffected.
- [ ] A test asserts the Center-frequency setting's `read({"center_frequency": 915_000_000}) == 915.0` and `to_command(915.0) == 915_000_000` (int) for representative bands (433.92, 868.3, 915).
- [ ] A test drives `async_step_user` with `manage_settings=True, discovery_enabled=False, initial_frequency=868.3` and asserts the created entry's `data` has `discovery_enabled is False` and `initial_frequency == 868.3`; a second case with `manage_settings=False` asserts `initial_frequency` is absent.
- [ ] A test drives `async_step_hassio` → `async_step_hassio_confirm` submitting the three fields and asserts they are persisted; a case forces `CannotConnect` and asserts the form re-shows with `errors["base"] == "cannot_connect"`.
- [ ] A test starts a coordinator with `initial_center_frequency=915.0` and an empty Store against a mocked server `meta`, and asserts that after first connect `get_desired("center_frequency") == 915.0` and the enforced command sent `val=915000000`; a follow-up with pre-populated desired state asserts the seed is NOT re-applied.
- [ ] Any pre-existing test that asserted a Hz center-frequency value (number/sensor/coordinator) is updated to MHz. The full suite passes under Python 3.14 via `uv`.

## Technical Requirements
- Files: `tests/test_config_flow.py` (extend), and the relevant coordinator/store/sensor/number test modules (extend or add).
- Run with the project's Python 3.14 stack via `uv` (system Python is 3.13 and lacks the test deps).

## Input Dependencies
- Tasks 1, 2, 3 (all implementation).

## Output Artifacts
- New/updated tests covering the plan's custom logic.

## Implementation Notes
<details>
<summary>Detailed implementation guidance + Meaningful Test Strategy</summary>

**Meaningful Test Strategy ("write a few tests, mostly integration"):**
- Test YOUR logic: the unit conversion, the Store migration, the flow persistence rules (especially "drop frequency when manage off"), and the one-shot seeding gate. Do NOT test HA framework internals, `voluptuous` itself, or `Store` plumbing beyond the migration result.
- Prefer driving the real config-flow via `hass.config_entries.flow.async_init`/`async_configure` over asserting schema internals.

**Store migration test:** construct the coordinator's `_SdrStore` (or the coordinator) for a fake entry, pre-seed the on-disk store via the `hass_storage` fixture with `{"version": 1, "data": {"values": {"center_frequency": 433920000}, "managed": ["center_frequency"]}}`, then `await coordinator.async_load_desired_state()` and assert the in-memory desired value is `433.92`. Look at existing SDR-store tests for the storage-key fixture pattern (`sdr_store_key(entry_id)`).

**Round-trip test:** import the Center-frequency `SdrSetting` via `SDR_SETTINGS_BY_KEY[KEY_CENTER_FREQUENCY]` and call its `.read(...)` / `.to_command(...)` directly.

**Flow tests:** follow the existing `tests/test_config_flow.py` patterns for `async_step_user` and the hassio discovery flow (there is existing coverage of `hassio_confirm` connectivity — extend it rather than duplicating). For the discovery flow, build a `HassioServiceInfo` like the existing tests do. Mock `Rtl433Coordinator.validate_connection` to succeed (and to raise `CannotConnect` for the error case).

**Seeding test:** reuse the existing coordinator test harness that mocks the WebSocket connect + `_refresh_meta`. If that harness is heavy, a focused alternative is to call the first-connect seeding logic by setting `coordinator.initial_center_frequency = 915.0`, `coordinator._desired = {}`, `coordinator.manage_settings = True`, stub `_adopt_from_server` and `_send_cmd`/`_refresh_meta`, and assert the desired map + that `_command_args(KEY_CENTER_FREQUENCY)` yields `("center_frequency", 915000000, None)`. Match whichever style the existing coordinator tests use.

**Updating existing tests:** grep the test tree for `center_frequency` and Hz literals (e.g. `433920000`, `6_000_000_000`) and update expectations to MHz where they assert the desired value, the number entity value/unit, or the sensor unit.
</details>
