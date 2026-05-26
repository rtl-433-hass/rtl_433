---
id: 4
group: "core"
dependencies: [2, 3]
status: "pending"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# In-place migration from 0.1.0 (per-device entries → nested devices)

## Objective
Add `async_migrate_entry` so an existing 0.1.0 install upgrades in place (no uninstall) with entity IDs and history preserved. The migration consolidates the legacy per-device config entries into the hub's `entry.data["devices"]` map, re-homes the existing registry devices and entities onto the hub config entry, and removes the obsolete device config entries.

## Skills Required
- `python`: registry manipulation and config-entry lifecycle code.
- `home-assistant`: `async_migrate_entry`, device/entity registry re-homing, `async_remove`.

## Acceptance Criteria
- [ ] `async_migrate_entry(hass, entry)` handles `entry.version == 1 → 2`.
- [ ] For the hub entry: every legacy child device entry (carrying `CONF_HUB_ENTRY_ID == hub.entry_id`) is folded into `hub.entry.data[CONF_DEVICES]` as `{device_key: {model, fields, timeout_override?}}` using each child's `data[CONF_MODEL]`/`data[CONF_DEVICE_KEY]`, `options["observed_fields"]`, and `options[CONF_AVAILABILITY_TIMEOUT]`.
- [ ] Each legacy device's **registry device and its entities are re-homed onto the hub config entry BEFORE** the legacy device config entry is removed (so nothing is deleted). Entity unique_ids and entity_ids are unchanged.
- [ ] Legacy device config entries are removed; after migration only the hub entry remains and its version is 2.
- [ ] Migration is idempotent and tolerant of ordering (a legacy device entry processed on its own does not crash and converges on the same invariant).
- [ ] `uv run pytest tests/` import-time passes; full migration behavior is asserted by Task 5.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File `custom_components/rtl_433/__init__.py` (add `async_migrate_entry`, export it).
- Reads legacy constants: `CONF_ENTRY_TYPE`, `ENTRY_TYPE_HUB`, `ENTRY_TYPE_DEVICE`, `CONF_HUB_ENTRY_ID`, `CONF_DEVICE_KEY`, `CONF_MODEL` (all still in `const.py`). The legacy observed-fields option key is the literal string `"observed_fields"`.
- Writes `CONF_DEVICES` / `DEVICE_FIELDS` / `DEVICE_TIMEOUT_OVERRIDE` (Task 1).

## Input Dependencies
- Task 2: `VERSION = 2` (the trigger).
- Task 3: the `entry.data[CONF_DEVICES]` shape and runtime that consumes it.

## Output Artifacts
- A seamless upgrade path consumed by Task 5's migration test.

## Implementation Notes

<details>
<summary>Detailed steps</summary>

**Add to `__init__.py`:**

```python
from homeassistant.helpers import device_registry as dr, entity_registry as er

LEGACY_CONF_OBSERVED_FIELDS = "observed_fields"

async def async_migrate_entry(hass, entry) -> bool:
    if entry.version > 2:
        return False  # downgrade unsupported
    if entry.version == 1:
        # A legacy *device* entry: re-home its objects to its hub, then let the hub
        # migration (or this same code) remove it. Simplest robust approach: do all
        # consolidation from the HUB entry; for a device entry just bump version and
        # return True (the hub removes it). To be order-independent, detect type:
        is_device = entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_DEVICE
        if is_device:
            # Re-home this device entry's registry objects onto its parent hub so that
            # if it is removed before the hub migrates, nothing is lost.
            hub_id = entry.data.get(CONF_HUB_ENTRY_ID)
            if hub_id:
                _rehome_device_objects(hass, entry, hub_id)
            hass.config_entries.async_update_entry(entry, version=2)
            return True
        # Hub entry: consolidate all children.
        await _migrate_hub_entry(hass, entry)
        hass.config_entries.async_update_entry(entry, version=2)
    return True
```

**`_rehome_device_objects(hass, device_entry, hub_entry_id)` (sync helper):**
- `dev_reg = dr.async_get(hass)`, `ent_reg = er.async_get(hass)`.
- For every device in `dev_reg.devices` whose `config_entries` includes `device_entry.entry_id`: `dev_reg.async_update_device(device.id, add_config_entry_id=hub_entry_id)` then `remove_config_entry_id=device_entry.entry_id` (add first so the device is never orphaned).
- For every entity in `er.async_entries_for_config_entry(ent_reg, device_entry.entry_id)`: `ent_reg.async_update_entity(entity.entity_id, config_entry_id=hub_entry_id)`.

**`_migrate_hub_entry(hass, hub_entry)`:**
1. `children = [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(CONF_HUB_ENTRY_ID) == hub_entry.entry_id and e.entry_id != hub_entry.entry_id]`.
2. Build `devices = dict(hub_entry.data.get(CONF_DEVICES, {}))`. For each child:
   - `device_key = child.data[CONF_DEVICE_KEY]`, `model = child.data.get(CONF_MODEL, "")`.
   - `fields = sorted(child.options.get(LEGACY_CONF_OBSERVED_FIELDS, []))`.
   - `rec = {CONF_MODEL: model, DEVICE_FIELDS: fields}`; if `child.options.get(CONF_AVAILABILITY_TIMEOUT) is not None: rec[DEVICE_TIMEOUT_OVERRIDE] = int(child.options[CONF_AVAILABILITY_TIMEOUT])`.
   - `devices[device_key] = rec`.
   - `_rehome_device_objects(hass, child, hub_entry.entry_id)` — **before removal**.
3. `hass.config_entries.async_update_entry(hub_entry, data={**hub_entry.data, CONF_DEVICES: devices})`.
4. For each child: `await hass.config_entries.async_remove(child.entry_id)`. (The v2 `async_remove_entry` is a no-op/cascade-free, so this is safe and won't delete the re-homed objects.)

**Ordering note:** HA processes migrations per entry; the hub path is authoritative. If a device entry migrates first, `_rehome_device_objects` protects its objects; when the hub later runs, the child still appears in `async_entries` (until removed) and is folded + removed. If the hub runs first and removes children, those children never run their own migration. Either way the invariant holds: only the hub remains, objects are re-homed, fields/overrides folded.

**Export:** add `async_migrate_entry` to `__all__`.

**Validation:** `uv run python -c "import custom_components.rtl_433"`. Behavior is asserted by Task 5's migration test (build hub + two device entries with seeded registry devices/entities, run setup, assert one entry remains and unique_ids/entity_ids preserved).
</details>
