---
id: 1
group: "device-registration"
dependencies: []
status: "completed"
created: 2026-06-01
skills:
  - python
---
# Post-connection device registration gate (Issue 1)

## Objective
Stop the coordinator from auto-registering devices that the rtl_433 server replays from its pre-connection backlog. Add a per-connection timestamp and only fire the new-device callback for a previously-unknown device once a message timestamped at/after the connection (within a small skew grace) is seen. Backlog events must still seed runtime state so a device that later transmits live still registers.

## Skills Required
- `python` (Home Assistant coordinator / asyncio)

## Acceptance Criteria
- [ ] The coordinator records the UTC time of the current successful WebSocket connection and clears it on disconnect.
- [ ] A previously-unknown device whose triggering event timestamp is before `connection_time - grace` does NOT fire `new_device_callback` (not registered/persisted), but its runtime state (`self.devices`, `seen_fields`, per-device field set) is still seeded.
- [ ] A previously-unknown device first seen in the backlog still fires `new_device_callback` on its first post-connection event (the backlog appearance does not permanently consume its "new" status).
- [ ] Events with no usable timestamp are treated as post-connection (still register), preserving the existing "never drop a real one" stance.
- [ ] Devices already present in `entry.data[CONF_DEVICES]` are unaffected.
- [ ] A named grace constant is added near `REPLAY_STALE_THRESHOLD` with a comment documenting the server/HA clock-sync assumption.
- [ ] A coordinator test feeds `_process_event` a backlog event (timestamp before connection time) then a live event (timestamp after) and asserts the new-device callback fires exactly once, only for the post-connection event.
- [ ] Full suite passes via `uv run pytest tests/ -q`.

## Technical Requirements
- File: `custom_components/rtl_433/coordinator/base.py`.
- Use the already-imported `dt_util.utcnow()` and existing `_parse_event_time`.
- Tests: `tests/` (extend the coordinator/registration tests; reuse existing fixtures).

## Input Dependencies
None.

## Output Artifacts
- `_connection_time` attribute on the coordinator (consumed conceptually by later work but self-contained here).
- The registration gate in `_process_event`.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

**1. Connection timestamp primitive**
- In `_connect_loop` (`coordinator/base.py`), immediately after `self.connected = True` (~line 407), set `self._connection_time = dt_util.utcnow()`.
- In the loop's `finally:` block where `self.connected = False` is set (~line 449), set `self._connection_time = None`.
- Initialize `self._connection_time: datetime | None = None` in `__init__` alongside the other runtime-state attributes (near where `self.connected`/`self._desired` are initialized, ~lines 280-310). `datetime` is already imported.

**2. Grace constant**
- Near `REPLAY_STALE_THRESHOLD = timedelta(seconds=30)` (~line 117), add a constant such as `DISCOVERY_BACKLOG_GRACE = timedelta(seconds=5)` with a comment: registration treats events older than `connection_time - DISCOVERY_BACKLOG_GRACE` as pre-connection backlog and does not auto-register them; this assumes the rtl_433 server and Home Assistant clocks are roughly in sync.

**3. Registration gate in `_process_event`** (~line 834-906)
- The current registration trigger is `is_new = key not in self.devices` followed by `if is_new and self.discovery_enabled and self.new_device_callback is not None:` (~line 900).
- The problem: `self.devices[key] = normalized` is assigned (~line 878) BEFORE the callback, so basing "new" on `self.devices` means a backlog event would consume the new status. Introduce a SEPARATE discovery set, e.g. `self._discovered: set[str]` (init as empty set in `__init__`), tracking device keys already offered to the callback.
- Compute post-connection: `is_post_connection = self._connection_time is None or event_time is None or event_time >= self._connection_time - DISCOVERY_BACKLOG_GRACE`. (`event_time` is already computed at ~line 851.)
- Fire the callback only when `key not in self._discovered and self.discovery_enabled and self.new_device_callback is not None and is_post_connection`. When it fires, add `key` to `self._discovered` before/after calling the callback.
- Keep the existing runtime-state seeding (`self.devices[key] = normalized`, `seen_fields`, `device_fields`, and the liveness update on `not is_replay`) UNCHANGED so backlog devices still seed.
- Note: `self._discovered` is per-process; on restart, already-persisted devices in `entry.data[CONF_DEVICES]` are loaded as entities and the `__init__.py` callback already skips known keys, so a known device re-seen post-restart will simply be re-offered and skipped — harmless. Do not persist `_discovered`.
- Preserve the `is_replay` argument passed to the callback (notification suppression) exactly as today.

**4. Test (write a few tests, mostly integration)**
- In the appropriate coordinator test module (look for existing tests that call `_process_event` / set `new_device_callback`), add a test that:
  - Sets `coordinator._connection_time` to a fixed UTC time, installs a `new_device_callback` mock, enables discovery.
  - Calls `_process_event` with an event whose `time` is ~60s before `_connection_time` (backlog) → asserts callback NOT called, but `coordinator.devices` contains the key.
  - Calls `_process_event` again for the same device with `time` after `_connection_time` → asserts callback called exactly once.
  - Add a case for an event with no `time` field → asserts callback fires (treated as post-connection).
- Times in rtl_433 events use the format `_parse_event_time` accepts (local naive `"YYYY-MM-DD HH:MM:SS"` interpreted in HA tz, or ISO-8601 with offset/Z). Match the format existing tests use.

**Validation**
- Run `uv run pytest tests/ -q` (system Python is 3.13; the test stack needs 3.14 via `uv` — see project memory). Do not validate syntax with python3.13 (PEP 758 `except A, B:` is used in this codebase and is valid on 3.14).
</details>
