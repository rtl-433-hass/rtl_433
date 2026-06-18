---
id: 2
group: "new-device-notification"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - python
  - pytest
---
# Tests for the new-device notification

## Objective
Lock in the gating + de-duplication: notify on a genuinely-new device, but NOT on restart/reload of a known device, NOT on a second sighting, NOT when discovery is off; re-notify after delete.

## Skills Required
- `python`, `pytest` — patch/observe `persistent_notification.async_create`; existing lifecycle/coordinator harness.

## Acceptance Criteria
- [ ] **Genuinely-new notifies once**: adopting a device absent from `entry.data[CONF_DEVICES]` (discovery on) → `async_create` called once with `notification_id == f"{DOMAIN}_new_device_{entry_id}_{device_key}"` and a message naming model/device key/hub.
- [ ] **No notification on restart/reload of a known device** (the regression guard): seed a hub with a device already in `entry.data[CONF_DEVICES]`, feed that device's event so the callback fires (`is_new` true against the empty in-memory `coordinator.devices`) → `async_create` NOT called.
- [ ] **Second sighting** of the same device in a session → not called again.
- [ ] **Discovery off** → not called.
- [ ] **Delete-then-re-transmit** re-notifies (same stable id): after the device is removed from the persisted map (`async_remove_config_entry_device` + `forget_device`), a later transmission calls `async_create` again.
- [ ] `python -m pytest -q` passes; `uvx ruff check tests/` + `uvx ruff format --check tests/` clean.

## Technical Requirements
- Files: `tests/test_lifecycle.py` (wiring) and/or `tests/test_coordinator.py` (callback-level). Patch `homeassistant.components.persistent_notification.async_create` (or `custom_components.rtl_433.persistent_notification.async_create` depending on import form — check how task 1 imported it).
- Reuse `hub_entry_builder` / `_setup_hub` / `_feed`; existing seams: `test_lifecycle.py` `test_new_device_added_when_discovery_on`/`_off`, `test_remove_device_then_re_add_with_discovery_on`.

## Input Dependencies
- Task 1.

## Output Artifacts
- New tests.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Patch target must match task 1's import. If task 1 did `from homeassistant.components import persistent_notification` and calls `persistent_notification.async_create(...)`, patch `custom_components.rtl_433.persistent_notification.async_create` (the name bound in the module) — verify and use the working target.
- The restart/reload case is the key regression: seed `entry.data[CONF_DEVICES]` with the device (so it's "known"), set up the hub (coordinator.devices starts empty), feed that device's frame, `await hass.async_block_till_done()`, assert `async_create` not called.
- For delete-then-re-notify, drive `async_remove_config_entry_device` then re-feed; assert a second `async_create` with the same id.

### Meaningful Test Strategy Guidelines
"write a few tests, mostly integration". Test the gating/dedup contract, not the persistent_notification component. Combine related scenarios per test where natural.
</details>
