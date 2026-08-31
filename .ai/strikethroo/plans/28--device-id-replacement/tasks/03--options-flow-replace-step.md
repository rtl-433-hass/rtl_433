---
id: 3
group: "user-interface"
dependencies: [2]
status: "completed"
created: 2026-08-31
skills:
  - home-assistant-config-flow
  - json
---
# Options-flow "Replace device" step and translations

## Objective

Give the user a two-pick path to recover from a battery-swap id change: add a
**Replace device** entry to the hub options menu, a step to choose the device to
keep, and a step to choose the newly-seen device to adopt it onto — then call the
shared re-key helper.

## Skills Required

`home-assistant-config-flow` — multi-step options flows, `voluptuous` schemas and
`SelectSelector` pickers. `json` — the matching strings in
`translations/en.json`.

## Acceptance Criteria

- [ ] `async_step_init` in `custom_components/rtl_433/options_flow.py` offers a
      fourth menu option, `replace`, alongside `hub` / `device` / `mappings`.
- [ ] `async_step_replace` lists the devices in `entry.data[CONF_DEVICES]` as the
      "device to keep" picker, and aborts with `no_devices` when the map is empty
      (matching `async_step_device`).
- [ ] `async_step_replace_target` lists candidates drawn from the **union** of
      `entry.data[CONF_DEVICES]` keys and the coordinator's seen-device keys
      (`coordinator.devices`), minus the device chosen on the previous step.
- [ ] Candidate labels show model and key; candidates whose model matches the
      chosen device sort first.
- [ ] Submitting calls `async_replace_device` from
      `custom_components/rtl_433/device_replace.py`; a `DeviceReplaceError` is
      rendered as a form error, not a traceback.
- [ ] The flow finishes without writing `entry.options` (the helper persists into
      `entry.data` and reloads).
- [ ] `translations/en.json` carries the new menu label, both step titles /
      descriptions / field labels, and any new error key.
- [ ] `uv run pytest tests/` passes.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements

- Files: `custom_components/rtl_433/options_flow.py`,
  `custom_components/rtl_433/translations/en.json`.
- Existing imports in `options_flow.py` already include `vol`, `SelectSelector`,
  `SelectSelectorConfig`, `SelectSelectorMode`, `SelectOptionDict`, and the
  device constants.
- The coordinator instance is reachable from `hass.data[DOMAIN]` keyed by
  `entry.entry_id`; check how `options_flow.py`'s `_registry` /
  `_device_commodity_default` helpers already reach runtime state and follow the
  same access pattern rather than inventing a new one.

## Input Dependencies

- Task 2: `async_replace_device` and `DeviceReplaceError` in
  `custom_components/rtl_433/device_replace.py`.

## Output Artifacts

- Two new options-flow steps plus the menu entry.
- Translation strings for both steps.

## Implementation Notes

<details>
<summary>Step-by-step implementation</summary>

1. **Menu.** In `async_step_init` (around line 97):

   ```python
   return self.async_show_menu(
       step_id="init",
       menu_options=["hub", "device", "mappings", "replace"],
   )
   ```

   Put `replace` last — it is the rarest action and the most consequential.

2. **Carry state between steps.** The class already uses class-level attributes
   for cross-step state (`_device_key`, `_calibration_commodity`,
   `_motion_clear_delay`). Add one in the same style:

   ```python
   # The device chosen on the replace step, carried into replace_target.
   _replace_old_key: str = ""
   ```

3. **`async_step_replace`** — model it closely on `async_step_device` (line 318),
   which is deliberately picker-only so the next form's defaults derive from the
   choice:

   ```python
   async def async_step_replace(
       self, user_input: dict[str, Any] | None = None
   ) -> ConfigFlowResult:
       """Pick the device to keep — the one whose history should survive."""
       devices: dict[str, Any] = dict(self.config_entry.data.get(CONF_DEVICES, {}))
       if not devices:
           return self.async_abort(reason="no_devices")

       if user_input is not None:
           self._replace_old_key = user_input[CONF_DEVICE]
           return await self.async_step_replace_target()

       options = [
           SelectOptionDict(value=key, label=self._device_label(key, rec))
           for key, rec in sorted(devices.items())
       ]
       return self.async_show_form(
           step_id="replace",
           data_schema=vol.Schema(
               {
                   vol.Required(CONF_DEVICE): SelectSelector(
                       SelectSelectorConfig(
                           options=options, mode=SelectSelectorMode.DROPDOWN
                       )
                   )
               }
           ),
       )
   ```

   Reuse the existing `_device_label` (line 304) so labels stay consistent with
   the settings picker.

