---
id: 1
group: "device-triggers"
dependencies: []
status: "completed"
created: 2026-05-28
skills:
  - python
  - home-assistant
complexity_score: 5
complexity_notes: "New device-automation trigger module with a base (core-state-trigger delegation) path and a custom state-change-listener subtyped path that must replicate trigger context; plus translations. Kept as one task because the schema, both attach paths, and enumeration are one coherent contract."
---
# Implement device_trigger.py + device_automation translations

## Objective
Add a device-automation **trigger** platform so the integration's `event` entities (button/motion/doorbell) appear in the UI device-trigger picker, with one base trigger per entity plus optional per-`event_type` subtypes. Triggers only — no conditions/actions. Add the `device_automation.trigger_type` translations.

## Skills Required
- `python`, `home-assistant` — device-automation trigger conventions, core `state` trigger delegation, `async_track_state_change_event`, entity registry, `HassJob` context wiring.

## Acceptance Criteria
- [ ] New `custom_components/rtl_433/device_trigger.py` with `async_get_triggers(hass, device_id)`, `async_attach_trigger(hass, config, action, trigger_info)`, a `TRIGGER_SCHEMA` extending `DEVICE_TRIGGER_BASE_SCHEMA` (required `entity_id` via `cv.entity_id_or_uuid`, required `type`, optional `subtype`), and `async_validate_trigger_config`.
- [ ] `async_get_triggers` enumerates the device's event entities via `er.async_entries_for_device(...)` filtered to `entry.domain == "event"` AND `entry.platform == DOMAIN`; returns one base descriptor per entity + one subtyped descriptor per persisted `event_type` (from `entry.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES][field_key]`, with the live `event_types` capability attribute as fallback).
- [ ] **Base trigger** (no `subtype`): delegates to `homeassistant.components.homeassistant.triggers.state` (match_all on the entity) — fires once per transmission.
- [ ] **Subtyped trigger**: uses `async_track_state_change_event` with a `@callback` listener that fires when `new_state is not None and new_state.attributes.get(ATTR_EVENT_TYPE) == subtype`, with **NO** `old == new` dedupe (so same-value repeats fire every time). `ATTR_EVENT_TYPE` from `homeassistant.components.event.const`.
- [ ] Both paths deliver a proper `device`-platform trigger payload + context (wrap `action` in `HassJob`, `hass.async_run_hass_job(job, {"trigger": {**trigger_info["trigger_data"], "platform": "device", "entity_id": entity_id, ...}}, event.context)`).
- [ ] **No** `async_get_conditions`/`async_get_actions`; `const.py` `PLATFORMS` **unchanged** (module is discovered by file presence).
- [ ] `translations/en.json` gains `device_automation.trigger_type` with a base type (`"{entity_name} triggered"`) and a subtyped type (`"{entity_name} triggered: {subtype}"`); **no** `trigger_subtype` block. Valid JSON.
- [ ] `ruff` clean; module parses/imports.

## Technical Requirements
- File: `custom_components/rtl_433/device_trigger.py` (new), `custom_components/rtl_433/translations/en.json`.
- Reference shape: `.venv/.../components/sensor/device_trigger.py` (enumerate registry entities → delegate to a core trigger), swapping `numeric_state` for `state`.

## Input Dependencies
None.

## Output Artifacts
- `device_trigger.py` + translations — consumed by tests (task 2) and docs (task 3).

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Study `.venv/lib/.../site-packages/homeassistant/components/sensor/device_trigger.py` (esp. `async_get_triggers` ~:160-300, `async_attach_trigger` ~:235-257, and the entity-id resolution at ~:306 via `async_get_entity_registry_entry_or_raise`).
- Read this repo's `entity.py` for the unique_id format `f"{hub_entry_id}:{device_key}:{object_suffix}"` (`:107`) and device identifiers `{DOMAIN: f"{hub_entry_id}:{device_key}"}` (`:120-126`); read `const.py` for `DEVICE_EVENT_TYPES`/`CONF_DEVICES` and `event.py` for how `event_types` are persisted (`async_upsert_event_types`).
- **Base path** (mirror sensor/device_trigger): build `{CONF_PLATFORM: "state", CONF_ENTITY_ID: config[CONF_ENTITY_ID]}`, then `state.async_validate_trigger_config` and `state.async_attach_trigger(hass, state_config, action, trigger_info, platform_type="device")`.
- **Subtyped path**: resolve the entity_id from the uuid via `er`; `async_track_state_change_event(hass, [entity_id], _listener)`. In `_listener(event)`: `new = event.data["new_state"]; if new and new.attributes.get(ATTR_EVENT_TYPE) == subtype: hass.async_run_hass_job(job, {...}, new.context)`. Return the unsub. Do NOT add an `old_state == new_state` guard.
- Recover `device_key`/`field_key` for subtype enumeration by parsing the registry entry's `unique_id` (split on `:`), falling back to the loaded entity's `event_types` capability attribute. The `object_suffix` for events equals the field key.
- Define a single trigger `type` constant (e.g. `"triggered"`); the subtype is carried separately in `subtype`.
- **Do not** add `device_trigger` to `PLATFORMS`. Do not define conditions/actions.
- Verify: `python -m ruff check custom_components/`; `python -c "import json; json.load(open('custom_components/rtl_433/translations/en.json'))"`; parse-check the module.
</details>
