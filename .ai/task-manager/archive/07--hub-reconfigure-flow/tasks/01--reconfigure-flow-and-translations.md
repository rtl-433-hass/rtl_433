---
id: 1
group: "reconfigure-flow"
dependencies: []
status: "completed"
created: 2026-05-28
skills:
  - python
  - home-assistant
---
# Implement `async_step_reconfigure` + translations

## Objective
Add Home Assistant's standard reconfigure flow to `Rtl433ConfigFlow` so an existing hub's connection target (`host`, `port`, `path`, `secure`) can be edited in place — preserving the entry's `entry_id`, its nested devices, entities, and history — and add the matching localized strings to `translations/en.json`.

## Skills Required
- `python` — Home Assistant config-flow code in `custom_components/rtl_433/config_flow.py`.
- `home-assistant` — config entries, `SOURCE_RECONFIGURE`, unique-id handling, `voluptuous` schemas, `async_update_reload_and_abort`.

## Acceptance Criteria
- [ ] `Rtl433ConfigFlow` gains `async_step_reconfigure(self, user_input=None)`.
- [ ] Initial render shows a form (`step_id="reconfigure"`) pre-filled with the current `host`/`port`/`path`/`secure` from `entry.data`; `manage_settings` is **omitted**.
- [ ] On submit, reachability is validated via `Rtl433Coordinator.validate_connection(self.hass, host, port, path, secure=secure)`; on `CannotConnect` the form re-shows with `errors={"base": "cannot_connect"}` and the entry is unchanged.
- [ ] On success: `new_unique_id = _hub_unique_id(host, port)`, `await self.async_set_unique_id(new_unique_id)`, a collision guard aborts with `already_configured` only when a **different** entry already owns `new_unique_id`, then `self.async_update_reload_and_abort(entry, unique_id=new_unique_id, title=f"rtl_433 ({host})", data_updates={CONF_HOST: host, CONF_PORT: port, CONF_PATH: path, CONF_SECURE: secure})`.
- [ ] `_abort_if_unique_id_mismatch` is **not** used (it would block the host/port change).
- [ ] `data_updates=` (merge) is used — never `data=` — so `entry.data["devices"]` and `CONF_MANAGE_SETTINGS` survive.
- [ ] `translations/en.json` gains `config.step.reconfigure` (title, description, `data` + `data_description` for host/port/path/secure, no `manage_settings`) and `config.abort.reconfigure_successful`; it remains valid JSON.

## Technical Requirements
- File: `custom_components/rtl_433/config_flow.py` (the flow class is `Rtl433ConfigFlow(ConfigFlow, domain=DOMAIN)`, `VERSION = 2`).
- Reuse existing helpers/constants: `_hub_unique_id(host, port)` (`config_flow.py:69-71`), `CONF_SECURE` (local, `config_flow.py:63`), `CONF_HOST`/`CONF_PORT`/`CONF_PATH` (`const.py`), `DEFAULT_PORT`/`DEFAULT_PATH`, and the user-step schema shape `STEP_USER_SCHEMA` (`config_flow.py:74-82`).
- Reuse `Rtl433Coordinator.validate_connection` and the `CannotConnect` exception already imported/used by `async_step_user`.
- File: `custom_components/rtl_433/translations/en.json` (repo uses only `en.json`, no `strings.json`).

## Input Dependencies
None (first task).

## Output Artifacts
- A working reconfigure step in `config_flow.py` consumed by the tests (task 2) and docs (task 3).
- New translation keys.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. **Read first**: open `custom_components/rtl_433/config_flow.py` and study `async_step_user` (`~:93-130`), `_hub_unique_id` (`~:69-71`), the `CONF_SECURE` definition (`~:63`), and `STEP_USER_SCHEMA` (`~:74-82`). Open `custom_components/rtl_433/translations/en.json` and study the existing `config.step.user` block and `config.abort` / `config.error` blocks.

2. **Build a reconfigure schema** mirroring the connection subset of `STEP_USER_SCHEMA` but WITHOUT `manage_settings`. Pre-fill defaults from the reconfigure entry. Pattern:
   ```python
   def _reconfigure_schema(self, entry):
       data = entry.data
       return vol.Schema({
           vol.Required(CONF_HOST, default=data.get(CONF_HOST, "")): str,
           vol.Required(CONF_PORT, default=data.get(CONF_PORT, DEFAULT_PORT)): int,
           vol.Required(CONF_PATH, default=data.get(CONF_PATH, DEFAULT_PATH)): str,
           vol.Required(CONF_SECURE, default=data.get(CONF_SECURE, False)): bool,
       })
   ```
   Match the field types/markers actually used in `STEP_USER_SCHEMA` (use `selector`/`cv` helpers if the user step does).

3. **The step**:
   ```python
   async def async_step_reconfigure(self, user_input=None):
       entry = self._get_reconfigure_entry()
       errors = {}
       if user_input is not None:
           try:
               await Rtl433Coordinator.validate_connection(
                   self.hass,
                   user_input[CONF_HOST], user_input[CONF_PORT],
                   user_input[CONF_PATH], secure=user_input[CONF_SECURE],
               )
           except CannotConnect:
               errors["base"] = "cannot_connect"
           else:
               new_unique_id = _hub_unique_id(user_input[CONF_HOST], user_input[CONF_PORT])
               await self.async_set_unique_id(new_unique_id)
               # Collision guard: abort only if a DIFFERENT entry owns this id.
               for other in self._async_current_entries():
                   if other.unique_id == new_unique_id and other.entry_id != entry.entry_id:
                       return self.async_abort(reason="already_configured")
               return self.async_update_reload_and_abort(
                   entry,
                   unique_id=new_unique_id,
                   title=f"rtl_433 ({user_input[CONF_HOST]})",
                   data_updates={
                       CONF_HOST: user_input[CONF_HOST],
                       CONF_PORT: user_input[CONF_PORT],
                       CONF_PATH: user_input[CONF_PATH],
                       CONF_SECURE: user_input[CONF_SECURE],
                   },
               )
       return self.async_show_form(
           step_id="reconfigure",
           data_schema=self._reconfigure_schema(entry),
           errors=errors,
       )
   ```
   Verify the exact `validate_connection` signature in `coordinator/base.py:771-793` and match positional/keyword args. Do not call `async_set_unique_id` before validation succeeds.

4. **Translations** — add under `config.step`:
   ```json
   "reconfigure": {
     "title": "Reconfigure rtl_433 hub",
     "description": "Update this hub's connection target. Nested devices and their history are preserved.",
     "data": {
       "host": "Host", "port": "Port", "path": "WebSocket path", "secure": "Use secure WebSocket (wss)"
     },
     "data_description": {
       "host": "Hostname or IP of the rtl_433 server.",
       "port": "WebSocket port (default 8433).",
       "path": "WebSocket path (default /ws).",
       "secure": "Connect with wss instead of ws."
     }
   }
   ```
   Mirror the wording/keys of the existing `user` step. Add `"reconfigure_successful": "Connection settings updated."` under `config.abort`. Keep JSON valid (watch trailing commas).

5. **Do not** add `manage_settings` to the reconfigure schema or strings. **Do not** touch the options flow.

6. Run `ruff`/the repo linter and `python -c "import json; json.load(open('custom_components/rtl_433/translations/en.json'))"` to confirm validity.
</details>
