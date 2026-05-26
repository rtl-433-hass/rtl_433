---
id: 1
group: "coordinator"
dependencies: []
status: "pending"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# Coordinator frame classification, hub-update signal, and connectivity state

## Objective
Stop non-event WebSocket frames (`meta`, state/stats, RPC `result`/`error`,
`shutdown`) from being normalized as device events, and introduce the hub-level
dispatcher signal plus hub runtime state that later hub entities subscribe to. A
frame is a decoded-device event **iff** it carries a `model` key or any identity
key (`id`/`channel`/`subtype`); a `shutdown` frame flips connectivity off; every
other object is dropped without normalization (no device key, no new-device
callback, no `seen_fields` pollution). The coordinator also emits the new
hub-update signal whenever connectivity changes (connect, disconnect, shutdown).

This is the root correctness fix and the foundation for the connectivity sensor
(Task 4) and the diagnostic sensors (Task 5).

## Skills Required
- `python` — async class methods, dict shape inspection.
- `home-assistant` — dispatcher signals, coordinator lifecycle conventions.

## Acceptance Criteria
- [ ] `const.py` defines `SIGNAL_HUB_UPDATE` and a `signal_hub_update(hub_entry_id)` helper, mirroring the existing `signal_new_device` helper.
- [ ] The coordinator gains `self.meta: dict[str, Any]` and `self.stats: dict[str, Any]` runtime state (initialized empty) alongside the existing `connected` flag, and a `_emit_hub_update()` method that dispatches `signal_hub_update(entry_id)` with no payload.
- [ ] `_handle_text_frame` classifies each parsed JSON object: a frame with `model` **or** any of `id`/`channel`/`subtype` (value not `None`) goes through the unchanged `_process_event` path; a frame with a `shutdown` key sets `connected=False` and emits the hub-update signal; any other object is dropped (no normalize, no device, no `seen_fields` change).
- [ ] The connect loop emits the hub-update signal when the socket opens (after `connected=True`) and when it drops (in the `finally`, after `connected=False`).
- [ ] New tests in `tests/test_coordinator.py` assert: a `meta` object, a stats frame, and an RPC `{"result":...}`/`{"error":...}` frame create no device and add nothing to `seen_fields`; a `{"shutdown":"goodbye"}` frame sets `connected=False` and emits the hub-update signal; a model-less identity event (`{"channel": 1, ...}`) still creates its device; a normal `model` event is unchanged.
- [ ] `uv run pytest tests/test_coordinator.py` passes; `uv run ruff check custom_components/rtl_433` is clean.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `custom_components/rtl_433/const.py`, `custom_components/rtl_433/coordinator/base.py`, `tests/test_coordinator.py`.
- Dispatcher: `homeassistant.helpers.dispatcher.async_dispatcher_send` (already imported in `base.py`).
- Preserve all existing coordinator behavior for real device events.

## Input Dependencies
None. This is a Phase 1 task.

## Output Artifacts
- `signal_hub_update` / `SIGNAL_HUB_UPDATE` in `const.py` (consumed by Tasks 4 and 5).
- `coordinator.meta`, `coordinator.stats`, `coordinator._emit_hub_update()` (consumed by Task 2 to publish getter results and Tasks 4/5 to render).
- The classifier (consumed implicitly by all downstream behavior and the cleanup in Task 3).

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### 1. `const.py` — add the hub-update signal
After the existing `SIGNAL_NEW_DEVICE` / `signal_new_device` block (around line 107-112), append a parallel block:

```python
# Hub-level "connectivity / SDR meta / server stats changed" signal. The
# coordinator dispatches this (no payload) whenever the hub's connection state,
# meta/SDR configuration, or server stats change; the statically-registered hub
# entities subscribe and re-read the coordinator's hub state.
SIGNAL_HUB_UPDATE: Final = "rtl_433_hub_update_{hub_entry_id}"


def signal_hub_update(hub_entry_id: str) -> str:
    """Return the hub-level update dispatcher signal for one hub."""
    return SIGNAL_HUB_UPDATE.format(hub_entry_id=hub_entry_id)
```

### 2. `coordinator/base.py` — imports and runtime state
- Add `signal_hub_update` to the `from ..const import (...)` block (alongside `signal_device_update`).
- In `__init__`, after the existing `self.connected = False` line (~line 147), add:

```python
# --- Hub-scoped runtime state (rendered by the hub entities) ---------
# Latest meta/SDR configuration (assembled from the HTTP getters in Task 2)
# and the latest server-stats payload. Populated over HTTP, not the socket.
self.meta: dict[str, Any] = {}
self.stats: dict[str, Any] = {}
```

