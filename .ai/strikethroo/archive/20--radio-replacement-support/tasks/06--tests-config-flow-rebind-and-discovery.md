---
id: 6
group: "tests"
dependencies: [1, 2]
status: "completed"
created: "2026-06-04"
skills:
  - pytest
---
# Tests: reconfigure rebind + discovery orphan reconciliation

## Objective
Cover the config-flow rebind behavior end to end: reconfigure rebinds a
discovered/adopted entry preserving `entry_id` + devices; collisions abort or
adopt-and-delete correctly; discovery offers the replace choice; legacy `hub:`
reconfigure is unchanged.

## Skills Required
- `pytest` — Home Assistant config-flow tests (`MockConfigEntry`, flow helpers).

## Acceptance Criteria
- [ ] Test: reconfigure a discovered/adopted entry with a new `radio_id` rebinds its `unique_id`, preserves `entry_id` and `entry.data["devices"]`, and updates host/port.
- [ ] Test: rebind whose target id is owned by a **populated** entry aborts `already_configured` and changes nothing; rebind whose target id is owned by an **empty orphan** deletes the orphan and succeeds.
- [ ] Test: `async_step_hassio` with an unknown radio id while a hub exists routes to `hassio_replace`; choosing the hub rebinds it; choosing `__new__` creates a new entry.
- [ ] Test: legacy `hub:` reconfigure still rebinds via host:port (unchanged behavior).
- [ ] At least one mutation/property test in `test_mut_config_flow.py` asserts the rebind does not change any entity `unique_id` / the `device_key` scheme.
- [ ] `uv run pytest tests/test_config_flow.py tests/test_mut_config_flow.py -q` passes.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `tests/test_config_flow.py`, `tests/test_mut_config_flow.py`.
- Follow existing fixtures/patterns in those files (`MockConfigEntry`, `hass.config_entries.flow`, the reconfigure-flow context helper already used there).
- Run via `uv run pytest` (Python 3.14 stack; system Python 3.13 cannot import the test deps).

## Input Dependencies
- Task 1 (reconfigure rebind + helper), Task 2 (discovery replace step).

## Output Artifacts
- New tests.

## Meaningful Test Strategy Guidelines
Your mantra: "write a few tests, mostly integration." Test YOUR logic (the rebind
helper's collision/orphan branches, the reconfigure rebind path, the discovery
routing decision), not Home Assistant's flow machinery. Combine related scenarios
into single tests where natural (e.g. one test driving the full reconfigure rebind
and asserting entry_id + devices + unique_id together). Do not add tests for
trivial schema plumbing or framework behavior.

## Implementation Notes
<details>
<summary>Guidance</summary>

- Read `tests/test_config_flow.py` first to reuse its helpers: how it builds a
  `MockConfigEntry` for a discovered/adopted hub (unique_id is a stable radio id,
  not `hub:...`), how it seeds `entry.data["devices"]`, and how it starts a
  reconfigure flow (`entry.start_reconfigure_flow(hass)` or the context dict with
  `"source": SOURCE_RECONFIGURE` + `"entry_id"`).
- **Rebind happy path:** create an adopted entry with `unique_id="radio-old"` and
  a non-empty `devices` map; run reconfigure submitting `radio_id="radio-new"` +
  host/port; assert `result["type"] == ABORT`, `reason == "reconfigure_successful"`,
  the entry's `unique_id == "radio-new"`, `entry_id` unchanged, devices preserved.
- **Populated collision:** add a second entry `unique_id="radio-new"` with a
  non-empty devices map; reconfigure the first to `radio-new`; assert abort
  `already_configured` and that neither entry changed.
- **Empty-orphan adopt:** second entry `unique_id="radio-new"` with empty/absent
  devices; reconfigure the first to `radio-new`; assert success and that the
  orphan entry was removed (`hass.config_entries.async_get_entry(orphan_id) is None`).
- **Discovery replace:** seed one adopted hub; fire `async_step_hassio` with a
  `HassioServiceInfo` whose `config["unique_id"]` is unknown and host:port differ;
  assert the flow shows `step_id == "hassio_replace"`; submit `{"replaces": <hub
  entry_id>}` and assert the hub rebinds; in a second run submit
  `{"replaces": "__new__"}` and assert a new entry is created.
- **Legacy unchanged:** an entry with `unique_id="hub:1.2.3.4:8433"`; reconfigure
  to a new host:port; assert it rebinds to `hub:<newhost>:<newport>` as today.
- **Mutation/property (`test_mut_config_flow.py`):** after a rebind, assert the
  nested device registry entries / entity unique_ids are byte-identical to
  pre-rebind (the additive-only guarantee). Reuse any existing registry-snapshot
  helper in that file.
</details>
