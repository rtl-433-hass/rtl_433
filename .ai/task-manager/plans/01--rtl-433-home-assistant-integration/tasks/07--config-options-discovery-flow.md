---
id: 7
group: "core"
dependencies: [3, 6]
status: "pending"
created: 2026-05-25
skills:
  - home-assistant
  - python
---
# Config, Options & Discovery Flow (hub + per-device + ignore)

## Objective
Implement the config flow and options flow: a hub config entry (host/port/path, validated for reachability, plain `ws://` default with `wss://` accepted, no auth/TLS handling), Battery-Notes-style integration discovery that surfaces newly seen devices, acceptance creating a per-device config entry, dismissal creating a `SOURCE_IGNORE` entry, and an options flow exposing the per-instance discovery toggle and the hub-level default availability timeout (plus per-device timeout override).

## Skills Required
- `home-assistant` — config/options flows, `async_step_integration_discovery`, `discovery_flow`, `SOURCE_IGNORE`, unique_id management
- `python` — voluptuous schemas, async

## Acceptance Criteria
- [ ] `custom_components/rtl_433/config_flow.py` (or a `config_flow_handler/` package) implements `ConfigFlow` for domain `rtl_433`.
- [ ] **Hub user step** (`async_step_user`): form for host, port (default 8433), path (default `/ws`); validates reachability via `coordinator.validate_connection`; on failure shows a form error; on success creates a **hub** config entry (entry data marks it as a hub) with a unique_id derived from host/port so the same server can't be added twice.
- [ ] Accepts `wss://` (the path/host fields allow a full scheme or a scheme toggle); no username/password/token/TLS-cert fields.
- [ ] **Discovery step** (`async_step_integration_discovery`): receives device discovery data (parent hub entry id, device key, model/metadata); sets unique_id to the instance-scoped per-device id; aborts if already configured or if a `SOURCE_IGNORE` entry exists; otherwise shows a confirm form that, on accept, creates a **per-device** config entry storing the parent hub entry id (`CONF_HUB_ENTRY_ID`) and device key.
- [ ] Dismissing a discovery yields a `SOURCE_IGNORE` entry (HA does this when the flow is removed/ignored) keyed by the same unique_id, so the device is not re-surfaced.
- [ ] **Options flow**: for a hub entry, exposes `discovery_enabled` (bool) and `availability_timeout` (int seconds, default from const); for a device entry, exposes a per-device `availability_timeout` override (optional).
- [ ] Per-device unique_id is instance-scoped, e.g. `{hub_entry_id}:{device_key}` so two hubs observing the same `model`+`id` do not collide (Success Criteria #1).
- [ ] `translations/en.json` is updated with the config/options step text and discovery confirm strings (this edits the skeleton from Task 3 — fine, different phase).
- [ ] `ruff check` passes; module imports cleanly.
- [ ] A single conventional commit (e.g. `feat: add config, options and discovery flows`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Use `homeassistant.helpers.discovery_flow.async_create_flow(hass, DOMAIN, context={"source": SOURCE_INTEGRATION_DISCOVERY}, data=...)` as the entry point that the coordinator's `new_device_callback` will call (wired in Task 9). Mirror Battery Notes.
- For ignore handling: in `async_step_integration_discovery`, call `await self.async_set_unique_id(device_unique_id)` then `self._abort_if_unique_id_configured()`; HA's ignore mechanism creates `SOURCE_IGNORE` entries automatically when the user dismisses — ensure the unique_id matches so dismissed devices don't reappear.
- The hub entry and device entry are both `rtl_433` domain entries distinguished by their `data`/`entry_type`. Provide helpers `is_hub_entry(entry)` / `is_device_entry(entry)` (can live in `const.py` usage or a small helper in this module).
- Options flow uses `OptionsFlowWithConfigEntry` (or `config_entries.OptionsFlow`) with voluptuous schemas.
- Do not block the event loop; validation uses the async coordinator helper.

## Input Dependencies
- Task 3: `const.py` (conf keys, defaults), package skeleton, `translations/en.json` skeleton.
- Task 6: `coordinator.validate_connection` for reachability validation; the device-key/normalization conventions (so discovery data and unique_ids are consistent).

## Output Artifacts
- `config_flow.py` (+ updated `translations/en.json`) consumed by `__init__.py` wiring (Task 9, which calls discovery and sets up entries) and tested in Task 10.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. Distinguish entry kinds by a `CONF_ENTRY_TYPE` (`"hub"` vs `"device"`) stored in `entry.data`, or by presence of `CONF_HUB_ENTRY_ID`. Add `is_hub_entry`/`is_device_entry` helpers.
2. **Hub step**: voluptuous schema `{host: str, port: int=8433, path: str="/ws", (optional) secure: bool=False}` — or accept a full URL. Build the ws URL; call `await Coordinator.validate_connection(hass, ...)`. On `CannotConnect`, `errors["base"]="cannot_connect"`. On success: `await self.async_set_unique_id(f"hub:{host}:{port}")`, `self._abort_if_unique_id_configured()`, `return self.async_create_entry(title=f"rtl_433 ({host})", data={...,"entry_type":"hub"})`.
3. **Discovery step**: `async def async_step_integration_discovery(self, discovery_info)`: extract `hub_entry_id`, `device_key`, `model`. `unique_id=f"{hub_entry_id}:{device_key}"`. `await self.async_set_unique_id(unique_id)`; `self._abort_if_unique_id_configured()`. Store discovery_info on `self`; show `async_step_confirm` form. On confirm, `async_create_entry(title=f"{model} ({device_key})", data={"entry_type":"device", CONF_HUB_ENTRY_ID: hub_entry_id, CONF_DEVICE_KEY: device_key, CONF_MODEL: model})`. Set `self.context["title_placeholders"]` for a nice discovered-card title.
4. **Ignore**: rely on HA's built-in ignore — because we set a unique_id and `_abort_if_unique_id_configured`, when the user dismisses the discovered card HA records a `SOURCE_IGNORE` entry with that unique_id and won't re-prompt. Verify the abort path checks both configured and ignored entries (HA's `_abort_if_unique_id_configured` does).
5. **Options flow**: `async_get_options_flow`. Branch on entry type:
   - hub: schema `{discovery_enabled: bool (default from options/data), availability_timeout: int (default DEFAULT_AVAILABILITY_TIMEOUT)}`.
   - device: schema `{availability_timeout: optional int}`.
   Persist to `entry.options`; the coordinator/entities read effective timeout = device override else hub default.
6. **translations/en.json**: add `config.step.user`, `config.step.confirm` (discovery), `config.error.cannot_connect`, `config.abort.already_configured`, and `options.step.init` text. Keep valid JSON.
7. Do NOT start the coordinator here; the flow only validates. Coordinator lifecycle is Task 9.
8. `ruff check`; commit `feat:`.
</details>
