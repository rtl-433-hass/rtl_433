---
id: 4
group: "core-scaffold"
dependencies: [1, 2]
status: "completed"
created: 2026-07-06
skills:
  - home-assistant
  - python
complexity_score: 6
complexity_notes: "Reduces a ~20-module integration to a Bronze-tier minimal subset while preserving the frozen identity ABI; requires judgement about what to strip and what the config flow/coordinator minimally need."
---
# Scaffold the Minimal Core Integration (Bronze Quality Scale)

## Objective
Scaffold the smallest reviewable `rtl_433` integration in the core fork at `~/github.com/deviantintegral/core-2/homeassistant/components/rtl_433/`: `manifest.json`, `const.py`, `__init__.py` with one `DataUpdateCoordinator`, `config_flow.py`, the `sensor` platform only, and a `quality_scale.yaml` targeting Bronze. It must reproduce the frozen compatibility contract's `unique_id` and device-identifier formats exactly. (Tests are Task 5.)

## Skills Required
- **home-assistant**: integration structure, config flow, `DataUpdateCoordinator`, entity platforms, manifest, quality scale.
- **python**: async integration code over the `pyrtl_433` library.

## Acceptance Criteria
- [ ] `homeassistant/components/rtl_433/` exists in the fork on branch `rtl_433-integration` containing exactly: `manifest.json`, `const.py`, `__init__.py`, `config_flow.py`, `sensor.py`, `quality_scale.yaml` (no other platform modules).
- [ ] `manifest.json` declares domain `rtl_433`, `config_flow: true`, `iot_class: local_push`, `integration_type: hub`, single codeowner, `requirements: ["pyrtl_433==0.1.1"]`, and does NOT pin a conflicting `aiohttp` version.
- [ ] All intra-package imports are relative (`from .const import ...`); zero references to `custom_components`.
- [ ] Entity `unique_id` and device `identifiers` use the exact templates from `COMPATIBILITY_CONTRACT.md`.
- [ ] `quality_scale.yaml` targets Bronze and enumerates the Bronze rules with their status.
- [ ] Only the `sensor` platform is wired in the coordinator/`PLATFORMS`; event/binary_sensor/number/select/switch and all non-platform modules are excluded.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Base the code on the current `custom_components/rtl_433/` modules, copying verbatim where the flat layout and relative imports allow, then stripping to the sensor-only subset.
- The coordinator wraps `pyrtl_433`'s async client (local_push websocket stream); no `async_add_executor_job` wrappers are needed because the library is fully async.
- Pin `aiohttp` only to a compatible minimum, deferring the upper bound to core's central management (or omit it if `pyrtl_433` already constrains it).

## Input Dependencies
- Task 1: the configured core fork/branch to write into.
- Task 2: `COMPATIBILITY_CONTRACT.md` — the exact identity formats to reproduce.

## Output Artifacts
- The minimal integration package under `homeassistant/components/rtl_433/` in the fork — consumed by Task 5 (tests + validation).

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

- **Strip aggressively.** Start from the current modules but include only what a sensor-only Bronze integration needs: config entry setup/unload, one coordinator, the sensor entity, and the const/manifest. Remove imports of stripped modules (event, device_trigger, calibration, repairs, options_flow, device_library, mapping, hub_settings, sdr_settings, number/select/switch/binary_sensor).
- **Preserve identity ABI.** The sensor entity's `unique_id` must be `f"{hub_entry_id}:{device_key}:{object_suffix}"` and the device `identifiers` must be `{(DOMAIN, entry.entry_id)}` for the hub device and `{(DOMAIN, f"{hub_entry_id}:{device_key}")}` for per-device — copied from the contract, not reinvented.
- **manifest.json** shape:
  ```json
  {
    "domain": "rtl_433",
    "name": "rtl_433",
    "codeowners": ["@deviantintegral"],
    "config_flow": true,
    "documentation": "https://www.home-assistant.io/integrations/rtl_433",
    "integration_type": "hub",
    "iot_class": "local_push",
    "quality_scale": "bronze",
    "requirements": ["pyrtl_433==0.1.1"]
  }
  ```
  (Use the core docs URL, not the GitHub repo URL, per core convention.)
- **config_flow.py**: keep only the minimal user step needed to establish the connection to the rtl_433 host, plus `validate_connection` via the pyrtl_433 client. Drop options flow, reconfigure, and discovery steps for this PR.
- **__init__.py**: `async_setup_entry` creating the coordinator, `async_forward_entry_setups(entry, [Platform.SENSOR])`, and `async_unload_entry`. Include `async_migrate_entry` only if the contract requires the core build to accept migrated entries at load — otherwise keep migration in the HACS build and have core tolerate the current `version=2` entries. Prefer the minimal path: set the same `VERSION`/`MINOR_VERSION` constants so core reads existing entries without migrating down.
- **quality_scale.yaml**: enumerate Bronze rules (config-flow, test-before-setup, unique-config-entry, runtime-data, etc.) with `status` markers; mark not-yet-applicable higher-tier rules as `todo`/`exempt` as appropriate. Model it on an existing core integration's `quality_scale.yaml` for exact rule keys.
- **Do not** run `git commit`/`push` unless the workflow's hooks direct it; leave the working tree staged/ready. Opening the PR is a later human step.
- Validation (hassfest, imports, pytest) is Task 5; this task ends when the package is written and internally consistent.
</details>