(`Any` is already imported.)

### 3. `_emit_hub_update` helper
Add a small method near `_dispatch` (the per-device fan-out):

```python
def _emit_hub_update(self) -> None:
    """Notify hub entities that connectivity / meta / stats changed."""
    async_dispatcher_send(self.hass, signal_hub_update(self.entry.entry_id))
```

### 4. Classifier in `_handle_text_frame`
The current method ends with `self._process_event(event)` after confirming the
frame is a dict. Replace that final unconditional call with classification:

```python
if not isinstance(event, dict):
    LOGGER.debug("rtl_433 skipping non-object frame: %r", text[:120])
    return
self._classify_frame(event)
```

Add the classifier method (identity keys mirror the normalizer's, minus
`model` which is handled explicitly):

```python
# Identity keys (besides ``model``) that mark a frame as a decoded-device event.
# Kept in sync with normalizer.IDENTITY_KEYS.
_EVENT_IDENTITY_KEYS = ("id", "channel", "subtype")

def _classify_frame(self, event: dict[str, Any]) -> None:
    """Route a parsed frame by shape (Plan: Frame Classification)."""
    is_event = event.get("model") is not None or any(
        event.get(key) is not None for key in self._EVENT_IDENTITY_KEYS
    )
    if is_event:
        self._process_event(event)
    elif "shutdown" in event:
        self._handle_shutdown()
    # All other non-event frames (meta / state / result / error) are ignored:
    # #2/#3 are sourced over HTTP (Task 2), so nothing else needs handling here.
```

`_EVENT_IDENTITY_KEYS` can be a module-level constant or a class attribute;
either is fine. Place it near the existing backoff constants if module-level.

### 5. Shutdown handler
```python
def _handle_shutdown(self) -> None:
    """Handle a ``{"shutdown": ...}`` frame: flip connectivity off."""
    if self.connected:
        LOGGER.debug("rtl_433 server announced shutdown for %s", self.ws_url)
    self.connected = False
    self._emit_hub_update()
```

### 6. Connect-loop emits
In `_connect_loop`, right after `self.connected = True` (and before
`LOGGER.debug("rtl_433 connected ...")` / `_read_frames`):

```python
self.connected = True
backoff = _BACKOFF_MIN
self._emit_hub_update()
```

In the `finally` block, after `self.connected = False`:

```python
finally:
    self._ws = None
    self.connected = False
    self._emit_hub_update()
```

### 7. Tests (`tests/test_coordinator.py`)
The existing `coordinator` fixture and `DISPATCH` patch target
(`custom_components.rtl_433.coordinator.base.async_dispatcher_send`) already
exist — reuse them. Note that `_emit_hub_update` calls the same
`async_dispatcher_send`, so when asserting *device* dispatch counts you may need
to inspect `call_args_list` and filter by the signal name, OR assert on
`coordinator.devices` / `coordinator.seen_fields` directly (preferred for the
classification tests). Add tests such as:

- `test_meta_frame_ignored`: feed a meta object (`{"center_frequency": 433920000, "samp_rate": 250000, "frequencies": [433920000], "hop_times": [600]}`) → `coordinator.devices == {}` and `coordinator.seen_fields == set()`.
- `test_stats_frame_ignored`: feed `{"enabled": 5, "since": "2026-05-26T10:00:00", "frames": {"count": 3, "fsk": 1, "events": 9}}` → no device, no seen_fields.
- `test_rpc_result_error_frames_ignored`: feed `{"result": "ok"}` and `{"error": "bad"}` → no device, no seen_fields.
- `test_shutdown_frame_flips_connectivity_and_emits`: set `coordinator.connected = True`; patch `DISPATCH`; feed `{"shutdown": "goodbye"}`; assert `coordinator.connected is False` and the hub-update signal `signal_hub_update(entry_id)` appears in `dispatch.call_args_list`.
- `test_model_less_identity_event_creates_device`: feed `{"channel": 1, "temperature_C": 5}` (no model) → `"unknown-ch1"` (or whatever `device_key` yields) is in `coordinator.devices`. Verify against `normalizer.device_key`.
- The existing `test_parses_frame_updates_state_and_dispatches` must still pass unchanged (normal `model` event).

Import `signal_hub_update` from `custom_components.rtl_433.const` in the test
module.

### Gotchas
- Do NOT add a `seen_fields` entry for ignored frames — that is the whole point.
- `_process_event` is unchanged; only its *gating* moves into the classifier.
- Keep the empty/whitespace/malformed/non-dict guards in `_handle_text_frame` exactly as they are; classification happens only for parsed dicts.
</details>
