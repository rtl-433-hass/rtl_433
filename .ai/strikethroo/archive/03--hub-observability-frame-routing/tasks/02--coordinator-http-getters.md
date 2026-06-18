---
id: 2
group: "coordinator"
dependencies: [1]
status: "completed"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# Coordinator HTTP getters (/cmd) on connect + periodic stats refresh

## Objective
Source the hub's SDR/meta configuration and server statistics deterministically
via one-shot HTTP GETs to the rtl_433 server's `/cmd` endpoint at
`scheme://host:port/cmd` (https when `secure`/wss), using the shared Home
Assistant aiohttp session — never over the streaming WebSocket. On each
successful WebSocket (re)connect the coordinator fetches `get_meta`, `get_gain`,
and `get_ppm_error` into `coordinator.meta`, and `get_stats` into
`coordinator.stats`; `get_stats` is additionally re-fetched on a fixed interval
while connected so throughput stays live. Each successful getter updates hub
state and emits the hub-update signal. Getter failures (including a proxy that
hides `/cmd`) are caught, logged at debug, and leave prior values intact without
disturbing the event stream or the connect loop.

## Skills Required
- `python` — async/aiohttp, defensive JSON parsing, timers.
- `home-assistant` — `async_get_clientsession`, `async_track_time_interval`, coordinator lifecycle.

## Acceptance Criteria
- [ ] A `_build_cmd_url(host, port, *, secure)` helper returns `http(s)://host:port/cmd` built from host/port **only** — never the configured WS `path`.
- [ ] On successful connect (inside `_connect_loop`, after `connected=True`), the coordinator fetches `get_meta` + `get_gain` + `get_ppm_error` and assembles `coordinator.meta`, and fetches `get_stats` into `coordinator.stats`; each populated getter triggers `_emit_hub_update()`.
- [ ] `coordinator.meta` carries the meta object's scalars (`center_frequency`, `samp_rate`, `conversion_mode`, `frequencies`, `hop_times`, plus a derived hop interval = `hop_times[0]` when present), the gain string from `get_gain` (empty string preserved), and the integer ppm from `get_ppm_error`. Missing keys are simply absent.
- [ ] `get_stats` is re-fetched on a fixed module-constant interval **only while connected**; the timer is started in `async_start` and cancelled in `async_stop`.
- [ ] Any getter HTTP/parse failure is caught, logged at `LOGGER.debug`, leaves the previous `meta`/`stats` values intact, and never raises into the connect loop or the watchdog.
- [ ] New tests assert: the `/cmd` URL resolves to `http(s)://host:port/cmd` regardless of the configured WS path; mocked `/cmd` responses for `get_meta`/`get_gain`/`get_ppm_error`/`get_stats` populate `coordinator.meta`/`coordinator.stats` per the documented shapes (including `gain=""`); and a getter failure leaves prior values intact and does not flip `connected`.
- [ ] `uv run pytest tests/` passes; `uv run ruff check custom_components/rtl_433` is clean.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `custom_components/rtl_433/coordinator/base.py`; tests in `tests/test_coordinator.py` (or a new `tests/test_getters.py`).
- HTTP: shared session via `async_get_clientsession(self.hass)` (already imported). Use a short per-request timeout.
- `/cmd` semantics: one command per request, returned as the response body. Issue each getter as a separate GET with the command as a query parameter (see Data Contracts).

## Input Dependencies
- Task 1: `coordinator.meta` / `coordinator.stats` state and `_emit_hub_update()`; the connect-loop structure that sets `connected`.

