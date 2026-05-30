---
id: 4
group: "ui"
dependencies: [1, 2]
status: "completed"
created: 2026-05-28
skills:
  - python
  - home-assistant
---
# Options-flow "Device mappings" step (native YAML editor) + translations

## Objective
Add a "Device mappings" entry to the hub options menu that renders Home Assistant's native YAML editor (`ObjectSelector`) pre-filled with the hub's current mappings, validates the parsed object on submit (rejecting with a per-field inline error), and on success normalizes + stores it in `entry.data[CONF_USER_MAPPINGS]` so the update listener reloads the hub.

## Skills Required
- `python`, `home-assistant`: options flow steps, `ObjectSelector`, `async_update_entry`, translations.

## Acceptance Criteria
- [ ] The options menu (`async_step_init`) includes a new `mappings` option.
- [ ] `async_step_mappings` renders a single `ObjectSelector` field defaulting to the current `entry.data.get(CONF_USER_MAPPINGS) or {}`.
- [ ] On submit: run `validate_user_mappings` (Task 1); if problems, re-show the form with `errors={"base": "invalid_mappings"}` and the joined problems surfaced (via `description_placeholders` or per-field error text); nothing is stored.
- [ ] On valid submit: store `normalize_overrides(user_input[field])` into `entry.data[CONF_USER_MAPPINGS]` via `async_update_entry(data={**entry.data, CONF_USER_MAPPINGS: ...})`; finish the flow.
- [ ] `entry.options` and `entry.data[CONF_DEVICES]` are left untouched by this step.
- [ ] `translations/en.json` has the menu label, step title/description, field label, and the `invalid_mappings` error message.
- [ ] `ruff` passes; existing config-flow tests still pass.

## Technical Requirements
- `from homeassistant.helpers.selector import ObjectSelector`.
- Storing into `entry.data` (not options) is deliberate — see plan Background/Decision Log. Use `self.hass.config_entries.async_update_entry(self.config_entry, data=...)` then `return self.async_create_entry(title="", data=self.config_entry.options)` (or the flow's standard finish) WITHOUT overwriting options.

## Input Dependencies
- Task 1: `validate_user_mappings`, `normalize_overrides`.
- Task 2: `CONF_USER_MAPPINGS`, the per-entry merge + reload-on-change listener.

## Output Artifacts
- The UI editing surface. Exercised by Task 5 tests and documented by Task 6.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

Read `config_flow.py` (`Rtl433OptionsFlow`, `async_step_init`, `async_step_hub`, `_write_device_record`) and `translations/en.json` (`options` section).

**Imports:** add `ObjectSelector` (and `ObjectSelectorConfig` if needed) to the selector import block; import `validate_user_mappings, normalize_overrides, CONF_USER_MAPPINGS` (const) appropriately (`CONF_USER_MAPPINGS` from `.const`).

**Menu:** in `async_step_init`, change `menu_options=["hub", "device"]` to `["hub", "device", "mappings"]`.

**Step:**
```
async def async_step_mappings(self, user_input=None):
    errors = {}
    placeholders = {}
    if user_input is not None:
        raw = user_input.get(CONF_USER_MAPPINGS) or {}
        problems = validate_user_mappings(raw)
        if problems:
            errors["base"] = "invalid_mappings"
            placeholders["problems"] = "; ".join(problems)
        else:
            normalized = normalize_overrides(raw)
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_USER_MAPPINGS: normalized},
            )
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
    current = self.config_entry.data.get(CONF_USER_MAPPINGS) or {}
    schema = vol.Schema({
        vol.Optional(CONF_USER_MAPPINGS, default=current): ObjectSelector(),
    })
    return self.async_show_form(
        step_id="mappings",
        data_schema=schema,
        errors=errors,
        description_placeholders=placeholders,
    )
```
Notes:
- `ObjectSelector()` with no config renders the ha-yaml-editor; the `default=current` makes it show the existing mappings as YAML.
- Returning `async_create_entry(data=dict(self.config_entry.options))` finishes the options flow without changing options (we already wrote `entry.data`). Confirm this does not clobber options — passing the *current* options back is a no-op. If the running HA version errors on an empty/identical options write, instead use `return self.async_abort(reason="mappings_saved")` after a successful `async_update_entry`; check existing patterns and pick the one that cleanly closes the dialog. Prefer `async_create_entry` to stay consistent with the other steps.
- Validation runs on the parsed object (the editor already blocked invalid YAML syntax client-side).

**translations/en.json** — under `options`:
- `step.init.menu_options.mappings`: "Device mappings"
- `step.mappings.title`: "Device mappings"
- `step.mappings.description`: short text explaining YAML overrides; mention errors are reported on save. You can reference `{problems}` here so the specific problems show, e.g. "... {problems}".
- `step.mappings.data.user_mappings`: "Mappings (YAML)"
- `error.invalid_mappings`: "Invalid mappings: {problems}" (if the HA version supports placeholders in error strings; otherwise put `{problems}` in the description and keep the error generic). Match the existing en.json structure exactly (keys, nesting) — read it first.

Run `ruff` + ensure `tests/test_config_flow.py` still passes (you may need to add the new step there, but full test coverage is Task 5).
</details>
