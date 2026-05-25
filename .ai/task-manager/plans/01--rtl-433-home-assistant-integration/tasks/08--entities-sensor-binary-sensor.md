---
id: 8
group: "core"
dependencies: [5, 6, 7]
status: "completed"
created: 2026-05-25
skills:
  - home-assistant
  - python
---
# Entity Base + sensor & binary_sensor Platforms

## Objective
Implement the shared base entity and the `sensor`/`binary_sensor` platforms for per-device config entries: build entities from the device's mapped fields, create new entities dynamically via `async_add_entities` when a previously unseen mapped field first appears, persist the set of observed field keys across restarts, restore last known state via `RestoreEntity`, and wire availability from the coordinator's last-seen/watchdog.

## Skills Required
- `home-assistant` — entity platforms, `CoordinatorEntity`/dispatcher, `RestoreEntity`, device registry, dynamic `async_add_entities`
- `python` — async, state management

## Acceptance Criteria
- [ ] `custom_components/rtl_433/entity.py` defines a base entity that centralizes: `DeviceInfo` (device registry), dispatcher subscription to `SIGNAL_DEVICE_UPDATE` for its device, availability wiring (consulting coordinator last-seen vs effective timeout), and `RestoreEntity` state restoration.
- [ ] `custom_components/rtl_433/sensor.py` `async_setup_entry` (for device entries) creates `SensorEntity` instances for each mapped measurement field the device has observed, using `device_class`/`state_class`/`unit_of_measurement`/`name`/`value_transform` from the loader (Task 5).
- [ ] `custom_components/rtl_433/binary_sensor.py` `async_setup_entry` creates `BinarySensorEntity` instances for mapped boolean fields (battery, tamper, contact/reed, alarm, leak), applying the `payload` mapping (including the `battery_ok` inversion).
- [ ] **Dynamic creation**: when a dispatched event carries a mapped field with no existing entity, a new entity is created via the platform's `async_add_entities` callback (deduped by unique_id), and the device's persisted observed-field set is expanded.
- [ ] **Persistence**: the per-device observed-field set is stored in the device config entry (e.g. `entry.options`/`entry.data` via `hass.config_entries.async_update_entry`) so entities are recreated on restart (Success Criteria #10).
- [ ] **Restore**: entities use `RestoreEntity`; on startup they show last known state until a fresh event or the timeout elapses (Success Criteria #11).
- [ ] Unique_ids are instance-scoped: `{hub_entry_id}:{device_key}:{object_suffix}` (no collisions across hubs — Success Criteria #1).
- [ ] `ruff check` passes; modules import cleanly.
- [ ] A single conventional commit (e.g. `feat: add sensor and binary_sensor platforms`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Entities belong to **per-device** config entries. `async_setup_entry` in each platform must resolve the parent hub's coordinator (via the stored `CONF_HUB_ENTRY_ID`) to subscribe to dispatcher signals and read last-seen.
- Use `homeassistant.helpers.dispatcher.async_dispatcher_connect`; store the unsub and clean up in `async_will_remove_from_hass`.
- Use `homeassistant.helpers.restore_state.RestoreEntity`.
- Availability: entity `available` returns False when `utcnow() - last_seen > effective_timeout`; recompute on dispatcher updates and on the watchdog signal; call `async_write_ha_state` when availability changes.
- Dynamic add: keep a set of created unique_ids per platform setup; on each event, for any mapped field without an entity, build and `async_add_entities([...])`, then persist the expanded field set.
- `DeviceInfo`: `identifiers={(DOMAIN, f"{hub_entry_id}:{device_key}")}`, `name` from model + device key, `via_device` pointing at the hub device if registered.

## Input Dependencies
- Task 5: `mapping.py` (`lookup`, `apply_transform`, descriptor fields, `should_skip`).
- Task 6: coordinator (last-seen, dispatcher signal name, effective-timeout resolution) and `normalizer`.
- Task 7: per-device config entry shape (`CONF_HUB_ENTRY_ID`, `CONF_DEVICE_KEY`, `CONF_MODEL`), options for per-device timeout.

## Output Artifacts
- `entity.py`, `sensor.py`, `binary_sensor.py` consumed by `__init__.py` wiring (Task 9, which forwards platform setup) and tested in Task 10.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. **Resolve the coordinator**: the hub stores its coordinator in `hass.data[DOMAIN][hub_entry_id]` (set in Task 9). A device platform setup reads `entry.data[CONF_HUB_ENTRY_ID]` → `hass.data[DOMAIN][hub_entry_id]`. Guard for the hub not yet loaded (device entries depend on the hub; Task 9 should set up hub before children, but be defensive).
2. **Base entity** (`entity.py`):
   - `__init__(self, coordinator, hub_entry_id, device_key, descriptor)`.
   - `_attr_unique_id = f"{hub_entry_id}:{device_key}:{descriptor.object_suffix}"`.
   - `device_info` as above.
   - `available`: compute from coordinator.last_seen[device_key] vs effective timeout.
   - `async_added_to_hass`: subscribe to dispatcher signal; restore state via `RestoreEntity` (`await self.async_get_last_state()`); register a watchdog/availability listener.
   - `async_will_remove_from_hass`: unsub.
   - `_handle_update(normalized)`: if the entity's field is present, apply transform and write state + mark available.
3. **sensor.py / binary_sensor.py** `async_setup_entry`:
   - Load mapping registry (once, cached on `hass.data`).
   - Read the device's persisted observed-field set; for each field whose descriptor `platform` matches this platform, create an entity. Track created unique_ids.
   - Register a dispatcher listener for the device that, on a new mapped field of this platform, creates+adds the entity and persists the expanded set via `hass.config_entries.async_update_entry(entry, options={...observed_fields...})`.
   - Use `async_add_entities`.
4. **Persistence of observed fields**: store as a list under `entry.options["observed_fields"]` (or data). On setup, union with any fields already seen. Persist additions immediately so a restart recreates them.
5. **binary payload**: use `apply_transform` from Task 5 to convert raw → bool, honoring `payload`/inversion.
6. **Restore + timeout**: after restore, `available` still reflects last-seen (which is empty at startup → unavailable only once timeout passes; to satisfy "restore then time out", treat a freshly-restored entity as available until the first watchdog tick determines staleness — initialize last_seen optimistically to startup time OR allow `available=True` until timeout from startup). Implement: on startup set a per-device `last_seen` to `utcnow()` baseline if unknown, so restored state shows and the device only goes unavailable after the timeout from startup (matches Clarification #10).
7. Only create `entity.py`, `sensor.py`, `binary_sensor.py`. The platform forwarding (`async_forward_entry_setups`) is Task 9.
8. `ruff check`; commit `feat:`.
</details>
