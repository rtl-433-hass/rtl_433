---
id: 6
group: "core"
dependencies: [3]
status: "completed"
created: 2026-05-25
skills:
  - python
  - home-assistant
---
# Event Normalizer & WebSocket Coordinator

## Objective
Implement the ingestion pipeline: (a) an event normalizer that derives a deterministic device key from identity fields and separates measurement fields from skip-fields, and (b) the WebSocket coordinator that owns the connection to one rtl_433 server (connect/reconnect with backoff, parse JSON frames, ignore keep-alives/malformed frames), tracks per-device last-seen timestamps, runs the availability watchdog, and fans events out via Home Assistant's dispatcher keyed by device.

## Skills Required
- `python` — async, aiohttp WebSocket client, resilient parsing
- `home-assistant` — dispatcher, config-entry-scoped runtime data, async lifecycle

## Acceptance Criteria
- [ ] `custom_components/rtl_433/normalizer.py` provides `device_key(event) -> str` (from `model` + present subset of `id`/`channel`/`subtype`) and `normalize(event) -> NormalizedEvent` (device key, identity metadata, dict of measurement field→value with skip-keys removed). Deterministic and stable across messages from the same physical device.
- [ ] Handles missing `id` (channel-only or model-only devices) without crashing; the key is stable for a given identity-field combination.
- [ ] `custom_components/rtl_433/coordinator/__init__.py` (and/or `coordinator/base.py`) defines a coordinator class that: connects to `ws(s)://host:port/path`, parses each JSON frame, ignores empty/keep-alive frames and malformed JSON (logging at debug), and reconnects with exponential backoff on drop.
- [ ] The coordinator maintains: observed device keys, per-device last-seen timestamps, the per-hub discovery-enabled flag, and resolved availability defaults — all scoped to the config entry (so multiple hubs coexist).
- [ ] On each parsed event, the coordinator updates last-seen and dispatches via `async_dispatcher_send` using the `SIGNAL_DEVICE_UPDATE` template from `const.py`.
- [ ] An availability watchdog (periodic, e.g. `async_track_time_interval`) compares last-seen against the effective timeout (per-device override → hub default) and dispatches availability changes; entities consult last-seen too.
- [ ] A `validate_connection(host, port, path)` helper (usable by the config flow in Task 7) attempts a short-lived connection and returns success/failure without side effects.
- [ ] `ruff check` passes; module imports cleanly; no blocking calls on the event loop.
- [ ] A single conventional commit (e.g. `feat: add event normalizer and websocket coordinator`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Use `aiohttp` (already in HA) for the WebSocket client; obtain the session via `homeassistant.helpers.aiohttp_client.async_get_clientsession`.
- Use `homeassistant.helpers.dispatcher.async_dispatcher_send`.
- Use `homeassistant.helpers.event.async_track_time_interval` for the watchdog; store the unsub in the coordinator and clean it up on unload.
- Reconnect/backoff must not spin hot; cap backoff (e.g. 1s → 60s). Cancel cleanly on shutdown.
- Skip-keys must come from the loader (Task 5) at runtime, OR the normalizer can take a skip-key predicate injected by the caller to avoid a hard import cycle. Prefer: normalizer accepts `skip_keys: set[str]` so it stays decoupled from the loader; the coordinator wires the loader's skip-keys in.
- Tolerate keep-alive/empty frames and malformed JSON without killing the coordinator loop (Risk: WebSocket variance).

## Input Dependencies
- Task 3: `const.py` (signal template, conf keys, defaults), package skeleton.
- (Soft) Task 5's skip-keys: to keep Phase 2 parallel/file-disjoint, do NOT import `mapping.py` here. Accept `skip_keys` as a parameter/attribute; the integration wiring (Task 9) injects the loaded skip-keys. The normalizer may fall back to a hardcoded minimal identity set (`model,id,channel,subtype,time`) as a default if none injected.

## Output Artifacts
- `normalizer.py` and `coordinator/` consumed by config flow (Task 7), entities (Task 8), and `__init__.py` wiring (Task 9); tested in Task 10.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. **normalizer.py**:
   - `IDENTITY_KEYS = ("model", "id", "channel", "subtype")`.
   - `device_key(event)`: build from present identity keys, e.g. `f"{model}"` plus `-{id}` / `-ch{channel}` / `-{subtype}` for whichever are present. Must be filesystem/HA-safe (slugify-ish, but keep deterministic). Stable: same identity → same key.
   - `normalize(event, skip_keys)`: returns a small dataclass `NormalizedEvent(device_key, model, identity: dict, fields: dict)` where `fields` excludes identity + skip-keys.
2. **coordinator/**:
   - Prefer a plain coordinator class (not necessarily `DataUpdateCoordinator`, since this is push not poll) holding `hass`, `entry`, connection params, `last_seen: dict[str, datetime]`, `devices: dict[str, NormalizedEvent-ish]`, `discovery_enabled`, `availability_timeout`.
   - `async_start()`: launch a background task (`entry.async_create_background_task`) running the connect loop.
   - Connect loop: open WS via aiohttp; `async for msg in ws:` parse text frames as JSON; skip empties; on exception, log, sleep backoff, reconnect; exit when a stop event is set.
   - On each event: `normalize`, update `last_seen[device_key] = utcnow()`, mark device available, `async_dispatcher_send(hass, SIGNAL_DEVICE_UPDATE.format(...), normalized)`. If device is new and discovery enabled, expose a hook the integration uses to start discovery (Task 7/9 will provide the callback — define a `new_device_callback` attribute the coordinator calls; do NOT import config_flow here).
   - Watchdog: `async_track_time_interval` every ~30s; for each device, if `utcnow() - last_seen > effective_timeout`, dispatch an availability-changed signal (or set a flag entities read). Effective timeout = per-device override (looked up via a callback/attribute) else hub default.
   - `async_stop()`: set stop event, close WS, cancel watchdog unsub.
   - `validate_connection(...)`: open a WS with a short timeout, return True on success; close immediately. Used by the config flow.
3. Keep `new_device_callback`, `effective_timeout_resolver`, and `skip_keys` as injectable attributes so this module has NO import of `config_flow`, `mapping`, or `entity` (avoids cycles and keeps Phase 2 file-disjoint from Task 5).
4. Use `homeassistant.util.dt.utcnow()` for timestamps.
5. Create only `normalizer.py` and `coordinator/` files. Do not touch `mapping.py`.
6. `ruff check`; commit `feat:`.
</details>
