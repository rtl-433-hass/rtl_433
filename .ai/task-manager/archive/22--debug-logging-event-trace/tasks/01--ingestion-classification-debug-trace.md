---
id: 1
group: "event-trace-instrumentation"
dependencies: []
status: "completed"
created: 2026-06-15
skills:
  - python
---
# Add ingestion + classification DEBUG trace in `_process_event`

## Objective
Emit one compact DEBUG log line for every device event frame received on the rtl_433 websocket, including the four-way classification verdict (`LIVE` / `REPLAY` / `STALE-GAP` / `BACKLOG`) and a short reason. The line must be produced for **all** event frames reaching `_process_event` — including devices that are not registered, are disabled, or have discovery turned off.

## Skills Required
- `python` — Home Assistant custom-component logging conventions, editing the coordinator.

## Acceptance Criteria
- [ ] A new `LOGGER.debug(...)` call in `coordinator/base.py::_process_event` logs, for every event frame: the `device_key`, the normalized fields, the raw event timestamp, and the classification verdict + reason.
- [ ] The verdict label and reason are derived from the **same** branch that sets `is_replay` / `_event_high_water` (no re-derivation that could drift from the real decision).
- [ ] Verdict labels used: `LIVE`, `REPLAY`, `STALE-GAP`, `BACKLOG`; with reason tokens such as `no-timestamp`, `event_time>high_water`, `event_time<=high_water`, `age>threshold`, `pre-connection`.
- [ ] The log call uses lazy `%`-style args (no f-strings forcing work when DEBUG is off) and cannot raise.
- [ ] No existing log line, level, or behavior is changed. The shared `LOGGER` from `const.py` is reused (no new logger).
- [ ] The new line is emitted regardless of device registration / `discovery_enabled` / entity enabled-state.

## Technical Requirements
- File: `custom_components/rtl_433/coordinator/base.py`, method `_process_event` (around line 996).
- `LOGGER` is already imported in this module from `.const` (verify; reuse it).
- The classification branch (around lines 1027–1052) sets `is_replay` and conditionally advances `self._event_high_water`. Add a local `verdict` string in each arm.
- `normalized` is a `NormalizedEvent` with `.device_key`, `.fields`, `.model`; `event_time` is the parsed raw timestamp (may be `None`).

## Input Dependencies
None.

## Output Artifacts
- A DEBUG ingestion/classification trace line whose exact textual contract (the `RX`/verdict format) is consumed by Task 3 (tests) and described by Task 4 (docs).

## Implementation Notes
<details>

The classification branch currently looks like:

```python
if event_time is None:
    is_replay = False                     # -> LIVE (no-timestamp)
elif self._event_high_water is not None and event_time <= self._event_high_water:
    is_replay = True                      # -> REPLAY (event_time<=high_water)
elif now - event_time > REPLAY_STALE_THRESHOLD:
    is_replay = True
    self._event_high_water = event_time   # -> STALE-GAP (age>threshold)
elif is_backlog:
    is_replay = True
    self._event_high_water = event_time   # -> BACKLOG (pre-connection)
else:
    is_replay = False
    self._event_high_water = event_time   # -> LIVE (event_time>high_water)
```

Add a local `verdict` assignment in each arm (e.g. `verdict = "LIVE (no-timestamp)"`, `"REPLAY (event_time<=high_water)"`, `"STALE-GAP (age>threshold)"`, `"BACKLOG (pre-connection)"`, `"LIVE (event_time>high_water)"`). This keeps the logged reason identical to the decision actually taken.

After the branch resolves (the `normalized = dataclasses.replace(...)` line is a good anchor — log just after it so the stamped `is_replay`/`event_time` are final), emit one line. Suggested format (match the style already in the file — `rtl_433` prefix, `%`-args):

```python
LOGGER.debug(
    "rtl_433 RX %s fields=%s time=%s -> %s",
    key,
    normalized.fields,
    event_time.isoformat() if event_time is not None else "none",
    verdict,
)
```

Notes:
- `key` is `normalized.device_key` and encodes model + id/channel, so a separate model arg is redundant; keep it compact.
- Do NOT log the full raw frame here (decision: compact summary only; the raw frame is already logged on parse failure in `_handle_text_frame`).
- Place the call so it runs for every event frame (before/around `_dispatch`), upstream of the registration gate, so unregistered/disabled/discovery-off devices are still logged.
- Keep all existing DEBUG lines (`back online`, `new_device_callback failed`, watchdog `went unavailable`) untouched.
- Run a quick lint/format pass consistent with the repo (the project uses ruff/standard formatting — match surrounding style).
</details>
