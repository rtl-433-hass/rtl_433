---
id: 2
group: "config-flow"
dependencies: [1]
status: "completed"
created: "2026-06-01"
skills:
  - python
---
# Add manage/discovery toggles and an initial-frequency field to both add flows

## Objective
Give the manual add flow (`async_step_user`) a "Discover new devices" checkbox and an optional initial center-frequency field (MHz), and give the Supervisor discovery confirm step (`async_step_hassio_confirm`) the "Manage settings" + "Discover new devices" checkboxes and the same frequency field. Persist the chosen values into `entry.data`, dropping the frequency when `manage_settings` is off. Add the matching user-facing strings to `en.json`.

## Skills Required
- `python` — Home Assistant config-flow (`voluptuous` schemas, `NumberSelector`), JSON translations.

## Acceptance Criteria
- [ ] `STEP_USER_SCHEMA` gains `CONF_DISCOVERY_ENABLED` (default `True`) and an optional `CONF_INITIAL_FREQUENCY` field (MHz, `NumberSelector` BOX mode, min 0).
- [ ] `async_step_user` persists `CONF_DISCOVERY_ENABLED`; it persists `CONF_INITIAL_FREQUENCY` only when `manage_settings` is true and a value was entered (otherwise the key is absent).
- [ ] `async_step_hassio_confirm` shows a form with `CONF_MANAGE_SETTINGS` (default `True`), `CONF_DISCOVERY_ENABLED` (default `True`), and the optional `CONF_INITIAL_FREQUENCY` field; it preserves the `addon`/`host`/`port` placeholders and the `cannot_connect` re-show behaviour, and persists the chosen values (frequency dropped when `manage_settings` is off).
- [ ] `en.json` `config.step.user` gains `data`/`data_description` for `discovery_enabled` and `initial_frequency`; `config.step.hassio_confirm` gains `data`/`data_description` for `manage_settings`, `discovery_enabled`, and `initial_frequency`.
- [ ] Linting passes; the reconfigure flow is unchanged.

## Technical Requirements
- Files: `custom_components/rtl_433/config_flow.py`, `custom_components/rtl_433/translations/en.json`.
- Imports from `const`: `CONF_DISCOVERY_ENABLED` (already imported), `CONF_INITIAL_FREQUENCY` (added in Task 1), `DEFAULT_MANAGE_SETTINGS`.
- Use `NumberSelector(NumberSelectorConfig(min=0, step="any", mode=NumberSelectorMode.BOX, unit_of_measurement="MHz"))` for the frequency field (helpers already imported).

## Input Dependencies
- Task 1: `CONF_INITIAL_FREQUENCY` constant and the MHz unit decision.

## Output Artifacts
- Both add flows collecting and persisting the three new values.
- Translation strings for the new fields.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

**`config_flow.py` — imports:** add `CONF_INITIAL_FREQUENCY` to the `from .const import (...)` block. `CONF_DISCOVERY_ENABLED` and `DEFAULT_MANAGE_SETTINGS` are already imported.

**Shared frequency field helper** (define a module-level constant schema fragment or build inline in both places). Build the field as `vol.Optional(CONF_INITIAL_FREQUENCY)` mapped to a `NumberSelector` so blank means "not set":
```python
_FREQUENCY_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=0, step="any", mode=NumberSelectorMode.BOX, unit_of_measurement="MHz")
)
```

**`STEP_USER_SCHEMA`** — extend to:
```python
STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_PATH, default=DEFAULT_PATH): str,
        vol.Optional(CONF_SECURE, default=False): bool,
        vol.Optional(CONF_MANAGE_SETTINGS, default=DEFAULT_MANAGE_SETTINGS): bool,
        vol.Optional(CONF_DISCOVERY_ENABLED, default=True): bool,
        vol.Optional(CONF_INITIAL_FREQUENCY): _FREQUENCY_SELECTOR,
    }
)
```

**`async_step_user`** — after the existing validate/duplicate checks, build the entry `data`:
- Always include `CONF_DISCOVERY_ENABLED: user_input[CONF_DISCOVERY_ENABLED]`.
- Compute `freq = user_input.get(CONF_INITIAL_FREQUENCY)`. Add `CONF_INITIAL_FREQUENCY: freq` to `data` **only if** `manage_settings and freq is not None`.
  Suggested:
  ```python
  data = {
      CONF_HOST: host,
      CONF_PORT: port,
      CONF_PATH: path,
      CONF_SECURE: secure,
      CONF_MANAGE_SETTINGS: manage_settings,
      CONF_DISCOVERY_ENABLED: user_input[CONF_DISCOVERY_ENABLED],
  }
  freq = user_input.get(CONF_INITIAL_FREQUENCY)
  if manage_settings and freq is not None:
      data[CONF_INITIAL_FREQUENCY] = float(freq)
  return self.async_create_entry(title=f"rtl_433 ({host})", data=data)
  ```

**`async_step_hassio_confirm`** — replace `data_schema=vol.Schema({})` (both the submit-branch error re-show and the final show) with a populated schema:
```python
confirm_schema = vol.Schema(
    {
        vol.Optional(CONF_MANAGE_SETTINGS, default=DEFAULT_MANAGE_SETTINGS): bool,
        vol.Optional(CONF_DISCOVERY_ENABLED, default=True): bool,
        vol.Optional(CONF_INITIAL_FREQUENCY): _FREQUENCY_SELECTOR,
    }
)
```
On submit (`user_input is not None`): keep the existing `validate_connection` call and the `cannot_connect` re-show (pass `data_schema=confirm_schema` instead of the empty schema, keep `description_placeholders=placeholders`). On success, read `manage_settings = user_input[CONF_MANAGE_SETTINGS]`, build `data` from `disc[...]` plus the three new values, applying the same "drop frequency when manage off" rule as the user flow. Replace the hard-coded `CONF_MANAGE_SETTINGS: DEFAULT_MANAGE_SETTINGS` with the submitted value. The final `async_show_form` must also pass `data_schema=confirm_schema`.

**`en.json`** — under `config.step.user`:
- `data`: add `"discovery_enabled": "Discover new devices"`, `"initial_frequency": "Initial frequency (MHz)"`.
- `data_description`: add
  `"discovery_enabled": "When on, newly observed devices on this server are added automatically as nested devices. You can change this later in the hub options."`
  `"initial_frequency": "Optional. Set the receiver's center frequency in MHz (for example 433.92) at setup. Leave blank to keep the server's current frequency. Only applies when \"Manage rtl_433 settings from Home Assistant\" is on."`

Under `config.step.hassio_confirm` add a `data` and `data_description` object mirroring the `user` step entries for `manage_settings`, `discovery_enabled`, and `initial_frequency` (reuse the `manage_settings` wording already used in `config.step.user.data_description`). Keep the existing `title` and `description`.

Validate JSON is well-formed (e.g. `python -m json.tool`).
</details>
