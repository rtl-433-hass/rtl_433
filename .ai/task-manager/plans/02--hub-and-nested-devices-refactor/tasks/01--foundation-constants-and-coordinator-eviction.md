---
id: 1
group: "foundation"
dependencies: []
status: "completed"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# Foundation: devices-map constants & coordinator device eviction

## Objective
Establish the shared contract the rest of the refactor builds on: the `entry.data["devices"]` map keys, the hub-level "new device" dispatcher signal, and a coordinator method that forgets a single device's runtime state. This task is intentionally small and additive so Phase 2 tasks can run in parallel against a stable contract.

## Skills Required
- `python`: edit module constants and a small coordinator method.
- `home-assistant`: dispatcher-signal naming conventions.

## Acceptance Criteria
- [ ] `const.py` defines the devices-map key and per-device sub-keys, and a new-device dispatcher signal + helper, without removing any constant the migration still needs.
- [ ] `coordinator/base.py` exposes a `forget_device(device_key)` method that removes the key from all per-device runtime dicts.
- [ ] `uv run pytest tests/` still passes (no behavior change yet; existing tests must not break from the additive constants).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File `custom_components/rtl_433/const.py`.
- File `custom_components/rtl_433/coordinator/base.py`.
- Home Assistant dispatcher signal string conventions (mirror the existing `SIGNAL_DEVICE_UPDATE`/`signal_device_update`).

## Input Dependencies
None.

## Output Artifacts
- `CONF_DEVICES` and device-map sub-keys (consumed by Tasks 2, 3, 4).
- `SIGNAL_NEW_DEVICE` + `signal_new_device()` (consumed by Tasks 3).
- `Rtl433Coordinator.forget_device()` (consumed by Task 3's `async_remove_config_entry_device`).

## Implementation Notes

<details>
<summary>Detailed steps</summary>

**`const.py` additions (do NOT remove existing constants — migration in Task 4 still reads `CONF_ENTRY_TYPE`, `ENTRY_TYPE_HUB`, `ENTRY_TYPE_DEVICE`, `CONF_HUB_ENTRY_ID`, `CONF_DEVICE_KEY`, `CONF_MODEL`):**

1. Add the per-hub devices-map key (lives under `entry.data`):
   ```python
   # Key under entry.data holding the consolidated per-device map for this hub.
   # Maps device_key -> {"model": str, "fields": list[str], "timeout_override": int | None}.
   CONF_DEVICES: Final = "devices"
   ```
2. Add the device-map sub-keys (reuse the existing `CONF_MODEL = "model"` for the model field):
   ```python
   # Sub-keys inside one entry.data["devices"][device_key] record.
   DEVICE_FIELDS: Final = "fields"               # sorted list of observed mapped field keys
   DEVICE_TIMEOUT_OVERRIDE: Final = "timeout_override"  # int seconds, or absent/None
   ```
3. Add the hub-level new-device signal next to the existing `SIGNAL_DEVICE_UPDATE`:
   ```python
   SIGNAL_NEW_DEVICE: Final = "rtl_433_new_device_{hub_entry_id}"

   def signal_new_device(hub_entry_id: str) -> str:
       """Return the hub-level 'a new device was observed' dispatcher signal."""
       return SIGNAL_NEW_DEVICE.format(hub_entry_id=hub_entry_id)
   ```
   Keep `SIGNAL_DEVICE_UPDATE` / `signal_device_update` as-is.

**`coordinator/base.py` addition:**

4. Add a method to `Rtl433Coordinator` that evicts one device's runtime state (used when a device is removed so a later event is treated as new again — see plan Clarification #4):
   ```python
   def forget_device(self, device_key: str) -> None:
       """Drop a device's runtime state so its next event is treated as new."""
       self.devices.pop(device_key, None)
       self.last_seen.pop(device_key, None)
       self.available.pop(device_key, None)
       self.device_fields.pop(device_key, None)
   ```
   Place it near the other public methods. Do not change any existing behavior.

**Validation:** run `uv venv` (if needed), `uv pip install -r requirements_test.txt`, then `uv run pytest tests/`. The suite should still pass because these are purely additive.
</details>
