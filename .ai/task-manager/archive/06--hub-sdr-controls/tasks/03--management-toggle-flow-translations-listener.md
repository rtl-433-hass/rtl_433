---
id: 3
group: "config-flow"
dependencies: [1, 2]
status: "completed"
created: 2026-05-27
skills:
  - home-assistant
  - python
---
# Management toggle: config/options flow, translations, reload-on-change, wiring

## Objective
Add the per-hub **"Manage rtl_433 settings from Home Assistant"** toggle (default on) to
both the initial connection-details form (`async_step_user`) and the hub options step
(`async_step_hub`), label both via `translations/en.json`, wire the effective value into
the coordinator (`manage_settings`), and extend `_async_update_listener` so that
**changing the toggle reloads the entry** (the entity set changes) while
`discovery`/`timeout` changes keep applying live (no reload).

## Skills Required
- `home-assistant` — config/options flow schemas, `translations/en.json` for
  `config.step.user` and `options.step.hub`, `async_reload`, options-over-data resolver.
- `python` — small resolver helper and listener branch.

## Acceptance Criteria
- [ ] `STEP_USER_SCHEMA` in `config_flow.py` includes
      `vol.Optional(CONF_MANAGE_SETTINGS, default=True): bool`, and the created hub
      entry's `data` records the chosen value.
- [ ] `async_step_hub`'s schema includes `CONF_MANAGE_SETTINGS` with its default read
      options-over-data (`entry.options.get(CONF_MANAGE_SETTINGS,
      entry.data.get(CONF_MANAGE_SETTINGS, DEFAULT_MANAGE_SETTINGS))`).
- [ ] `translations/en.json` gains the toggle's `data` label **and** a
      `data_description` (short explanation) under **both** `config.step.user` and
      `options.step.hub`.
- [ ] `__init__.py` resolves the effective toggle with a `_hub_manage_settings(entry)`
      helper (options > data > default, mirroring `_hub_discovery_enabled`) and passes
      `manage_settings=...` to the `Rtl433Coordinator(...)` constructor.
- [ ] `async_setup_entry` awaits `coordinator.async_load_desired_state()` (so the Store
      is loaded / cleared per the toggle) before/around `coordinator.async_start()`
      (whichever the coordinator API from Task 2 expects — if `async_start` already
      calls it, do not double-call).
- [ ] `_async_update_listener` compares the toggle's **old vs new effective value**;
      when it changed, it calls `await hass.config_entries.async_reload(entry.entry_id)`
      and returns (no live-apply needed because the reload rebuilds everything). When it
      did not change, it keeps the existing live-apply of `discovery`/`timeout` and does
      **not** reload.
- [ ] `uv run ruff check custom_components/rtl_433` passes;
      `uv run pytest tests/test_config_flow.py` passes.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- The listener must detect the toggle change without persisting extra bookkeeping. The
  running coordinator already holds the *previous* effective value as
  `coordinator.manage_settings`; compare that against `_hub_manage_settings(entry)`
  (the new effective value) to decide whether to reload. Read the coordinator from
  `hass.data[DOMAIN][entry.entry_id]` as the listener already does, and guard `None`.
- Do **not** bump config-flow `VERSION` (the option is additive; the plan says no bump).
- Keep `CONF_MANAGE_SETTINGS` imported from `.const` (Task 1), not re-defined.

## Input Dependencies
- Task 1: `CONF_MANAGE_SETTINGS`, `DEFAULT_MANAGE_SETTINGS`.
- Task 2: coordinator `manage_settings` constructor parameter and
  `async_load_desired_state()`.

## Output Artifacts
- `config_flow.py`, `translations/en.json`, and `__init__.py` updated. No new files.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

**config_flow.py** — extend the user schema and import the const:
```python
from .const import ( ... CONF_MANAGE_SETTINGS, DEFAULT_MANAGE_SETTINGS, ... )

STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    vol.Required(CONF_PATH, default=DEFAULT_PATH): str,
    vol.Optional(CONF_SECURE, default=False): bool,
    vol.Optional(CONF_MANAGE_SETTINGS, default=DEFAULT_MANAGE_SETTINGS): bool,
})
```
In `async_step_user`, read `manage = user_input[CONF_MANAGE_SETTINGS]` and add
`CONF_MANAGE_SETTINGS: manage` to the `data=` dict of `async_create_entry`.

In `async_step_hub`, add to the schema:
```python
manage_default = entry.options.get(
    CONF_MANAGE_SETTINGS, entry.data.get(CONF_MANAGE_SETTINGS, DEFAULT_MANAGE_SETTINGS)
)
... vol.Required(CONF_MANAGE_SETTINGS, default=manage_default): bool ...
```

**translations/en.json** — add under `config.step.user.data` /
`config.step.user.data_description` and `options.step.hub.data` /
`options.step.hub.data_description`:
```json
"manage_settings": "Manage rtl_433 settings from Home Assistant"
```
description (both steps, wording can be tuned):
```json
"manage_settings": "When on, Home Assistant adopts this server's current SDR settings and re-applies them on every reconnect, and adds controls for frequency, gain, and more. Turn off to leave the server's own configuration untouched."
```

**__init__.py** — add the resolver near the other `_hub_*` resolvers:
```python
def _hub_manage_settings(entry: ConfigEntry) -> bool:
    return bool(entry.options.get(
        CONF_MANAGE_SETTINGS,
        entry.data.get(CONF_MANAGE_SETTINGS, DEFAULT_MANAGE_SETTINGS),
    ))
```
Pass `manage_settings=_hub_manage_settings(entry)` into the `Rtl433Coordinator(...)`
construction. If the Task 2 `async_start()` already awaits `async_load_desired_state()`,
nothing else is needed here; otherwise `await coordinator.async_load_desired_state()`
right before `await coordinator.async_start()`.

**Listener reload-on-change** — replace the body of `_async_update_listener`:
```python
coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
if coordinator is None:
    return
new_manage = _hub_manage_settings(entry)
if new_manage != coordinator.manage_settings:
    # The entity set changes (controls appear/disappear); a reload rebuilds it.
    await hass.config_entries.async_reload(entry.entry_id)
    return
coordinator.discovery_enabled = _hub_discovery_enabled(entry)
coordinator.availability_timeout = _hub_availability_timeout(entry)
LOGGER.debug(...)
```

Run `uv run ruff check custom_components/rtl_433` and
`uv run pytest tests/test_config_flow.py` before finishing.
</details>
