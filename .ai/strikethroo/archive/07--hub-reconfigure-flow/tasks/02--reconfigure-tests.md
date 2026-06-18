---
id: 2
group: "reconfigure-flow"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - python
  - pytest
---
# Tests for the reconfigure flow

## Objective
Add regression tests to `tests/test_config_flow.py` that lock in the reconfigure flow's behavior: happy path (in-place update + reload + device preservation), `cannot_connect` rejection, unique-id reconciliation, collision abort, and single-reload.

## Skills Required
- `python` + `pytest` — using `pytest-homeassistant-custom-component` and the existing test fixtures.

## Acceptance Criteria
- [ ] **Happy path**: starting a reconfigure flow on a seeded hub entry and submitting changed-and-reachable host/port/path aborts with `reconfigure_successful`; `entry.data` reflects the new connection params; `entry.entry_id` and a seeded `entry.data["devices"]` are unchanged.
- [ ] **cannot_connect**: with `validate_connection` patched to raise `CannotConnect`, the result is a form with `errors == {"base": "cannot_connect"}` and `entry.data` is unchanged.
- [ ] **Unique-id reconciliation**: after a host/port change, the entry's `unique_id == f"hub:{newhost}:{newport}"`.
- [ ] **Collision guard**: with two hub entries configured, reconfiguring one onto the other's host/port aborts with `already_configured` and mutates neither entry.
- [ ] **Single reload**: a successful reconfigure reloads the entry exactly once (no double teardown via the update listener).
- [ ] All new tests pass: `python -m pytest tests/test_config_flow.py -q`.

## Technical Requirements
- File: `tests/test_config_flow.py`.
- Patch `validate_connection` via the existing `VALIDATE` constant (`tests/test_config_flow.py:33`).
- Build entries via the `hub_entry_builder` fixture (`tests/conftest.py:55-104`) which yields a v2 `MockConfigEntry` with `unique_id=f"hub:{host}:{port}"`.
- Start reconfigure flows with `hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id})` (import `SOURCE_RECONFIGURE` from `homeassistant.config_entries`), or the `entry.start_reconfigure_flow(hass)` helper if available in the pinned HA.

## Input Dependencies
- Task 1: the reconfigure step and translations must exist.

## Output Artifacts
- New passing test cases in `tests/test_config_flow.py`.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

- Mirror the style of the existing user/options tests in `tests/test_config_flow.py`. Reuse the `VALIDATE` patch target and `hub_entry_builder`.
- For the happy path, seed `entry.data["devices"]` with at least one device record (copy the shape the builder/other tests use) and assert it is byte-for-byte unchanged after reconfigure, plus `entry.entry_id` unchanged — this is the device-preservation proof.
- For collision: add two entries via `hub_entry_builder` with different host/port (so different unique_ids), then reconfigure entry A submitting entry B's host/port; assert `result["type"] == "abort"` and `result["reason"] == "already_configured"`, and that both entries' `data` are unchanged.
- For single-reload: spy on `hass.config_entries.async_reload` (or `async_schedule_reload`) with a mock/counter, or assert the coordinator object in `hass.data[DOMAIN][entry.entry_id]` is reconstructed exactly once. Pick whichever is reliable in the pinned HA.
- `await hass.async_block_till_done()` after submitting so the scheduled reload settles before assertions that depend on it.

### Meaningful Test Strategy Guidelines
Your critical mantra for test generation is: "write a few tests, mostly integration".
- **Test YOUR code**, not HA framework internals. Focus on the custom reconfigure logic: data merge/preservation, unique-id reconciliation, collision guard, cannot_connect, single reload.
- Combine related scenarios where natural; do not write a separate test per assertion when one flow exercise covers several.
- Do not test `voluptuous`/HA form rendering itself.
</details>
