---
id: 7
group: "tests"
dependencies: [3]
status: "completed"
created: "2026-06-04"
skills:
  - pytest
---
# Tests: unreachable-repair rebind fix flow

## Objective
Cover the reframed `server_unreachable` repair: its fix flow rebinds the hub to a
new radio id and clears the issue.

## Skills Required
- `pytest` — Home Assistant repairs/issue-registry tests.

## Acceptance Criteria
- [ ] Test: `async_create_fix_flow` returns the custom rebind flow for a `server_unreachable_{entry_id}` issue and a plain confirm flow for the sample-rate issue.
- [ ] Test: driving the fix flow with a new `radio_id` + connection details rebinds the hub (`unique_id` updated, `entry_id` + devices preserved) and deletes the unreachable issue from the issue registry.
- [ ] Test: a `cannot_connect` validation re-shows the form (no rebind).
- [ ] `uv run pytest tests/test_diagnostics_repairs.py -q` passes.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `tests/test_diagnostics_repairs.py`.
- Patch `Rtl433Coordinator.validate_connection` to succeed/raise `CannotConnect` as needed (follow how existing repairs/coordinator tests stub connectivity).
- Run via `uv run pytest` (Python 3.14 stack).

## Input Dependencies
- Task 3 (`HubRadioReplaceRepairFlow` + `async_create_fix_flow` routing).

## Output Artifacts
- New tests.

## Meaningful Test Strategy Guidelines
Your mantra: "write a few tests, mostly integration." Test the rebind-from-repair
path (the real value) and the routing decision, not `ConfirmRepairFlow` internals
or the issue-registry framework. One integration-style test that creates the
unreachable issue, drives the fix flow to completion, and asserts both the rebind
and the cleared issue is worth more than many small unit tests.

## Implementation Notes
<details>
<summary>Guidance</summary>

- Read `tests/test_diagnostics_repairs.py` for existing patterns: how it sets up a
  hub `MockConfigEntry`, raises `async_raise_hub_unreachable`, and inspects the
  issue registry (`homeassistant.helpers.issue_registry.async_get`).
- **Routing test:** call `await async_create_fix_flow(hass,
  f"server_unreachable_{entry.entry_id}", None)` and assert it is a
  `HubRadioReplaceRepairFlow`; call it with a `sample_rate_low_for_band_{...}` id
  and assert `ConfirmRepairFlow`.
- **Rebind test:** raise the unreachable issue for an adopted hub
  (`unique_id="radio-old"`, non-empty devices). Obtain the fix flow, drive
  `async_step_init` then submit the confirm form with `radio_id="radio-new"` +
  host/port (patch `validate_connection` to succeed). Assert: flow creates entry
  (completes), `entry.unique_id == "radio-new"`, `entry_id` + devices preserved,
  and the issue is gone from the registry.
- **cannot_connect:** patch `validate_connection` to raise `CannotConnect`; submit;
  assert the form is re-shown with the `cannot_connect` error and no rebind
  happened.
- To drive a `RepairsFlow` directly you can instantiate `HubRadioReplaceRepairFlow(entry)`,
  set `flow.hass = hass`, and `await flow.async_step_confirm(user_input)`; or use
  the repairs websocket/flow helpers if the existing tests already do.
</details>
