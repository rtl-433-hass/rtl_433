---
id: 1
group: "api"
dependencies: []
status: "pending"
created: 2026-09-01
skills:
  - home-assistant-integration
  - websocket-api
---
# Extract the adoption service and add the WebSocket API

## Objective

Move adopt / ignore / un-ignore out of the options flow into a shared service, then
build the WebSocket API on top of it: five admin-gated commands plus a
subscription that pushes when the pending list changes without firing per RF frame.

## Skills Required

- `home-assistant-integration` — dispatcher signals, config entries, coordinator.
- `websocket-api` — `homeassistant.components.websocket_api` command registration,
  schemas, subscriptions, and error responses.

## Acceptance Criteria

- [ ] New `custom_components/rtl_433/adoption.py` exposes `async_adopt_devices`,
      `async_ignore_devices`, `async_unignore_devices`, each returning which keys
      were applied and which were skipped.
- [ ] `options_flow.py` calls that service; `_apply_add_and_ignore` and the
      un-ignore submit branch no longer contain their own implementations.
- [ ] **Every plan-29 test passes unmodified.** If a test needs changing, the
      extraction changed behaviour — stop and report it.
- [ ] `const.py` defines `SIGNAL_PENDING_UPDATE` with a `signal_pending_update()`
      helper, matching the existing signal-helper style.
- [ ] The coordinator fires that signal when the pending map's membership
      changes: a new candidate, an adopt, an ignore, a `forget_device`.
- [ ] A repeat sighting of an existing candidate does **not** fire a message per
      frame; updates are coalesced behind a short throttle.
- [ ] New `custom_components/rtl_433/websocket_api.py` registers, all admin-gated:
      `rtl_433/hubs`, `rtl_433/devices/pending`, `rtl_433/devices/add`,
      `rtl_433/devices/ignore`, `rtl_433/devices/unignore`, and
      `rtl_433/devices/subscribe`.
- [ ] Each command validates `entry_id` and returns a proper websocket error for
      an unknown or unloaded entry rather than raising.
- [ ] `rtl_433/devices/pending` returns per device: key, model, count, signal
      level (or null), `first_seen`/`last_seen` as ISO strings, and the latest
      field values.
- [ ] Commands are registered once per Home Assistant run, not once per entry.
- [ ] `manifest.json` declares the `http`, `websocket_api`, and `panel_custom`
      dependencies.
- [ ] `uv run pytest tests/` exits 0; ruff check and format clean.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Read `homeassistant/components/dynalite/panel.py` and
  `homeassistant/components/knx/websocket.py` in
  `.venv/lib/python3.14/site-packages/` for the registration pattern before
  writing.
- The logic to extract is `Rtl433OptionsFlow._apply_add_and_ignore`
  (~line 262 of `options_flow.py`) and the `user_input is not None` branch of
  `async_step_ignored_devices` (~line 313).
- Adoption must keep going through `coordinator.adopt_device` followed by
  `async_upsert_device` — one registration path, not two.
- The coordinator is at `hass.data[DOMAIN][entry.entry_id]`.

## Input Dependencies

None — first task of the plan.

## Output Artifacts

- `adoption.py` and `websocket_api.py`, consumed by task 2's panel and asserted
  by task 3's tests.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### 1. `adoption.py`

Signatures along these lines — return a result so a caller can report skips:

```python
async def async_adopt_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Rtl433Coordinator,
    device_keys: Iterable[str],
) -> AdoptionResult:
    """Adopt each pending device and persist it, reporting what was applied.

    A key that is no longer pending yields ``None`` from ``adopt_device`` -- it
    stopped being a candidate between the render and the call, or a second caller
    already took it -- and is reported as skipped rather than silently dropped:
    the WebSocket caller has a user waiting on an answer, unlike the options flow
    which could afford to ignore it.
    """
```

`AdoptionResult` can be a small frozen dataclass of `applied: list[str]` and
`skipped: list[str]`. Move the bodies across with their behaviour intact — the
existing docstrings in `options_flow.py` explain *why* each step is as it is;
carry that reasoning into the new module rather than dropping it.

Then make the options flow call the service. Its steps keep their own docstrings
about flow mechanics (aborts, `async_create_entry` handing options back
unchanged); only the doing moves.

### 2. `SIGNAL_PENDING_UPDATE`

In `const.py`, next to the existing signals, following their exact style:

```python
# Hub-level "the pending-device list changed" signal. Fired when the set of
# candidates changes -- one appears, or one is adopted, ignored, or forgotten --
# so the WebSocket subscription behind the discovery panel can push a fresh list.
# Deliberately NOT fired for a repeat sighting of a known candidate: a busy
# receiver decodes constantly, and a per-frame push would saturate the socket for
# a count that ticked up by one.
SIGNAL_PENDING_UPDATE: Final = "rtl_433_pending_update_{hub_entry_id}"
```

Fire it from `_record_pending` **only on the new-candidate branch**, and from
`adopt_device`, `ignore_device`, and `forget_device` in `base.py`.

For the repeat-sighting case, the counts and last-seen do drift out of date in an
open panel. Handle that in the **websocket layer**, not the coordinator: the
subscription re-sends the list at most once every few seconds when sightings have
occurred (a throttle/debounce, e.g. `async_call_later` coalescing). Keep the
coalescing logic in `websocket_api.py` so the coordinator stays a pure state
holder.

### 3. `websocket_api.py`

Follow the core pattern:

```python
@callback
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "rtl_433/devices/add",
        vol.Required("entry_id"): str,
        vol.Required("device_keys"): [str],
    }
)
@websocket_api.async_response
async def ws_add_devices(hass, connection, msg): ...
```

Resolve the entry and coordinator once in a small helper that sends
`websocket_api.const.ERR_NOT_FOUND` for an unknown entry id and a clear error for
an entry that is not loaded, returning `None` so each command can bail early.
Never let a bad `entry_id` raise out of a handler.

Register everything in one `async_register_commands(hass)` function called from
`__init__.py`'s `async_setup_entry`, guarded so it runs once per run — set a
sentinel in `hass.data[DOMAIN]`, or check as dynalite does with
`async_panel_exists`. Two hubs must not register the commands twice.

For `rtl_433/devices/subscribe`, follow the standard subscription shape:
connection subscribes, the handler wires `async_dispatcher_connect` and stores the
unsubscribe in `connection.subscriptions[msg["id"]]`, sends the current list
immediately, then pushes on change. Remember `connection.send_result(msg["id"])`
to acknowledge before pushing events.

### 4. `manifest.json`

```json
"dependencies": ["http", "panel_custom", "websocket_api"],
```

Keep the keys alphabetically ordered as hassfest expects.

### 5. Faithfulness check

After the extraction, run `git diff --stat tests/` — it should be **empty**. The
plan-29 suite passing untouched is what proves the behaviour moved intact. If
something genuinely must change, stop and report rather than editing the test.

</details>
