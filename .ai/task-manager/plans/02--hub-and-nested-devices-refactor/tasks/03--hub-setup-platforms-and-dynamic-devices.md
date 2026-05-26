---
id: 3
group: "core"
dependencies: [1]
status: "pending"
created: "2026-05-26"
skills:
  - python
  - home-assistant
complexity_score: 5
complexity_notes: "Cohesive core runtime model change across __init__/entity/sensor/binary_sensor/diagnostics; kept as one task to preserve the devices-map + dispatcher contract in a single author's hands. Migration is split out (Task 4) to keep this ≤5."
---
# Hub setup, single-entry platforms & runtime device management

## Objective
Convert the runtime from per-device config entries to one hub config entry with nested device-registry devices. The hub forwards platforms once; entities for every known device are created from `entry.data["devices"]`; new devices are added at runtime (gated by the discovery toggle) and new fields are added dynamically; a device can be removed from its device page via `async_remove_config_entry_device`. No migration logic in this task (Task 4 adds it).

## Skills Required
- `python`: substantial refactor across several modules.
- `home-assistant`: config-entry setup, `async_forward_entry_setups`, device/entity registry, dispatcher, `async_remove_config_entry_device`.

## Acceptance Criteria
- [ ] `async_setup_entry` sets up the hub directly (library load, hub device registration, coordinator start, reachability watcher, options-update listener) and calls `async_forward_entry_setups(entry, PLATFORMS)` once; the device-entry code path (`_async_setup_device_entry`, `ConfigEntryNotReady` deferral) is gone.
- [ ] The coordinator's `new_device_callback` is wired to dispatch `signal_new_device(entry_id)` (gated by `discovery_enabled`, which the coordinator already enforces); it no longer starts a discovery flow.
- [ ] `effective_timeout_resolver` reads `entry.data[CONF_DEVICES][device_key][DEVICE_TIMEOUT_OVERRIDE]`, falling back to the hub default.
- [ ] `entity.py` provides a hub-wide platform setup that (a) creates entities for every device in `entry.data["devices"]`, (b) adds a new device's entities when `signal_new_device` fires, (c) adds new fields dynamically per device, and (d) keeps `entry.data["devices"]` updated (model + sorted unioned fields) writing only on change.
- [ ] `sensor.py` and `binary_sensor.py` delegate to the hub-wide setup; entity unique_ids and `DeviceInfo` are byte-for-byte the 0.1.0 scheme (`{hub_entry_id}:{device_key}:{object_suffix}`, identifier `(DOMAIN, "{hub_entry_id}:{device_key}")`, `via_device (DOMAIN, hub_entry_id)`).
- [ ] `async_remove_config_entry_device` returns `False` for the hub device `(DOMAIN, entry.entry_id)` and `True` for nested devices; allowing removal drops the device from `entry.data[CONF_DEVICES]` and calls `coordinator.forget_device(device_key)`.
- [ ] `async_remove_entry` no longer cascade-removes child entries; `diagnostics.py` always resolves the hub coordinator (no device-entry branch).
- [ ] The integration imports cleanly and a minimal hub setup works (verified by Task 5 tests).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `custom_components/rtl_433/__init__.py`, `entity.py`, `sensor.py`, `binary_sensor.py`, `diagnostics.py`.
- Uses Task 1: `CONF_DEVICES`, `DEVICE_FIELDS`, `DEVICE_TIMEOUT_OVERRIDE`, `signal_new_device`, `Rtl433Coordinator.forget_device`.
- Do NOT add `async_migrate_entry` here (Task 4).

## Input Dependencies
- Task 1 constants, signal, and `forget_device`.

## Output Artifacts
- A working single-hub runtime that Task 4 (migration) and Task 5 (tests) build on.

## Implementation Notes

<details>
<summary>Detailed steps</summary>

**Device-map helper (put in `entity.py`, used by the dynamic-add handlers):**
```python
async def async_upsert_device(hass, entry, device_key, *, model=None, fields=None):
    """Merge a device's model/fields into entry.data[CONF_DEVICES]; write only on change."""
    devices = {k: dict(v) for k, v in entry.data.get(CONF_DEVICES, {}).items()}
    rec = devices.setdefault(device_key, {CONF_MODEL: model or "", DEVICE_FIELDS: []})
    changed = False
    if model and rec.get(CONF_MODEL) != model:
        rec[CONF_MODEL] = model; changed = True
    if fields:
        merged = sorted(set(rec.get(DEVICE_FIELDS, [])) | set(fields))
        if merged != rec.get(DEVICE_FIELDS, []):
            rec[DEVICE_FIELDS] = merged; changed = True
    if changed:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICES: devices})
```

**`entity.py` — `Rtl433Entity` base:** keep the class essentially as-is (identity, DeviceInfo, availability, dispatcher subscription, restore). It already takes `(coordinator, hub_entry_id, device_key, model, descriptor)` — unchanged. The unique_id / DeviceInfo / `via_device` must stay identical to 0.1.0.

