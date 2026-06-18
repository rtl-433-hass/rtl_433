---
id: 1
group: "config-flow-discovery"
dependencies: []
status: "completed"
created: 2026-06-01
skills:
  - python
---
# Implement Supervisor (hassio) discovery in the config flow

## Objective
Add Home Assistant Supervisor add-on discovery support to the rtl_433 config flow so radios published by the rtl_433 add-on are auto-discovered, confirmed by the user, and keyed by the add-on's stable per-radio `unique_id`. Also harden the existing manual `user` and `reconfigure` flows so the two identity schemes (`hub:{host}:{port}` for manual adds, the add-on's stable radio id for discovered/adopted entries) coexist without ever producing duplicate entries or clobbering a stable id. All changes are confined to `custom_components/rtl_433/config_flow.py` and `custom_components/rtl_433/translations/en.json`.

## Skills Required
- `python` — Home Assistant config-flow development (discovery flows, `async_set_unique_id`, `_abort_if_unique_id_configured`, `async_update_entry`, `async_create_entry`).

## Acceptance Criteria
- [ ] `Rtl433ConfigFlow.async_step_hassio(self, discovery_info: HassioServiceInfo)` is implemented and reads `host`, `port`, `path` (default `/ws`), `secure` (default `False`), and `unique_id` from `discovery_info.config`.
- [ ] If an existing entry targets the same `host:port` but has a *different* `unique_id`, that entry is re-keyed to the advertised radio `unique_id` (with refreshed connection data) and the flow aborts with `already_configured` (adoption/migration; no new entry, no confirmation).
- [ ] Otherwise the flow calls `async_set_unique_id(<radio id>)` then `_abort_if_unique_id_configured(updates={host, port, path, secure})` (same-radio re-advertisement, including on a new port, updates the stored connection in place).
- [ ] A genuinely new radio routes to `async_step_hassio_confirm`, which shows a confirmation form (no input fields) with `addon`/`host`/`port` description placeholders, validates connectivity via `Rtl433Coordinator.validate_connection`, and on success creates a hub entry with `data = {host, port, path, secure, manage_settings: DEFAULT_MANAGE_SETTINGS}`. A failed validation re-shows the form with a `cannot_connect` error.
- [ ] The manual `async_step_user` aborts with `already_configured` when the chosen `host:port` is already configured by any existing entry (manual or discovered), via a shared host:port lookup helper.
- [ ] The `async_step_reconfigure` flow preserves a stable (non-`hub:`) radio `unique_id` instead of recomputing `hub:{host}:{port}` for such entries.
- [ ] `translations/en.json` gains a `config.step.hassio_confirm` block (title + description using `{addon}`, `{host}`, `{port}` placeholders) and any new abort reason string used by the code. Existing `cannot_connect` error and `already_configured` abort strings are reused.
- [ ] `uv run --python 3.14 ruff check custom_components/rtl_433/config_flow.py` is clean.

## Technical Requirements
- Import `HassioServiceInfo` from `homeassistant.helpers.service_info.hassio`.
- Reuse existing module symbols already imported in `config_flow.py`: `CONF_HOST`, `CONF_PORT`, `CONF_PATH`, `CONF_MANAGE_SETTINGS`, `DEFAULT_PATH`, `DEFAULT_MANAGE_SETTINGS`, the module-level `CONF_SECURE = "secure"`, `Rtl433Coordinator`, `CannotConnect`, and the `_hub_unique_id` helper.
- The add-on always supplies a JSON-safe `unique_id`; use it verbatim (no further sanitisation).
- The HA `hassio` component injects `config["addon"]` (the add-on display name) before dispatch.

## Input Dependencies
None (first task).

## Output Artifacts
- Updated `custom_components/rtl_433/config_flow.py` with `async_step_hassio`, `async_step_hassio_confirm`, a `_find_entry_by_host_port` helper, the user-step dedup guard, and the reconfigure stable-id preservation.
- Updated `custom_components/rtl_433/translations/en.json`.

## Implementation Notes

<details>
<summary>Step-by-step implementation guidance</summary>

All edits are in `custom_components/rtl_433/config_flow.py` and `custom_components/rtl_433/translations/en.json`.

**1. Add the import.** Near the other `homeassistant` imports add:
```python
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
```

**2. Add a host:port lookup helper** on `Rtl433ConfigFlow`:
```python
def _find_entry_by_host_port(self, host: str, port: int) -> ConfigEntry | None:
    """Return an existing entry targeting this host:port, if any."""
    for entry in self._async_current_entries():
        if entry.data.get(CONF_HOST) == host and entry.data.get(CONF_PORT) == port:
            return entry
    return None
```

**3. Add a discovery-state attribute** on the class (alongside `VERSION`/`MINOR_VERSION`):
```python
_discovery: dict[str, Any] | None = None
```

