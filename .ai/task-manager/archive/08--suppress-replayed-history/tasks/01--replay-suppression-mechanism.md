---
id: 1
group: "replay-suppression"
dependencies: []
status: "completed"
created: 2026-05-28
skills:
  - python
  - home-assistant
complexity_score: 5
complexity_notes: "Single atomic coordinator+event change spanning base.py/normalizer.py/entity.py/event.py; kept as one task because all files must agree on one is_replay carrier decision — splitting across agents would risk inconsistent signatures."
---
# Implement replay-suppression mechanism (parser + classification + dispatch + event guard)

## Objective
Make the coordinator distinguish replayed/stale frames from live ones using two signals derived from the raw event `time`, so replays seed sensor values but do NOT re-fire `event` entities or refresh `last_seen`/`available`. Suppressed event-entity transmissions are logged at INFO.

## Skills Required
- `python`, `home-assistant` — coordinator event path (`coordinator/base.py`), `NormalizedEvent` (`normalizer.py`), entity dispatch (`entity.py`), event platform (`event.py`), `homeassistant.util.dt`.

## Acceptance Criteria
- [ ] **Defensive timestamp parser** (Component 1): converts raw `time` (local `"YYYY-MM-DD HH:MM:SS"` with optional fractional seconds, OR ISO-8601 with offset/`Z`) to a single comparable UTC basis; missing/blank/unparseable → "no usable timestamp"; never raises into the frame loop.
- [ ] **High-water mark + threshold** (Component 2): coordinator holds a max-parsed-`time` high-water mark (new runtime field near `self.devices`, initially unset) and a module constant `REPLAY_STALE_THRESHOLD` (`timedelta`, ~30 s, near the other interval constants).
- [ ] **Three-way classification** in `_process_event`:
  - already-seen replay: parsed `time <= mark` → `is_replay=True`, do not touch liveness, seed sensors.
  - stale gap event: parsed `time > mark` AND `now - time > REPLAY_STALE_THRESHOLD` → `is_replay=True`, advance mark, log INFO (in the event entity), do not touch liveness, seed sensors.
  - live: (`time > mark` AND recent) OR no usable `time` → fire, stamp `last_seen=now` + `available=True`, advance mark (when timed), seed sensors.
- [ ] **Replay-aware state update**: always update `self.devices[key]` + field tracking; only live frames stamp `last_seen`/`available`/back-online log. New-device callback (`base.py:717-721`) still fires for replay-discovered devices.
- [ ] **`is_replay` carrier**: choose ONE mechanism (add `is_replay: bool` to the `NormalizedEvent` frozen dataclass, OR a second positional arg on `signal_device_update`) and update **every** `_handle_dispatch` consistently. Carry the event `time`/age too (needed for the INFO log). The watchdog re-dispatch (`base.py:766`) must pass `is_replay=False`.
- [ ] **Event guard + INFO log** (Component 4): `Rtl433Event._handle_dispatch` must NOT `_trigger_event`, append `event_types`, or persist for replay/stale frames (it may still `async_write_ha_state`). When suppressing a frame that *would* have fired (field present in `event.fields`), log once at INFO: device key, model, event type (`str(value)`), event time/age.
- [ ] Base `Rtl433Entity._handle_dispatch` still applies value + writes state for replays (sensors seed).
- [ ] `time` never leaks into `NormalizedEvent.fields` (consume it before/independently of normalize and/or exclude it).
- [ ] `ruff` clean; module imports cleanly.

## Technical Requirements
- Files: `custom_components/rtl_433/coordinator/base.py`, `custom_components/rtl_433/normalizer.py`, `custom_components/rtl_433/entity.py`, `custom_components/rtl_433/event.py`.
- Use `dt_util.utcnow()` for `now` (consistent with the watchdog, `base.py:752`).

## Input Dependencies
None (first task).

## Output Artifacts
- The replay-suppression mechanism, consumed by tests (task 2) and docs (task 3).

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

- **Read first**: `_process_event` (`base.py:697`), `_dispatch` (`base.py:728`), the runtime-state block (`base.py:200-206`), interval constants (`base.py:63-88`), watchdog re-dispatch (`base.py:750-766`); `NormalizedEvent` + `normalize()` + `DEFAULT_SKIP_KEYS` (`normalizer.py`); `Rtl433Entity._handle_dispatch` (`entity.py:193`); `Rtl433Event._handle_dispatch` (`event.py:78-110`) and its identity dedupe (`event.py:89`).
- **Carrier recommendation**: adding `is_replay: bool = False` (and e.g. `event_time: datetime | None = None`) to `NormalizedEvent` keeps the flag traveling with the object and minimizes signature churn — but `NormalizedEvent` is created by `normalize()`, so the coordinator must set these after constructing it (it's frozen — use `dataclasses.replace` or add as non-init fields set via object.__setattr__, OR make them init kwargs `normalize()` passes through). Pick the cleanest; document it. If instead you add a dispatch arg, update `_dispatch`, the watchdog re-dispatch, and EVERY `_handle_dispatch(self, event)` → `_handle_dispatch(self, event, is_replay)` across `entity.py`, `sensor.py`, `binary_sensor.py`, `event.py`, and the last-seen sensor.
- **Parser**: try `dt_util.parse_datetime` first (handles ISO-8601); for the local `"YYYY-MM-DD HH:MM:SS[.ffffff]"` form, parse with `datetime.strptime` variants and treat as local, then convert to UTC via `dt_util.as_utc(dt_util.as_local(...))` or `dt_util.as_utc` with `dt_util.DEFAULT_TIME_ZONE`. Reduce naive→aware consistently. Wrap in try/except → None.
- **Classification order matters**: parse → if None: live. else compare to mark → `<=`: already-seen replay. else age check → stale vs live. Advance mark to `max(mark, time)` on stale and live (timed) outcomes.
- **`time` out of fields**: confirm via reading normalize() whether runtime `skip_keys` (from `_skip_keys.yaml`) lists `time` — it does NOT, so either pop `time` from the dict before `normalize()` or add it to the skip set used. Verify nothing else depends on `time` in `fields`.
- Keep changes minimal and surgical; do not add user config or new entities.
- After coding: `python -m ruff check custom_components/` and `python -c "import ast,glob; [ast.parse(open(f).read()) for f in glob.glob('custom_components/rtl_433/**/*.py', recursive=True)]"`.
</details>
