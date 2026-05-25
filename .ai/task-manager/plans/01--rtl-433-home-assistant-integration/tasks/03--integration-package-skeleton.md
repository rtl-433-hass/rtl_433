---
id: 3
group: "core"
dependencies: []
status: "completed"
created: 2026-05-25
skills:
  - home-assistant
  - python
---
# Integration Package Skeleton (manifest, const, init stub, translations)

## Objective
Create the minimal `custom_components/rtl_433/` package scaffold that all later code tasks build on: a valid `manifest.json`, a `const.py` with shared constants, a minimal `__init__.py` with the setup/unload entry points (stubbed for now), and a `translations/en.json` skeleton. This establishes the import surface and shared constants so later tasks have non-conflicting files to extend.

## Skills Required
- `home-assistant` — manifest schema, integration entry points, translations layout
- `python` — module structure

## Acceptance Criteria
- [ ] `custom_components/rtl_433/manifest.json` is valid and passes hassfest expectations: `domain: "rtl_433"`, `name`, `version`, `codeowners`, `config_flow: true`, `iot_class: "local_push"`, `documentation`/`issue_tracker` URLs, `requirements: []`, `integration_type: "hub"`.
- [ ] `custom_components/rtl_433/const.py` defines: `DOMAIN = "rtl_433"`, default port (`8433`), default WebSocket path (`/ws`), default availability timeout, config keys (host/port/path/discovery-enabled/timeout), the per-device→hub parent reference key, dispatcher signal name template, and discovery source constants usage notes. Include `PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]`.
- [ ] `custom_components/rtl_433/__init__.py` defines `async_setup_entry`/`async_unload_entry` stubs that import without error (real logic added in Task 9). They should distinguish hub vs device entries by entry data shape (leave a clearly marked TODO for the wiring done in Task 9).
- [ ] `custom_components/rtl_433/translations/en.json` exists with a minimal valid structure (`config`, `options` skeleton).
- [ ] `python3 -c "import ast; ast.parse(open(f).read())"` passes for each `.py` file; `manifest.json` and `en.json` are valid JSON.
- [ ] `ruff check custom_components/rtl_433` passes on the created files.
- [ ] A single conventional commit (e.g. `feat: add rtl_433 integration package skeleton`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Domain: `rtl_433`. Use `homeassistant.const.Platform`.
- `const.py` is the single source of truth for keys used across coordinator, config flow, entities, and tests. Name keys clearly: `CONF_HOST`, `CONF_PORT`, `CONF_PATH`, `CONF_DISCOVERY_ENABLED`, `CONF_AVAILABILITY_TIMEOUT`, `CONF_HUB_ENTRY_ID`, `CONF_DEVICE_KEY`, `CONF_MODEL`, etc.
- `DEFAULT_PORT = 8433`, `DEFAULT_PATH = "/ws"`, `DEFAULT_AVAILABILITY_TIMEOUT` = a documented sensible default (e.g. `600` seconds) — not a magic constant elsewhere.
- Provide `SIGNAL_DEVICE_UPDATE = "rtl_433_device_update_{hub_entry_id}_{device_key}"` style template (or a helper) so coordinator and entities agree on dispatcher keys.

## Input Dependencies
None (Phase 1). Sits under `custom_components/rtl_433/` but only touches `manifest.json`, `const.py`, `__init__.py`, `translations/en.json` — disjoint from Task 4 which only writes `device_library/` + `docs/`.

## Output Artifacts
- The package import surface and shared constants consumed by Tasks 5, 6, 7, 8, 9, 10.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. `manifest.json` minimum keys for hassfest:
   ```json
   {
     "domain": "rtl_433",
     "name": "rtl_433",
     "codeowners": ["@deviantintegral"],
     "config_flow": true,
     "documentation": "https://github.com/rtl-433-hass/rtl_433",
     "iot_class": "local_push",
     "issue_tracker": "https://github.com/rtl-433-hass/rtl_433/issues",
     "integration_type": "hub",
     "requirements": [],
     "version": "0.1.0"
   }
   ```
   Keep keys alphabetically sorted (hassfest enforces ordering). Confirm `version` is present (required for custom integrations).
2. `const.py`: define `DOMAIN`, `PLATFORMS`, all `CONF_*` keys, defaults, and the dispatcher signal helper. Add a `LOGGER = logging.getLogger(__package__)`.
3. `__init__.py`: provide:
   ```python
   async def async_setup_entry(hass, entry): ...  # TODO Task 9: branch hub vs device
   async def async_unload_entry(hass, entry): ...  # TODO Task 9
   ```
   Make them import-safe (return True / minimal) so the package imports cleanly. Clearly comment that real wiring lands in Task 9 to avoid merge confusion (Task 9 is in a later phase, so it will edit this file then).
4. `translations/en.json`: minimal:
   ```json
   { "config": { "step": {}, "abort": {}, "error": {} }, "options": { "step": {} } }
   ```
   (Task 7 fills in real step text.)
5. Run `ruff check custom_components/rtl_433` and `python3 -c "import ast; ..."` to validate.
6. Commit with a conventional `feat:` message.
</details>