**4. Implement `async_step_hassio`** on `Rtl433ConfigFlow`:
```python
async def async_step_hassio(
    self, discovery_info: HassioServiceInfo
) -> ConfigFlowResult:
    """Handle a Supervisor add-on discovery message for one radio."""
    config = discovery_info.config
    radio_uid = config.get("unique_id")
    if not radio_uid:
        return self.async_abort(reason="invalid_discovery_info")

    host: str = config[CONF_HOST]
    port: int = config[CONF_PORT]
    path: str = config.get(CONF_PATH, DEFAULT_PATH)
    secure: bool = config.get(CONF_SECURE, False)
    addon: str = config.get("addon", "rtl_433")

    # Adopt/migrate a pre-existing entry on the same server onto the stable id.
    existing = self._find_entry_by_host_port(host, port)
    if existing is not None and existing.unique_id != radio_uid:
        self.hass.config_entries.async_update_entry(
            existing,
            unique_id=radio_uid,
            data={
                **existing.data,
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_PATH: path,
                CONF_SECURE: secure,
            },
        )
        return self.async_abort(reason="already_configured")

    # Same radio (matched by stable id) — update connection target in place.
    await self.async_set_unique_id(radio_uid)
    self._abort_if_unique_id_configured(
        updates={
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PATH: path,
            CONF_SECURE: secure,
        }
    )

    self._discovery = {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_PATH: path,
        CONF_SECURE: secure,
        "addon": addon,
    }
    self.context["title_placeholders"] = {"name": f"{addon} ({host}:{port})"}
    return await self.async_step_hassio_confirm()
```

**5. Implement `async_step_hassio_confirm`:**
```python
async def async_step_hassio_confirm(
    self, user_input: dict[str, Any] | None = None
) -> ConfigFlowResult:
    """Confirm adoption of a discovered radio, then create the entry."""
    assert self._discovery is not None
    disc = self._discovery
    placeholders = {
        "addon": disc["addon"],
        "host": disc[CONF_HOST],
        "port": str(disc[CONF_PORT]),
    }

    if user_input is not None:
        try:
            await Rtl433Coordinator.validate_connection(
                self.hass,
                disc[CONF_HOST],
                disc[CONF_PORT],
                disc[CONF_PATH],
                secure=disc[CONF_SECURE],
            )
        except CannotConnect:
            return self.async_show_form(
                step_id="hassio_confirm",
                data_schema=vol.Schema({}),
                errors={"base": "cannot_connect"},
                description_placeholders=placeholders,
            )
        return self.async_create_entry(
            title=f"rtl_433 ({disc[CONF_HOST]}:{disc[CONF_PORT]})",
            data={
                CONF_HOST: disc[CONF_HOST],
                CONF_PORT: disc[CONF_PORT],
                CONF_PATH: disc[CONF_PATH],
                CONF_SECURE: disc[CONF_SECURE],
                CONF_MANAGE_SETTINGS: DEFAULT_MANAGE_SETTINGS,
            },
        )

    return self.async_show_form(
        step_id="hassio_confirm",
        data_schema=vol.Schema({}),
        description_placeholders=placeholders,
    )
```

**6. Harden `async_step_user`.** Inside the `if user_input is not None:` block, after the successful `validate_connection` (in the `else:` branch) and *before* `async_set_unique_id(_hub_unique_id(...))`, add a host:port dedup guard so a manual add can't duplicate a discovered radio:
```python
if self._find_entry_by_host_port(host, port) is not None:
    return self.async_abort(reason="already_configured")
```
(Place it right after the `else:` that follows the `except CannotConnect`.)

**7. Harden `async_step_reconfigure`.** The current code always computes `new_unique_id = _hub_unique_id(host, port)` and calls `async_update_reload_and_abort(..., unique_id=new_unique_id, ...)`. Change it so a stable (non-`hub:`) id is preserved:
```python
current_uid = entry.unique_id or ""
if current_uid.startswith("hub:") or not current_uid:
    # Legacy manual entry: keep the host:port identity scheme.
    new_unique_id = _hub_unique_id(host, port)
    await self.async_set_unique_id(new_unique_id)
    for other in self._async_current_entries():
        if other.unique_id == new_unique_id and other.entry_id != entry.entry_id:
            return self.async_abort(reason="already_configured")
    return self.async_update_reload_and_abort(
        entry,
        unique_id=new_unique_id,
        title=f"rtl_433 ({host})",
        data_updates={
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PATH: path,
            CONF_SECURE: secure,
        },
    )
# Discovered/adopted entry: preserve its stable radio unique_id.
return self.async_update_reload_and_abort(
    entry,
    title=f"rtl_433 ({host})",
    data_updates={
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_PATH: path,
        CONF_SECURE: secure,
    },
)
```
Keep the existing pre-`if` body (reading host/port/path/secure and the `validate_connection`/`CannotConnect` handling) intact; only the unique_id/collision/return section changes.

**8. Translations.** In `custom_components/rtl_433/translations/en.json`, under `config.step`, add (after the `reconfigure` block):
```json
"hassio_confirm": {
  "title": "Add discovered rtl_433 radio",
  "description": "Home Assistant discovered an rtl_433 radio published by the {addon} add-on at {host}:{port}. Add it to keep its devices and history stable across restarts and port changes."
}
```
Under `config.abort`, add:
```json
"invalid_discovery_info": "The discovery message was missing required information."
```
Leave the existing `already_configured` abort and `cannot_connect` error strings unchanged (they are reused).

**Validation while implementing:** run `uv run --python 3.14 ruff check custom_components/rtl_433/config_flow.py` and `python -c "import json; json.load(open('custom_components/rtl_433/translations/en.json'))"` to confirm clean lint and valid JSON. Do not hand-edit `manifest.json` version or `CHANGELOG.md` (release-please-managed).
</details>