**`entity.py` — replace `async_setup_device_platform` with a hub-wide `async_setup_hub_platform(hass, entry, async_add_entities, platform, entity_cls)`:**
1. Resolve `coordinator = hass.data[DOMAIN][entry.entry_id]`.
2. Get the cached registry `registry = hass.data[DOMAIN][DATA_LIBRARY][0]`.
3. Define `_descriptor_for(field_key)` (lookup + `descriptor.platform == platform`), and `_build(device_key, model, field_keys)` that dedupes by unique_id and returns entities (mirror current `_build_for_fields`, but unique_ids are tracked per-(device_key) — use a dict `created: dict[str, set[str]]` keyed by device_key).
4. **Initial build:** for each `device_key, rec` in `entry.data.get(CONF_DEVICES, {})`: union `rec[DEVICE_FIELDS]` with `coordinator.device_fields.get(device_key, set())`; `async_add_entities(_build(device_key, rec.get(CONF_MODEL, ""), union))`; `await async_upsert_device(...)` to persist any coordinator-known fields not yet stored.
5. **New-field dynamic add:** register ONE dispatcher listener per device_key on `signal_device_update(entry_id, device_key)` that adds entities for newly-seen mapped fields of this platform and calls `async_upsert_device`. To cover devices added later, register this inside the new-device handler too (see step 6). Use `entry.async_on_unload(...)` for every subscription.
6. **New-device dynamic add:** subscribe to `signal_new_device(entry.entry_id)` with a handler `(device_key, model)` that: builds this platform's entities for `coordinator.device_fields.get(device_key, set())`, `async_add_entities(...)`, registers the per-device new-field listener (step 5) for this device_key, and calls `async_upsert_device(hass, entry, device_key, model=model, fields=...)`. Register the subscription via `entry.async_on_unload`.
   - Note both `sensor` and `binary_sensor` platforms run this independently; each adds only its own descriptors. `async_upsert_device` is idempotent/union so concurrent writes converge.
7. Remove the old `CONF_OBSERVED_FIELDS`/`async_persist_observed_fields` mechanism (replaced by the devices map).

**`sensor.py` / `binary_sensor.py`:** change `async_setup_entry` to call `async_setup_hub_platform(hass, entry, async_add_entities, PLATFORM, Rtl433Sensor|Rtl433BinarySensor)`. The entity classes are otherwise unchanged (they still seed from `coordinator.devices.get(device_key)` and restore state).

**`__init__.py`:**
8. `async_setup_entry`: drop the `is_hub_entry` branch — always set up the hub. Keep the body of the old `_async_setup_hub_entry` (library load, `effective_timeout_resolver`, hub device registration, coordinator construction/start, reachability watcher, options-update listener) and ADD `await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)` at the end. Delete `_async_setup_device_entry`.
9. Change `effective_timeout_resolver(device_key)` to read `entry.data.get(CONF_DEVICES, {}).get(device_key, {}).get(DEVICE_TIMEOUT_OVERRIDE)` → int or hub default.
10. Change `new_device_callback(device_key, model)` to `async_dispatcher_send(hass, signal_new_device(entry.entry_id), device_key, model)`. Remove the discovery_flow import/usage and the unique_id-dedup loop (the coordinator's `is_new` check already dedupes; recreation comes from the devices map).
11. `async_unload_entry`: now just `coordinator.async_stop()`, pop from `hass.data`, clear repair, and `await hass.config_entries.async_unload_platforms(entry, PLATFORMS)`.
12. Delete `async_remove_entry` cascade logic (hub deletion removes nested devices/entities automatically). You may remove `async_remove_entry` entirely.
13. Add:
    ```python
    async def async_remove_config_entry_device(hass, config_entry, device_entry) -> bool:
        # Refuse removing the hub device itself.
        if (DOMAIN, config_entry.entry_id) in device_entry.identifiers:
            return False
        # Find this device's device_key from its identifier (DOMAIN, f"{entry_id}:{device_key}").
        device_key = None
        for domain, ident in device_entry.identifiers:
            if domain == DOMAIN and ident.startswith(f"{config_entry.entry_id}:"):
                device_key = ident.split(":", 1)[1]
                break
        if device_key is not None:
            devices = {k: v for k, v in config_entry.data.get(CONF_DEVICES, {}).items() if k != device_key}
            hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, CONF_DEVICES: devices})
            coordinator = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
            if coordinator is not None:
                coordinator.forget_device(device_key)
        return True
    ```
14. Update `__all__` (drop `async_remove_entry` if removed; add `async_remove_config_entry_device`; Task 4 adds `async_migrate_entry`).

**`diagnostics.py`:** remove the `is_hub_entry`/`CONF_HUB_ENTRY_ID` branch in `_resolve_coordinator`; the coordinator is always `hass.data[DOMAIN].get(entry.entry_id)`. Drop the now-unused import of `is_hub_entry`. Everything else (redaction, `unmatched_field_keys`, device snapshot) stays.

**Validation:** `uv run python -c "import custom_components.rtl_433"`. Full behavior is verified by Task 5; do not rewrite tests here.
</details>
