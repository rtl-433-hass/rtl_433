---
id: 3
group: "client"
dependencies: [1, 2]
status: "completed"
created: 2026-07-04
skills:
  - asyncio
  - aiohttp
complexity_score: 8
complexity_notes: "The core deliverable: reproduce base.py's WebSocket+HTTP transport as a standalone async client, decoupling three HA seams (session, dispatcher, dt_util) without changing protocol behavior."
---
# Implement the decoupled Rtl433Client transport

## Objective
Reproduce the WebSocket + HTTP `/cmd` transport of
`custom_components/rtl_433/coordinator/base.py` as a standalone, Home-Assistant-free
async client, `pyrtl_433.Rtl433Client`. Same protocol behavior; the three HA
couplings become an injected aiohttp session, an event callback / async-iterator,
and stdlib time.

## Skills Required
- **asyncio**: connection lifecycle as tasks, backoff loop, `asyncio.Lock`,
  cancellation/shutdown, an async-iterator/queue event interface.
- **aiohttp**: `ClientSession.ws_connect`, `WSMsgType` handling, `session.get`
  with query params, timeouts, `heartbeat`.

## Acceptance Criteria
- [ ] `pyrtl_433/client.py` defines `Rtl433Client` and `CannotConnect` (a plain `Exception`/`RuntimeError` subclass — **not** `HomeAssistantError`).
- [ ] The constructor accepts connection params (`host`, `port`, `path`, `secure`) and an **injected `aiohttp.ClientSession`** (with an option to create/own one), plus a consumer hook: an event callback and/or an `async for event in client` async-iterator that yields fully normalized, replay-classified `NormalizedEvent`s.
- [ ] `start()`/`stop()` manage the connect loop as an asyncio task (replacing `entry.async_create_background_task`), and `stop()` cleanly closes the socket/owned session and cancels tasks.
- [ ] Migrated transport methods reproduce the source: connect with `ws_connect(url, heartbeat=30)`; capped exponential backoff `1.0 → 60.0` on drop; frame read-loop routing TEXT frames and breaking on CLOSE/ERROR; `json.loads` with empty/malformed/non-dict frames dropped; event vs `{"shutdown": ...}` classification; `/cmd` GET getter (`_fetch_cmd`) and setter (`_send_cmd`, `val` as int / `arg` verbatim, empty = gain-auto) under an `asyncio.Lock`; `unwrap_result` envelope handling; `refresh_meta`/`refresh_stats`/`refresh_dev_info` assembly exposing `meta`/`stats`/`dev_info`/`dev_query` snapshots; `validate_connection` reachability probe raising `CannotConnect`.
- [ ] All time comes from stdlib `datetime.now(UTC)` (injectable clock for tests) — no `homeassistant.util.dt`.
- [ ] `grep -rn "homeassistant" pyrtl_433/client.py` returns nothing; `uv run python -c "from pyrtl_433 import Rtl433Client"` imports with no Home Assistant installed.
- [ ] `pyrtl_433/__init__.py` exports `Rtl433Client`, `CannotConnect`, and `NormalizedEvent`.

## Technical Requirements
- Source of truth (read-only): `custom_components/rtl_433/coordinator/base.py`
  methods `_connect_loop`, `_read_frames`, `_handle_text_frame`,
  `_classify_frame`, `_handle_shutdown`, `_fetch_cmd`, `_send_cmd`,
  `_refresh_meta`, `_refresh_stats`, `_refresh_dev_info`, `validate_connection`,
  and constants `_BACKOFF_MIN/_MAX`, `_GETTER_TIMEOUT`, `_VALIDATE_TIMEOUT`,
  `_REFRESH_INTERVAL`.
- Decoupling seams:
  - `async_get_clientsession(self.hass)` → injected `aiohttp.ClientSession`.
  - `async_dispatcher_send(...)` / `async_track_time_interval` → event
    callback/async-queue emission + an internal periodic refresh task.
  - `dt_util.utcnow()`/parse → stdlib `datetime` (+ `pyrtl_433.replay.parse_event_time`).
- Depends on `pyrtl_433.normalizer.normalize`, `pyrtl_433.replay.classify_replay`,
  `pyrtl_433.sdr` command transforms, `pyrtl_433._urls`.

## Input Dependencies
- Task 1: package scaffold. Task 2: `normalizer`, `replay`, `sdr`, `_urls` modules.

## Output Artifacts
- `pyrtl_433/client.py` (`Rtl433Client`, `CannotConnect`) and updated
  `pyrtl_433/__init__.py`. Consumed by task 4 (tests) and task 6 (baseline).

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

Model `Rtl433Client` on `Rtl433Coordinator` but keep only the transport/protocol
concerns; **do not** port the SDR desired-state `Store`/adoption/enforcement
policy, the availability watchdog, device-registration/discovery callbacks, or the
dispatcher fan-out (those are HA policy, out of scope). The client exposes the
SDR-command *primitives* (`_send_cmd` + the `pyrtl_433.sdr` transforms) so a
consumer can build a policy on top.

Suggested shape:
```python
class CannotConnect(RuntimeError): ...

class Rtl433Client:
    def __init__(self, host, port=8433, path="/ws", *, secure=False,
                 session: aiohttp.ClientSession, clock=lambda: datetime.now(UTC),
                 on_event=None):
        # store params; self._session = session; self._cmd_lock = asyncio.Lock()
        # self._queue = asyncio.Queue() for the async-iterator interface
        # self.meta = {}; self.stats = {}; self.dev_info = {}; self.dev_query = None
        # self._event_high_water = None; self._connection_time = None

    async def start(self): ...          # spawn the connect loop task
    async def stop(self): ...           # cancel tasks, close owned session
    def __aiter__(self): ...            # yield NormalizedEvents from the queue
    async def _connect_loop(self): ...  # ws_connect + seed + read + backoff
    async def _read_frames(self, ws): ...
    def _handle_text_frame(self, data: str): ...   # json.loads + route
    def _classify_frame(self, event: dict): ...
    async def _fetch_cmd(self, command): ...
    async def _send_cmd(self, command, *, val=None, arg=None) -> bool: ...
    async def refresh_meta(self): ...
    async def refresh_stats(self): ...
    async def refresh_dev_info(self): ...
    @staticmethod
    async def validate_connection(session, host, port, path, *, secure=False) -> bool: ...
```

Event emission: where the source calls `self._dispatch(...)` /
`async_dispatcher_send`, instead build the `NormalizedEvent` (via
`normalize` + `dataclasses.replace(..., is_replay=verdict.is_replay,
event_time=...)`), then push to `self._queue` and/or invoke `self._on_event`.
Preserve the replay high-water-mark advance from `classify_replay`'s verdict.

Backoff: replicate `await asyncio.wait_for(self._stop_event.wait(),
timeout=backoff)` with `backoff = min(backoff * 2, _BACKOFF_MAX)` starting at
`_BACKOFF_MIN`, reset to min on a successful connect.

`_send_cmd`/`_fetch_cmd`: `self._session.get(build_cmd_url(...),
params={"cmd": command, ...}, timeout=aiohttp.ClientTimeout(total=10))`, then
`await resp.json(content_type=None)`, unwrap with `unwrap_result`. Keep the
malformed-JSON error-dedup logging behavior. Serialize `_send_cmd` under
`self._cmd_lock`.

`validate_connection`: short-lived `ws_connect(url,
timeout=aiohttp.ClientTimeout(total=10))`; on failure raise `CannotConnect`.

Keep behavior byte-faithful to the source wherever it is protocol logic; only the
three seams change. Verify no `homeassistant` import and a clean import with HA
absent. Tests are task 4.
</details>
