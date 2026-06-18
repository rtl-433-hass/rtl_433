---
id: 1
group: "new-device-notification"
dependencies: []
status: "completed"
created: 2026-05-28
skills:
  - python
  - home-assistant
---
# Raise a restart-safe persistent notification on genuinely-new device discovery

## Objective
When a hub discovers a genuinely-new RF device (one never adopted before), raise an in-app `persistent_notification`. Gate it on the persisted devices map so HA restarts / coordinator reloads do NOT re-notify known devices.

## Skills Required
- `python`, `home-assistant` — `new_device_callback` in `__init__.py`, `persistent_notification.async_create`.

## Acceptance Criteria
- [ ] Inside `new_device_callback` (`custom_components/rtl_433/__init__.py`), in addition to the existing `signal_new_device` dispatch, call `homeassistant.components.persistent_notification.async_create(...)`.
- [ ] **Restart-safe gate**: capture `is_new_device = device_key not in entry.data.get(CONF_DEVICES, {})` **before** the dispatch (the signal handler schedules a deferred `async_upsert_device` that adds the key). Only notify when `is_new_device`.
- [ ] Dispatch the signal first, then (conditionally) notify — a notification failure must never block device creation.
- [ ] Stable `notification_id = f"{DOMAIN}_new_device_{entry.entry_id}_{device_key}"` so a deleted-then-re-transmitting device replaces rather than stacks.
- [ ] Title: `"rtl_433: new device discovered"`. Message names the model, device key, and hub (`entry.title`); fall back to the key alone when `model` is empty.
- [ ] No new options/entities/signals/services; no `notify.*`/push/repair-issue. `CONF_DEVICES` is already imported.
- [ ] `ruff check`/`ruff format --check` clean; existing tests pass.

## Technical Requirements
- File: `custom_components/rtl_433/__init__.py` (the `new_device_callback` closure in `async_setup_entry`; it closes over `hass` and `entry`).
- Import `from homeassistant.components import persistent_notification` at module top.

## Input Dependencies
None.

## Output Artifacts
- The gated notification, consumed by tests (task 2) and docs (task 3).

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Read `new_device_callback` (~`__init__.py:181`) — currently only `async_dispatcher_send(hass, signal_new_device(entry.entry_id), device_key, model)`. Confirm current lines.
- Reference shape:
  ```python
  from homeassistant.components import persistent_notification

  def new_device_callback(device_key: str, model: str) -> None:
      # entry.data[CONF_DEVICES] is the restart-persisted "ever-adopted" record;
      # read it BEFORE dispatch (which schedules the upsert that adds the key).
      is_new_device = device_key not in entry.data.get(CONF_DEVICES, {})
      async_dispatcher_send(hass, signal_new_device(entry.entry_id), device_key, model)
      if is_new_device:
          name = model or device_key
          persistent_notification.async_create(
              hass,
              f"A new device '{name}' (key {device_key}) was added under hub "
              f"'{entry.title}'.",
              title="rtl_433: new device discovered",
              notification_id=f"{DOMAIN}_new_device_{entry.entry_id}_{device_key}",
          )
  ```
- The coordinator already wraps the callback in try/except, so no extra guarding is needed beyond ordering.
- Do NOT add a discovery check (the callback only fires when discovery is enabled). Do NOT add translations (persistent notifications are plain text).
- Note (cross-plan): on this branch the coordinator's replay-suppression (plan 08) is present; a device first seen only via a replay/gap frame is still genuinely new (absent from the persisted map) and will notify — acceptable.
- Verify: `uvx ruff check .`; `uvx ruff format --check .`; `python -m pytest tests/test_lifecycle.py tests/test_coordinator.py -q`.
</details>
