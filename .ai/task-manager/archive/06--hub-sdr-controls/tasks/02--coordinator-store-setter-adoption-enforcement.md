---
id: 2
group: "coordinator"
dependencies: [1]
status: "completed"
created: 2026-05-27
skills:
  - python
  - home-assistant
complexity_score: 5
complexity_notes: "Coordinator-layer state machine: Store load/persist, write path, adoption guards, full reconnect enforcement, and a serialization lock. Single file (coordinator/base.py) and one skill domain, so kept as one task; bounded by safe-default API so the system stays runnable at the phase boundary."
---
# Coordinator: desired-state Store, `set_sdr`, adoption, reconnect enforcement

## Objective
Extend `coordinator/base.py` so the coordinator becomes the single, restart-surviving
source of truth for managed SDR settings. Add a `Store`-backed desired-state map, a
`set_sdr(field, value)` write path, first-connect **adoption** from Plan 3's getters
(with the hop-mode and `/cmd`-unreachable guards), **full enforcement** (replay of all
managed fields on every reconnect), and a single serialization lock around all `/cmd`
issuance. Expose a small, stable public API (with safe defaults) so the entity
platforms (Task 4), the integration wiring (Task 3), and the sensor suppression
(Task 5) can consume it without the system ever being un-runnable between phases.

## Skills Required
- `python` — async/await, `asyncio.Lock`, defensive error handling.
- `home-assistant` — `homeassistant.helpers.storage.Store`, the shared aiohttp session,
  and the existing coordinator connect-loop lifecycle.

## Acceptance Criteria
- [ ] Coordinator constructor accepts `manage_settings: bool = True` and stores it as
      `self.manage_settings`. (Default `True` keeps every existing construction site,
      including tests, working before Task 3 wires the real value.)
- [ ] A desired-state map (`self._desired: dict[str, Any]`) and a managed-set
      (`self._managed: set[str]`) are loaded from a `homeassistant.helpers.storage.Store`
      keyed by `sdr_store_key(entry_id)` at startup via an awaitable
      `async_load_desired_state()` called from `async_start()` **before** the connect
      loop is created. When `manage_settings` is `False`, loading **clears** the Store
      (so a later re-enable re-adopts).
- [ ] `set_sdr(field: str, value: Any)` updates `self._desired[field]`, marks the field
      managed, persists the Store, and — if connected — issues the field's `/cmd`
      command (under the lock) and reads it back via the getter to reconcile. A `/cmd`
      failure is caught, logged at debug, and **leaves the desired value intact**.
- [ ] On every successful connect, after `_refresh_meta()`/`_refresh_stats()` and only
      when `manage_settings` is `True`: if the desired map is empty, run **adoption**;
      then run **enforcement** (replay all managed fields).
- [ ] **Adoption** seeds `self._desired`/`self._managed` from `self.meta`
      (center_frequency, samp_rate, ppm_error, gain, conversion_mode, hop_interval) and
      persists the Store. Guards: (a) if `len(self.meta.get("frequencies", [])) > 1`
      (hop mode), **skip** center_frequency (leave it unmanaged); (b) if the getters
      produced nothing (`self.meta` is empty / `/cmd` unreachable), **adopt nothing**,
      leave the Store empty, and surface it via the existing graceful-degradation path
      (the reachability repair already fires; do not raise).
- [ ] **Enforcement** issues each managed field's setter command in sequence through
      the shared lock; a getter/command failure is logged at debug and leaves prior
      desired values intact without disturbing the connect loop or the event stream.
- [ ] All `/cmd` issuance (enforcement replay, write-path send, read-back) is serialized
      through one `asyncio.Lock` so a user write and a reconnect replay cannot
      interleave requests to the same server.
- [ ] Public read API for entities: a way to read the current desired value and managed
      status of a field (e.g. `get_desired(field)` / `is_managed(field)`), plus
      `clear_desired_state()` used when management is turned off.
- [ ] `uv run pytest tests/test_coordinator.py` and `uv run ruff check
      custom_components/rtl_433` pass. (Adding the new API must not break the existing
      coordinator tests.)

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Reuse the existing `_build_cmd_url` + `async_get_clientsession` + `_GETTER_TIMEOUT`
  plumbing for the new `_send_cmd`. The streaming WebSocket must **never** be used to
  send a command (commands go only over `/cmd`).
- Reuse the `sdr_settings` registry from Task 1 for the field list, command names,
  `arg`/`val` kind, `to_command` transforms, and the gain-arg composition.
- Adoption/enforcement run **off the connect path** that already calls
  `_refresh_meta()`/`_refresh_stats()` inside `_connect_loop` (right after
  `self.connected = True`). Wrap the new calls so an exception there can never kill the
  loop (the loop already has a broad `except`, but the new helpers should also swallow
  their own `/cmd` errors like `_fetch_cmd` does).