4. **`async_step_replace_target`** — the candidate list is the important part.
   Union the devices map with the coordinator's seen keys, because the docs
   recommend turning discovery off in urban areas, and with discovery off the
   replacement never gets a registered row — but the coordinator has still heard
   it:

   ```python
   async def async_step_replace_target(
       self, user_input: dict[str, Any] | None = None
   ) -> ConfigFlowResult:
       """Pick the new identity to adopt onto the kept device."""
       old_key = self._replace_old_key
       devices: dict[str, Any] = dict(self.config_entry.data.get(CONF_DEVICES, {}))
       errors: dict[str, str] = {}

       if user_input is not None:
           try:
               await async_replace_device(
                   self.hass, self.config_entry, old_key, user_input[CONF_DEVICE]
               )
           except DeviceReplaceError:
               errors["base"] = "replace_failed"
           else:
               return self.async_create_entry(title="", data=dict(self.config_entry.options))
       ...
   ```

   Note the finish path: the helper already wrote `entry.data` and reloaded, so
   the step must not clobber options — returning the entry's existing options
   unchanged is the least-surprising way to close a flow that persisted
   elsewhere. Check how `_write_device_record` closes its flow and mirror it if
   it differs.

   Build the candidate list as:

   ```python
   coordinator = ...  # same access pattern the module already uses
   seen = set(getattr(coordinator, "devices", {}) or {})
   candidates = (set(devices) | seen) - {old_key}
   ```

   Guard for the coordinator being absent (the options flow can be opened while
   the entry is not loaded) — fall back to the devices map alone.

   Sort same-model candidates first, since a battery swap keeps the model:

   ```python
   old_model = devices.get(old_key, {}).get(CONF_MODEL, "")
   def _sort_key(key: str) -> tuple[int, str]:
       model = devices.get(key, {}).get(CONF_MODEL, "")
       return (0 if model and model == old_model else 1, key)
   ```

   For a candidate with no record in the devices map, derive its label from the
   coordinator's normalized event (`coordinator.devices[key].model` or similar)
   or fall back to the bare key — do not crash on a missing record.

   Abort with a dedicated reason (e.g. `no_replacement_candidates`) when the
   candidate set is empty; that is a real state (a single-device hub) and a bare
   empty dropdown is a dead end.

5. **Translations.** Add to `options.step` in
   `custom_components/rtl_433/translations/en.json`, and add `replace` to
   `options.step.init.menu_options`:

   ```json
   "replace": {
     "title": "Replace device",
     "description": "Some sensors draw a new transmitter id when their batteries are changed, so they appear as a new device. Pick the device you want to keep — its history, calibration and automations will be moved onto the new id.",
     "data": { "device": "Device to keep" },
     "data_description": { "device": "The existing device whose history you want to preserve." }
   },
   "replace_target": {
     "title": "Replace device",
     "description": "Pick the newly discovered device that is really the same hardware. Its duplicate entities are removed and the device you kept takes over its id.",
     "data": { "device": "New device" },
     "data_description": { "device": "The device that appeared after the batteries were changed." }
   }
   ```

   Add `"replace": "Replace device"` to `menu_options`, and to `options.error`
   add `"replace_failed": "That device could not be replaced. ..."`. Add any new
   abort reason to `options.abort` alongside the existing `no_devices`.

   Validate the file parses: `python3 -c "import json; json.load(open('custom_components/rtl_433/translations/en.json'))"`.

6. Run `uv run pytest tests/`. Task 4 owns the flow's test coverage; here just
   confirm nothing existing broke.
</details>
