---
id: 3
group: "event-trace-instrumentation"
dependencies: [1, 2]
status: "completed"
created: 2026-06-15
skills:
  - pytest
---
# Tests for the event-trace DEBUG logging

## Objective
Lock the new observable logging behavior with a few focused, mostly-integration tests so the trace contract cannot silently regress and the touched modules stay at/above their mutation baselines.

## Skills Required
- `pytest` — `caplog`, the project's `pytest-homeassistant-custom-component` harness and existing coordinator/event fixtures.

## Acceptance Criteria
- [ ] A test feeds a **live** doorbell/event frame through `coordinator._handle_text_frame(...)` and asserts the captured DEBUG records contain one ingestion line with the `device_key` and a `LIVE` verdict.
- [ ] A test feeds a **replay** (at/below high-water) and a **backlog** (pre-connection) frame and asserts the corresponding `REPLAY` / `BACKLOG` verdict lines appear and that the frame does **not** fire an HA event.
- [ ] A test exercises the `event` entity: a live press logs the `fired ... event_type=` line; the watchdog re-dispatch logs the dedupe line; and a suppressed replay still logs the existing INFO line.
- [ ] Tests capture at DEBUG against the package logger via `caplog.set_level(logging.DEBUG, logger="custom_components.rtl_433")`.
- [ ] The full suite passes under the project's Python 3.14 / `uv` setup, and `coordinator/base.py` + `event.py` remain at/above their mutation-score baselines.

## Meaningful Test Strategy Guidelines
Mantra: "write a few tests, mostly integration".
- **Test YOUR logic**: the classification verdict emitted per branch and the entity's fire/dedupe decision — not aiohttp, HA core, or the logging framework.
- Combine related scenarios into single tests where natural (e.g. all four verdicts driven through one coordinator).
- Do not add tests for trivial getters, framework behavior, or pre-existing logged lines beyond confirming they are unchanged.

## Technical Requirements
- Test files: extend `tests/test_coordinator.py` for the ingestion/classification lines; add or extend an event-entity test (no dedicated event test file exists yet — create `tests/test_event_trace.py` or extend the closest existing event/entity test) for the firing/dedupe lines.
- Frames are fed as JSON strings via `coordinator._handle_text_frame('{"model": ..., "id": ..., "time": ...}')` (see existing patterns in `tests/test_coordinator.py`).
- Use the existing doorbell fixture `tests/fixtures/doorbell_event.json` where helpful.

## Input Dependencies
- Task 1: the ingestion/classification DEBUG line and its verdict format.
- Task 2: the firing/dedupe DEBUG lines.

## Output Artifacts
- Passing tests asserting the trace contract; mutation coverage for the new branches/strings.

## Implementation Notes
<details>

- To force each verdict in the coordinator: drive `_handle_text_frame` with frames whose `time` field is set relative to the coordinator's `_connection_time` / `_event_high_water`. Existing coordinator tests already construct replay/backlog scenarios — mirror their setup (search `_event_high_water`, `_connection_time`, `is_backlog`, `REPLAY_STALE_THRESHOLD` in `tests/test_coordinator.py` and `tests/test_mut_coordinator_base*.py`).
- Assert on `caplog.records` / `caplog.text`: check for substrings like the device key and `-> LIVE`, `-> REPLAY`, `-> BACKLOG`. Keep assertions on stable tokens (the verdict label), not on volatile parts (exact timestamp).
- For the event entity, reuse whatever harness existing entity tests use to instantiate `Rtl433Event` and invoke `_handle_dispatch(normalized)` directly with a crafted `NormalizedEvent` (live, replay, and the same-object watchdog re-paint).
- Run via the project's documented command, e.g. `uv run pytest tests/test_coordinator.py tests/test_event_trace.py -q` (system Python is 3.13; the stack needs 3.14 via `uv`).
- After tests pass, run the mutation check for the two modules to confirm baselines hold; if a new string/branch introduces a surviving mutant, tighten an assertion to kill it.
</details>