## Input Dependencies
- Task 1: `sdr_settings.py` (registry, `gain_command_arg`, conversion mappers) and
  `const.py`'s `sdr_store_key` / `SDR_STORE_VERSION`.

## Output Artifacts
- Extended `coordinator/base.py` with the public API consumed by Tasks 3–5:
  - `Rtl433Coordinator(..., manage_settings: bool = True)` and `self.manage_settings`.
  - `async def async_load_desired_state(self) -> None`
  - `async def set_sdr(self, field: str, value: Any) -> None`
  - `def get_desired(self, field: str) -> Any` and `def is_managed(self, field) -> bool`
  - `async def clear_desired_state(self) -> None` (also wipes the Store)

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

**Store load.** Construct the Store lazily in `__init__` or in `async_load_desired_state`:
```python
from homeassistant.helpers.storage import Store
from ..const import SDR_STORE_VERSION, sdr_store_key

self._store: Store[dict[str, Any]] = Store(
    hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id)
)
self._desired: dict[str, Any] = {}
self._managed: set[str] = set()
self._cmd_lock = asyncio.Lock()
```
```python
async def async_load_desired_state(self) -> None:
    if not self.manage_settings:
        await self._store.async_remove()      # re-enable will re-adopt
        self._desired, self._managed = {}, set()
        return
    data = await self._store.async_load() or {}
    self._desired = dict(data.get("values", {}))
    self._managed = set(data.get("managed", []))
```
Call it at the top of `async_start()` (await it before creating the background task).

**Persist helper:**
```python
async def _persist_desired(self) -> None:
    await self._store.async_save(
        {"values": self._desired, "managed": sorted(self._managed)}
    )
```

**Send one command** (serialized):
```python
async def _send_cmd(self, command: str, *, val: int | None = None, arg: str | None = None) -> bool:
    session = async_get_clientsession(self.hass)
    url = _build_cmd_url(self.host, self.port, secure=self.secure)
    params = {"cmd": command}
    if val is not None: params["val"] = str(int(val))
    if arg is not None: params["arg"] = arg
    async with self._cmd_lock:
        try:
            async with session.get(url, params=params, timeout=_GETTER_TIMEOUT) as resp:
                resp.raise_for_status()
            return True
        except Exception as err:  # noqa: BLE001 - never kill the loop
            LOGGER.debug("rtl_433 /cmd %s failed at %s: %s", command, url, err)
            return False
```
Note `gain` with the auto sentinel must send `arg=""` (an *empty string*, not omitted),
so always include `arg` for the gain command.

**Build (command, val/arg) for a field.** Iterate `sdr_settings`; for the gain pair use
`gain_command_arg(self._desired.get(GAIN_DB_KEY), self._desired.get(GAIN_AUTO_KEY, False))`
and emit a single `gain` command. For the others use the entry's `arg_kind` +
`to_command(self._desired[field])`.

**set_sdr:**
```python
async def set_sdr(self, field: str, value: Any) -> None:
    self._desired[field] = value
    self._managed.add(field)
    # gain is two keys but one managed concept; mark both when either is set
    await self._persist_desired()
    if self.connected:
        await self._enforce_field(field)   # send + read-back (best effort)
```
`_enforce_field` sends the mapped command and then calls `_refresh_meta()` (or the
field's specific getter) to reconcile `self.meta`, swallowing failures.

**Adoption:**
```python
async def _adopt_from_server(self) -> None:
    meta = self.meta
    if not meta:                      # /cmd unreachable -> adopt nothing
        return
    hopping = len(meta.get("frequencies", []) or []) > 1
    for setting in SDR_SETTINGS:
        if setting.key == "center_frequency" and hopping:
            continue
        current = setting.read(meta)
        if current is None:
            continue
        self._desired[setting.key] = current
        self._managed.add(setting.key)
    await self._persist_desired()
```
(Adapt for the gain pair: seed `gain_db` from the numeric part and `gain_auto` from
`gain == ""`.)

**Connect-loop hook** — inside `_connect_loop`, after the existing
`await self._refresh_meta(); await self._refresh_stats()`:
```python
if self.manage_settings:
    if not self._desired:
        await self._adopt_from_server()
    await self._enforce_all()
```
`_enforce_all` loops the managed fields and calls `_send_cmd` for each (gain once).
Everything best-effort; never raise into the loop.

**clear_desired_state:** clears `_desired`/`_managed` and `await self._store.async_remove()`.

**Tests already exist** in `tests/test_coordinator.py`; do not regress them. The
existing `coordinator` fixture constructs `Rtl433Coordinator(...)` without
`manage_settings`, which is why it must default to `True`. The existing `_connect_loop`
is exercised indirectly; the new adoption/enforcement only run inside the real loop
(stubbed in lifecycle tests), so unit tests for them are added in Task 6.

Run `uv run pytest tests/test_coordinator.py` and
`uv run ruff check custom_components/rtl_433` before finishing.
</details>
