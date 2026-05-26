---
id: 2
group: "flows"
dependencies: [1]
status: "completed"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# Config & options flow rework (remove per-device discovery, hub options menu)

## Objective
Reshape the config flow to the single-hub model: bump the config-entry `VERSION` to 2 (triggers migration), remove the per-device integration-discovery/confirm steps, and rework the OptionsFlow into a menu with a hub-settings step and a device-settings step (per-device availability-timeout override written into the hub's `entry.data["devices"]` map). Update the translation strings to match.

## Skills Required
- `python`: rewrite flow classes and voluptuous schemas.
- `home-assistant`: config/options flow APIs, `OptionsFlowWithReload`/menus, translations.

## Acceptance Criteria
- [ ] `config_flow.py` sets `VERSION = 2`.
- [ ] `async_step_integration_discovery` and `async_step_confirm` are removed; the hub `async_step_user` is unchanged in behavior.
- [ ] The OptionsFlow is a menu: a hub step (discovery toggle + default availability timeout, persisted to `entry.options`) and a device step (pick a known device from `entry.data["devices"]`, set/clear its `timeout_override`, written into `entry.data["devices"]` via `async_update_entry(data=...)`).
- [ ] The separate device OptionsFlow class is removed; `is_device_entry` is removed (everything is a hub now).
- [ ] `translations/en.json` drops `config.flow_title` and the `confirm` step, rewords `already_configured` to refer to the *server*, and adds the options menu/device-step strings.
- [ ] `uv run pytest tests/test_config_flow.py` may fail here (tests are rewritten in Task 5); the module must import cleanly and `uv run python -c "import custom_components.rtl_433.config_flow"` succeeds.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File `custom_components/rtl_433/config_flow.py`.
- File `custom_components/rtl_433/translations/en.json`.
- Uses `CONF_DEVICES`, `DEVICE_TIMEOUT_OVERRIDE`, `CONF_MODEL` from Task 1.

## Input Dependencies
- Task 1: `CONF_DEVICES`, `DEVICE_TIMEOUT_OVERRIDE`.

## Output Artifacts
- A single-hub config flow at `VERSION = 2` and a menu-based hub OptionsFlow consumed by users; the device-step override format is read by Task 3's `effective_timeout_resolver` and written into the same map Task 3/4 use.

## Implementation Notes

<details>
<summary>Detailed steps</summary>

**`config_flow.py`:**

1. Set `VERSION = 2` on `Rtl433ConfigFlow`.
2. Keep `async_step_user` exactly as today (host/port/path/secure, `validate_connection`, hub unique_id `hub:{host}:{port}`, create hub entry). The created entry data keeps `CONF_ENTRY_TYPE: ENTRY_TYPE_HUB` for continuity (harmless; the runtime no longer branches on it) **or** you may drop it — either is fine, but if you keep writing it, do NOT branch on it at runtime.
3. Delete `async_step_integration_discovery`, `async_step_confirm`, the discovery transient state (`self._discovery_*`), and the `is_device_entry` helper. Remove now-unused imports (`SOURCE_INTEGRATION_DISCOVERY` is referenced from `__init__.py`, not here).
4. Replace `async_get_options_flow` so it always returns the hub options flow (no device branch).
5. Rework the options flow into a menu:
   - `async_step_init` shows `self.async_show_menu(step_id="init", menu_options=["hub", "device"])`.
   - `async_step_hub`: same fields as today's hub options (`CONF_DISCOVERY_ENABLED` bool default from options→data→True; `CONF_AVAILABILITY_TIMEOUT` int ≥1 default from options→data→`DEFAULT_AVAILABILITY_TIMEOUT`). On submit `return self.async_create_entry(title="", data={...})` (writes `entry.options`).
   - `async_step_device`: build a selector of known devices from `self.config_entry.data.get(CONF_DEVICES, {})` (label `"{model} ({device_key})"`, value `device_key`). First form picks a device; second form sets an optional timeout override (int ≥1; leaving blank clears it). Persist by reading `dict(self.config_entry.data)`, updating `data[CONF_DEVICES][device_key][DEVICE_TIMEOUT_OVERRIDE]` (delete the key or set `None` to clear), and calling `self.hass.config_entries.async_update_entry(self.config_entry, data=data)`. Then `return self.async_create_entry(title="", data=self.config_entry.options)` (no options change) or `async_abort(reason="...")` — keep it simple: finish the flow without altering `entry.options`.
   - If `entry.data[CONF_DEVICES]` is empty, the device step should show an informational abort (`reason="no_devices"`).
   - Prefer using a single-select selector (`SelectSelector`) for the device list; a plain `vol.In({...})` mapping is acceptable.
6. Keep `is_hub_entry` if other modules still import it, OR remove it and update importers — coordinate with Task 3 (Task 3 removes the hub/device branch in `diagnostics.py`). Simplest: remove `is_hub_entry`/`is_device_entry` and let Task 3 stop importing them.

**`translations/en.json`:**

7. Remove the top-level `config.flow_title` and the `config.step.confirm` block.
8. Reword `config.abort.already_configured` from "This device is already configured." to e.g. "This rtl_433 server is already configured."
9. Replace the single `options.step.init` block with: an `init` menu (with `menu_options` titles for `hub` and `device`), a `hub` step (reuse the existing discovery toggle + availability timeout strings), and a `device` step (device picker + timeout override; blank clears). Add an `options.abort.no_devices` string ("No devices have been discovered yet.").
10. Keep `error.cannot_connect` and the `issues.server_unreachable` block unchanged.

**Note on coordination:** `effective_timeout_resolver` (wired in Task 3) reads `entry.data[CONF_DEVICES][device_key][DEVICE_TIMEOUT_OVERRIDE]`. Ensure the device step writes exactly that shape.

**Validation:** `uv run python -c "import custom_components.rtl_433.config_flow"`; `python -m json.tool custom_components/rtl_433/translations/en.json >/dev/null` to confirm valid JSON.
</details>