## Output Artifacts
- Populated `coordinator.meta` / `coordinator.stats` (consumed by Task 5's diagnostic sensors).
- `_build_cmd_url` and the getter methods (referenced by docs in Task 6).

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Data Contracts (verified against rtl_433 C source — parse defensively)
- **`get_meta`** returns an object: `center_frequency` (int), `samp_rate` (int), `conversion_mode` (int), `frequencies` (int[]), `hop_times` (int[]), `duration` (int), `stats_interval` (int), plus `report_*` flags. **No gain, no ppm.**
- **`get_gain`** returns the gain string (e.g. `"32.8"`; empty string ⇒ auto). May be returned as a bare JSON string or wrapped — read defensively.
- **`get_ppm_error`** returns an integer.
- **`get_stats`** returns `{"enabled": int, "since": "YYYY-MM-DDTHH:MM:SS", "frames": {"count": <ook>, "fsk": <fsk>, "events": <total>}, "stats": [ {per-protocol}... ]}`.

### How `/cmd` requests are shaped
The rtl_433 `/cmd` endpoint executes one command per request. Issue a GET with
the command name and (empty) argument, e.g. the documented form is a query
string `?<cmd>` or `?cmd=<cmd>`. Check `WEBSOCKET_API.md` "Request format" and
"Related endpoints (same command set)" for the exact parameter name used by this
server, and match it. Build requests like:

```python
url = self._build_cmd_url(self.host, self.port, secure=self.secure)
async with session.get(url, params={"<param>": "get_meta"}, timeout=...) as resp:
    payload = await resp.json(content_type=None)
```

Use `content_type=None` on `resp.json()` because rtl_433 may not set a strict
`application/json` content-type for scalar getters.

### `_build_cmd_url`
Mirror `_build_ws_url` but for HTTP and always at the server root:

```python
def _build_cmd_url(host: str, port: int, *, secure: bool = False) -> str:
    """Build the ``http(s)://host:port/cmd`` URL (server root, never the WS path)."""
    scheme = "https" if secure else "http"
    return f"{scheme}://{host}:{port}/cmd"
```

Place it as a module-level function next to `_build_ws_url`.

### Getter methods
Add a small async helper that performs one getter and returns the parsed JSON or
`None` on any failure:

```python
_GETTER_TIMEOUT = 10.0  # seconds, per getter request

async def _fetch_cmd(self, command: str) -> Any | None:
    """GET one ``/cmd`` getter; return parsed JSON or None on any failure."""
    session = async_get_clientsession(self.hass)
    url = _build_cmd_url(self.host, self.port, secure=self.secure)
    try:
        async with session.get(
            url, params={"<param>": command}, timeout=_GETTER_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)
    except Exception as err:  # noqa: BLE001 - getters must never kill the loop
        LOGGER.debug("rtl_433 getter %s failed at %s: %s", command, url, err)
        return None
```

Then orchestration methods:

```python
async def _refresh_meta(self) -> None:
    """Fetch get_meta + get_gain + get_ppm_error into ``self.meta``."""
    meta = await self._fetch_cmd("get_meta")
    gain = await self._fetch_cmd("get_gain")
    ppm = await self._fetch_cmd("get_ppm_error")
    new_meta: dict[str, Any] = {}
    if isinstance(meta, dict):
        for key in ("center_frequency", "samp_rate", "conversion_mode",
                    "frequencies", "hop_times"):
            if key in meta:
                new_meta[key] = meta[key]
        hop_times = meta.get("hop_times")
        if isinstance(hop_times, list) and hop_times:
            new_meta["hop_interval"] = hop_times[0]
    if isinstance(gain, str):
        new_meta["gain"] = gain
    if isinstance(ppm, int):
        new_meta["ppm_error"] = ppm
    if new_meta:
        self.meta = {**self.meta, **new_meta}
        self._emit_hub_update()

async def _refresh_stats(self) -> None:
    """Fetch get_stats into ``self.stats``."""
    stats = await self._fetch_cmd("get_stats")
    if isinstance(stats, dict):
        self.stats = stats
        self._emit_hub_update()
```

(Adjust the `gain` unwrapping if the server wraps the scalar — read defensively.)

### Hook into the connect loop
In `_connect_loop`, after `self.connected = True` and `self._emit_hub_update()`
(from Task 1) and before `await self._read_frames(ws)`, seed the getters:

```python
await self._refresh_meta()
await self._refresh_stats()
```

These are sequential, off the read path, and each failure is swallowed by
`_fetch_cmd`, so they cannot break the connection.

### Periodic stats refresh
Add a module constant and a timer started in `async_start` / cancelled in
`async_stop`, mirroring the existing watchdog wiring:

```python
_STATS_REFRESH_INTERVAL = timedelta(seconds=60)
```

In `async_start`, after the watchdog `async_track_time_interval` registration:

```python
self._stats_unsub = async_track_time_interval(
    self.hass, self._async_stats_tick, _STATS_REFRESH_INTERVAL,
    name=f"rtl_433 stats {self.entry.entry_id}",
)
```

Add `self._stats_unsub: Callable[[], None] | None = None` to `__init__`, and a
tick that only refreshes while connected:

```python
async def _async_stats_tick(self, _now: datetime) -> None:
    """Re-fetch get_stats on the interval, only while connected."""
    if self.connected:
        await self._refresh_stats()
```

In `async_stop`, cancel it alongside `_watchdog_unsub`:

```python
if self._stats_unsub is not None:
    self._stats_unsub()
    self._stats_unsub = None
```

### Tests
Use the `aioclient_mock` fixture from `pytest_homeassistant_custom_component`
(available as a `hass`-side fixture) to stub `/cmd` responses. Drive the getter
methods directly (e.g. `await coordinator._refresh_meta()`) rather than opening a
socket. Suggested cases:

- `test_cmd_url_ignores_ws_path`: build a coordinator with `path="/some/proxy/ws"`, assert `_build_cmd_url(host, port, secure=...)` returns `http://host:port/cmd` (and `https://...` when `secure=True`).
- `test_refresh_meta_populates_state`: mock `get_meta`/`get_gain`/`get_ppm_error`; assert `coordinator.meta` has `center_frequency`, `samp_rate`, `conversion_mode`, `frequencies`, `hop_times`, `hop_interval == hop_times[0]`, `gain`, `ppm_error`; assert the hub-update signal fired.
- `test_refresh_meta_empty_gain_preserved`: `get_gain` returns `""` → `coordinator.meta["gain"] == ""` (Task 5 renders this as "auto").
- `test_refresh_stats_populates_state`: mock `get_stats`; assert `coordinator.stats["frames"]["events"]` etc. present.
- `test_getter_failure_leaves_values_intact`: pre-seed `coordinator.meta = {"gain": "32.8"}`; mock `/cmd` to 500/raise; call `_refresh_meta()`; assert `coordinator.meta == {"gain": "32.8"}` and `coordinator.connected` unchanged.

If `aioclient_mock` keys requests by URL only (not query), and all getters share
the same `/cmd` URL, register a `side_effect`/callback that returns different
bodies per the `params`/command. Consult the fixture's API in the installed
`pytest_homeassistant_custom_component` version.

### Gotchas
- Never build the `/cmd` URL from `self.path`. Graceful degradation depends on this being the server root.
- `secure` maps wss⇒https, ws⇒http.
- Do not let a getter exception escape `_fetch_cmd`.
</details>
