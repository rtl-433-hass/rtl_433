---
id: 4
group: "transport"
dependencies: [2, 3]
status: "completed"
created: 2026-07-04
skills:
  - home-assistant
  - async-python
complexity_score: 9
complexity_notes: "817-line base.py plus three mixins; the embedded WS/HTTP transport is deleted and replaced by a coordinator that owns pyrtl_433.Rtl433Client with an injected HA session and callbacks wired into the dispatcher. Highest-risk task: reconnect/availability/refresh behavior parity."
---
# Re-architect Rtl433Coordinator to wrap pyrtl_433.Rtl433Client

## Objective
Delete the transport embedded in `Rtl433Coordinator`/`coordinator/base.py` and its mixins, and
have the coordinator instead *own* and drive a `pyrtl_433.Rtl433Client` — injecting Home
Assistant's shared aiohttp session and wiring the client's callbacks into the existing
dispatcher-based entity-update path. All HA-facing behavior (availability, reconnect, refresh
cadence, SDR settings management, connection validation) is preserved.

## Skills Required
- **home-assistant**: `DataUpdateCoordinator`, dispatcher signals, `async_get_clientsession`,
  config-entry lifecycle, availability/watchdog patterns.
- **async-python**: aiohttp session ownership, asyncio task/callback wiring, start/stop lifecycle.

## Acceptance Criteria
- [ ] The coordinator constructs `pyrtl_433.Rtl433Client(host, port=..., path=..., secure=..., session=async_get_clientsession(hass), on_event=..., on_hub_update=..., skip_keys=..., clock=...)` and no longer contains an embedded WS connect loop, frame reader, or `/cmd` GET/SET plumbing.
- [ ] Client `on_event` feeds the existing `_process_event`→`_dispatch` chain; `on_hub_update` drives `_emit_hub_update`; dispatcher signals (`signal_hub_update`/`signal_device_update`) are emitted exactly as before.
- [ ] `async_start`/`async_stop` delegate to the client's `start()`/`stop()`; the injected HA session is **not** owned/closed by the client (HA owns it).
- [ ] Meta/stats/dev_info refresh uses the client's `refresh_meta`/`refresh_stats`/`refresh_dev_info`; SDR `/cmd` setting uses the client's `_send_cmd`; SDR settings management (`_sdr.py` mixin) works against the client + Task 3 adapter.
- [ ] `validate_connection` delegates to `Rtl433Client.validate_connection(session, host, port, path, secure=...)`; `CannotConnect` is imported from `pyrtl_433`; `config_flow.py`, `repairs.py`, and `diagnostics.py` import sites updated.
- [ ] Deleted: `_build_ws_url`, `_build_cmd_url`, `_unwrap_result`, `_read_frames`, `_connect_loop`, and any now-dead local transport helpers; the stale `coordinator/__pycache__/_http.cpython-314.pyc` is removed and nothing imports `coordinator._http`.
- [ ] The integration loads and a config entry sets up without error (unit-level; full behavior parity is verified in Task 7).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- `pyrtl_433.Rtl433Client.__init__(host, *, port=8433, path="/ws", secure=False,
  session=None, skip_keys=None, on_event=None, on_hub_update=None, clock=None)`. Passing a
  session means the client will **not** close it — correct for HA's shared session.
- Public client API: `start()`, `stop()`, `refresh_meta/stats/dev_info()`, `ws_url` property,
  `validate_connection(session, host, port, path, *, secure=False)` staticmethod, `_send_cmd(command, *, val=None, arg=None)`, and read-only state `connected/meta/stats/dev_info/dev_query`. Events are delivered via `on_event(NormalizedEvent)`; hub state changes via `on_hub_update()`. The async-iterator API is **not** used (dispatcher push is retained).
- The `/cmd` setter is the underscore `_send_cmd` (no public alias in 0.1.0) — call it directly and note the wart.
- HA-specific logic that must stay in the integration: the availability/watchdog mixin
  (`_watchdog.py`, `_AvailabilityMixin`), the SDR settings mixin (`_sdr.py`, `_SdrSettingsMixin`,
  `_SdrStore`), device registration/forget, and dispatcher fan-out. These now observe the
  client's connection state/callbacks instead of the deleted internal loop.
- Construction site: `__init__.py:183`. Import sites of `CannotConnect`/`Rtl433Coordinator`:
  `__init__.py:56`, `config_flow.py:58`, `repairs.py:52`, `diagnostics.py:23`.

## Input Dependencies
- Task 2: normalizer + replay sourced from `pyrtl_433` (the client emits `pyrtl_433`'s `NormalizedEvent`).
- Task 3: SDR adapter (coordinator SDR settings management uses it).

## Output Artifacts
- A coordinator that is a thin HA adapter over `pyrtl_433.Rtl433Client`; the integration's
  transport source-of-truth is now the library.
- The functional target that Task 5 (tests) and Task 7 (end-to-end parity) validate against.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

1. **Map old→new** before editing. Old internals → client methods:
   `_connect_loop`/`_read_frames`→`client.start()`; `async_stop`→`client.stop()`;
   `_fetch_cmd`+`_unwrap_result`→`client.refresh_*`/state props; `_send_cmd`→`client._send_cmd`;
   `_build_ws_url`/`_build_cmd_url`→gone (client builds URLs internally); `validate_connection`→
   `Rtl433Client.validate_connection(session, ...)`.
2. **Construct the client** in the coordinator (in `__init__` or `async_start`) with
   `session=async_get_clientsession(self.hass)`. Wire callbacks:
   - `on_event=self._on_client_event` where `_on_client_event(evt)` calls the existing
     `_process_event(evt)` / classification / `_dispatch(...)` path. The client already
     normalizes + replay-classifies events, so avoid double-classifying — reconcile with the
     `_EventProcessingMixin` from Task 2 (the mixin may now only do HA-side mapping/dispatch,
     since `NormalizedEvent.is_replay`/`event_time` arrive pre-computed). Verify replay/backlog
     semantics stay identical.
   - `on_hub_update=self._emit_hub_update`.
   - `clock=` the coordinator's time source if it uses one (for deterministic tests), else omit.
3. **Lifecycle**: `async_start` → adopt SDR state (existing `_sdr.py` logic) then `await
   client.start()`. `async_stop` → `await client.stop()` then existing teardown. Do not close
   the HA session.
4. **Availability/watchdog**: rewire `_AvailabilityMixin` to observe the client's `connected`
   state / `on_hub_update` callback rather than the deleted frame loop. Preserve
   `DEFAULT_AVAILABILITY_TIMEOUT` and watchdog interval behavior.
5. **SDR settings**: `_SdrSettingsMixin` uses the Task 3 adapter + `client._send_cmd` and
   `client.meta` to read/write settings; `_SdrStore` persistence is HA-side and stays.
6. **Refresh tick**: `_async_refresh_tick` calls `client.refresh_meta/stats/dev_info` on the
   existing cadence.
7. **Errors**: `from pyrtl_433 import CannotConnect`; update `config_flow.py`, `repairs.py`,
   `diagnostics.py`, and remove the local `CannotConnect` class.
8. **Cleanup**: delete dead helpers, remove the stale `_http` pyc, and grep for
   `coordinator._http`, `_build_ws_url`, `_unwrap_result` to confirm no references remain.
9. Do a unit-level config-entry setup to confirm the integration loads; leave full behavior
   parity (reconnect, unavailable/recover, `/cmd` round-trip) to Task 7.
</details>
