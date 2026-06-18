---
id: 1
group: "config-flow"
dependencies: []
status: "completed"
created: "2026-06-04"
skills:
  - python
---
# Shared rebind helper + reconfigure rebind path (A, B2 core)

## Objective
Add a single shared `async_rebind_hub` helper (the spine reused by discovery and
repairs) and wire it into `async_step_reconfigure` so a discovered/adopted hub
entry can be re-pointed at a **new stable radio `unique_id`** while preserving its
`entry_id`, `entry.data["devices"]`, and history.

## Skills Required
- `python` — Home Assistant config-flow + config-entries APIs.

## Acceptance Criteria
- [ ] A module-level `async def async_rebind_hub(hass, entry, new_unique_id, conn_updates, title=None) -> str` exists in `config_flow.py`.
- [ ] It returns `"already_configured"` (and makes **no** change) when another entry with a **non-empty** `entry.data["devices"]` already owns `new_unique_id`.
- [ ] It deletes an **empty-orphan** conflicting entry (no/empty devices map) and proceeds with the rebind.
- [ ] It updates the entry's `unique_id` + connection data and reloads it in place (entry_id preserved).
- [ ] `const.py` defines `CONF_RADIO_ID: Final = "radio_id"`.
- [ ] The reconfigure form for a **discovered/adopted** entry shows an optional `CONF_RADIO_ID` free-text field pre-filled with the entry's current `unique_id`.
- [ ] Submitting reconfigure with a changed radio id rebinds via the helper; an unchanged value keeps today's connection-only update; the **legacy `hub:`** branch is unchanged.
- [ ] On rebind collision (`already_configured`), reconfigure aborts `already_configured`.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `custom_components/rtl_433/config_flow.py`, `custom_components/rtl_433/const.py`.
- Use `hass.config_entries.async_update_entry(...)` + `async_reload(...)`; do not touch device/entity registries.
- Reuse the existing `CONF_SECURE` constant and `Rtl433Coordinator.validate_connection`.

## Input Dependencies
None.

## Output Artifacts
- `async_rebind_hub` helper + `CONF_RADIO_ID` constant — consumed by tasks 2, 3, 4, 6.

## Implementation Notes
<details>
<summary>Step-by-step</summary>

**1. `const.py`** — add near the other hub keys:
```python
# Free-text "new stable radio unique_id" the user supplies when re-pointing a hub
# at a replacement radio (reconfigure / discovery-replace / unreachable repair).
CONF_RADIO_ID: Final = "radio_id"
```

**2. `config_flow.py` imports** — add `from homeassistant.core import HomeAssistant`
(if not already importing it for typing) and `CONF_DEVICES`, `CONF_RADIO_ID` to the
`.const` import block. `CONF_SECURE` already lives in this module.

**3. Add the shared helper** (module level, after `_hub_unique_id`):
```python
async def async_rebind_hub(
    hass: HomeAssistant,
    entry: ConfigEntry,
    new_unique_id: str,
    conn_updates: dict[str, Any],
    title: str | None = None,
) -> str:
    """Re-point a hub entry at a new stable radio unique_id, in place.

    Preserves entry_id (so all nested devices/entities/history survive). When a
    *different* entry already owns ``new_unique_id``: if that entry has a
    populated devices map it is a real hub -> return ``"already_configured"`` and
    change nothing; if it is an empty orphan (e.g. a duplicate auto-created by
    discovery on a new host:port) it is removed and the rebind proceeds.
    Returns ``"ok"`` on success.
    """
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id:
            continue
        if other.unique_id == new_unique_id:
            if other.data.get(CONF_DEVICES):
                return "already_configured"
            await hass.config_entries.async_remove(other.entry_id)
            break
    updates: dict[str, Any] = {
        "unique_id": new_unique_id,
        "data": {**entry.data, **conn_updates},
    }
    if title is not None:
        updates["title"] = title
    hass.config_entries.async_update_entry(entry, **updates)
    await hass.config_entries.async_reload(entry.entry_id)
    return "ok"
```

**4. Reconfigure schema** — extend `_reconfigure_schema` so a non-legacy entry
shows the radio-id field. Add a parameter or branch on the entry's unique_id:
```python
@staticmethod
def _reconfigure_schema(entry: ConfigEntry) -> vol.Schema:
    data = entry.data
    fields: dict[Any, Any] = {}
    uid = entry.unique_id or ""
    # Only discovered/adopted entries carry a stable radio id worth rebinding;
    # legacy hub:host:port entries rebind via host:port alone.
    if uid and not uid.startswith("hub:"):
        fields[vol.Optional(CONF_RADIO_ID, default=uid)] = str
    fields.update({
        vol.Required(CONF_HOST, default=data.get(CONF_HOST, "")): str,
        vol.Required(CONF_PORT, default=data.get(CONF_PORT, DEFAULT_PORT)): int,
        vol.Required(CONF_PATH, default=data.get(CONF_PATH, DEFAULT_PATH)): str,
        vol.Optional(CONF_SECURE, default=data.get(CONF_SECURE, False)): bool,
    })
    return vol.Schema(fields)
```

**5. Reconfigure submit** — in `async_step_reconfigure`, inside the `else:` after a
successful `validate_connection`, replace the discovered/adopted branch
(`config_flow.py:235-245`) with a rebind-aware version. Keep the legacy `hub:`
branch (lines 211-234) untouched:
```python
# Discovered/adopted entry: allow re-pointing at a new stable radio id.
conn = {CONF_HOST: host, CONF_PORT: port, CONF_PATH: path, CONF_SECURE: secure}
new_uid = (user_input.get(CONF_RADIO_ID) or "").strip() or (entry.unique_id or "")
if new_uid and new_uid != entry.unique_id:
    status = await async_rebind_hub(
        self.hass, entry, new_uid, conn, title=f"rtl_433 ({host})"
    )
    if status == "already_configured":
        return self.async_abort(reason="already_configured")
    return self.async_abort(reason="reconfigure_successful")
return self.async_update_reload_and_abort(
    entry,
    title=f"rtl_433 ({host})",
    data_updates=conn,
)
```

**6. Run** `uv run pytest tests/test_config_flow.py -q` to confirm existing
reconfigure tests still pass (behavior for unchanged-id and legacy `hub:` is
preserved). New tests are added in task 6.
</details>
