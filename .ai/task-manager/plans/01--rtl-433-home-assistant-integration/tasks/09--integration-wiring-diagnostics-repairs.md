---
id: 9
group: "core"
dependencies: [6, 7, 8]
status: "completed"
created: 2026-05-25
skills:
  - home-assistant
  - python
---
# Integration Wiring (setup/unload, cascade removal) + Diagnostics + Repairs

## Objective
Complete the integration lifecycle in `__init__.py`: set up hub entries (start the coordinator, load the mapping library + user overrides, inject skip-keys and the new-device discovery callback, register the hub device), set up device entries (forward `sensor`/`binary_sensor` platforms), tear everything down on unload, and cascade-remove a hub's child device entries (with their devices/entities) when the hub is deleted. Add diagnostics export (with redaction) and a minimal repairs surface.

## Skills Required
- `home-assistant` — config-entry lifecycle, `async_forward_entry_setups`, device registry, `async_remove_entry`, diagnostics, repairs
- `python` — async orchestration

## Acceptance Criteria
- [ ] `__init__.py` `async_setup_entry` branches on entry type:
  - **Hub**: load mapping library (executor) + user overrides, create the coordinator (injecting skip-keys, effective-timeout resolver, and the `new_device_callback` that calls `discovery_flow.async_create_flow` only when discovery is enabled and the device is neither configured nor ignored), register a hub device, store the coordinator in `hass.data[DOMAIN][entry.entry_id]`, start the coordinator, register an options-update listener (so toggling discovery / timeout takes effect).
  - **Device**: ensure the parent hub is loaded, then `async_forward_entry_setups(entry, [SENSOR, BINARY_SENSOR])`.
- [ ] `async_unload_entry` cleanly stops the coordinator (hub) or unloads platforms (device), removing dispatcher subscriptions and the watchdog.
- [ ] **Cascade removal**: deleting a hub entry removes its child device entries (matched by `CONF_HUB_ENTRY_ID`) and their HA devices/entities, leaving no orphans (Success Criteria #9). Implement via `async_remove_entry` and/or an unload path that enumerates and removes children with `hass.config_entries.async_remove`.
- [ ] **New-device discovery callback**: on an unknown device, if the hub's discovery toggle is on and no configured/ignored entry exists for its unique_id, start an integration-discovery flow with `{hub_entry_id, device_key, model}`; if discovery is off, record the device in hub runtime state without surfacing it.
- [ ] `custom_components/rtl_433/diagnostics.py` exports (redacted) hub connection state, observed devices, last-seen times, and **unmatched field keys** (fields seen but not in the mapping library) to aid contributors.
- [ ] `custom_components/rtl_433/repairs.py` provides a minimal, actionable repairs flow (e.g. an issue when the server is unreachable). Keep scope tight — no speculative issues.
- [ ] `ruff check` passes; the full package imports cleanly.
- [ ] A single conventional commit (e.g. `feat: wire integration lifecycle, diagnostics and repairs`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- `hass.data[DOMAIN]` holds per-hub coordinators keyed by hub entry id; device entries look up their parent there.
- Hub setup order: device entries must find their hub's coordinator. Use `entry.async_on_unload` for cleanup. If a device entry loads before its hub, either defer (raise `ConfigEntryNotReady`) or set up hubs first.
- The new-device callback must check the entity/config-entry registries for an existing configured OR ignored entry (unique_id `{hub_entry_id}:{device_key}`) before creating a flow, to prevent discovery loops/duplicates.
- Track unmatched field keys in coordinator runtime state for diagnostics (the normalizer/coordinator can record fields with no descriptor — pass the `lookup` or the descriptor set in, or record raw field names and let diagnostics diff against the library).
- Use `homeassistant.components.diagnostics`/redaction helpers (`async_redact_data`).

## Input Dependencies
- Task 6: coordinator (start/stop, callbacks, last-seen) and normalizer.
- Task 7: config flow (entry types, discovery flow entry point, unique_id scheme).
- Task 8: entity platforms (`sensor`/`binary_sensor` setup entry points).
- Task 5: mapping loader (load library + user overrides, skip-keys, unmatched-key detection).

## Output Artifacts
- Completed `__init__.py`, `diagnostics.py`, `repairs.py` — the integration is now end-to-end functional. Consumed by tests (Task 10) and the integration harness (Task 11).

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. **Edit `__init__.py`** (created as a stub in Task 3). Replace the stubs with:
   - `async_setup_entry`: `hass.data.setdefault(DOMAIN, {})`. If `is_hub_entry(entry)`:
     - `library, skip_keys = await hass.async_add_executor_job(load_library)`; `await hass.async_add_executor_job(load_user_overrides, hass.config.path(...))` merged in; cache the registry on `hass.data[DOMAIN]["library"]` so platforms reuse it.
     - Build `effective_timeout_resolver(device_key)` reading hub option default + per-device entry override.
     - Build `new_device_callback(device_key, model)` → check registries → `discovery_flow.async_create_flow(...)`.
     - Instantiate coordinator with these; register hub device in device registry; store coordinator; `await coordinator.async_start()`; add options update listener (`entry.add_update_listener`) that pushes new discovery/timeout settings into the coordinator and reloads if needed.
   - else (device entry): verify hub coordinator present (`ConfigEntryNotReady` if not), `await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)`.
2. **`async_unload_entry`**: device → `async_unload_platforms`. hub → `await coordinator.async_stop()`, pop from `hass.data`. Return success bool.
3. **`async_remove_entry(hass, entry)`** (hub): find child device entries `[e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(CONF_HUB_ENTRY_ID) == entry.entry_id]` and `await hass.config_entries.async_remove(child.entry_id)` for each (this removes their devices/entities). Confirm no orphan devices remain (the device registry entries are tied to the device config entry and are cleaned up automatically on removal).
4. **Options update listener**: when hub options change (discovery toggle / timeout), update the coordinator's flags live; consider `async_reload` only if necessary.
5. **diagnostics.py**: `async_get_config_entry_diagnostics(hass, entry)` returning redacted dict: connection params (redact host?), state, devices, last_seen (iso), and `unmatched_field_keys`.
6. **repairs.py**: register an issue via `homeassistant.helpers.issue_registry.async_create_issue` when the coordinator reports the server unreachable beyond reconnect attempts; clear it on reconnect. Keep minimal.
7. This task EDITS `__init__.py` (from Task 3) and CREATES `diagnostics.py`, `repairs.py`. It is the only task in its phase, so no file-conflict concerns.
8. `ruff check`; ensure `python -c "import ast"` parses; commit `feat:`.
</details>
