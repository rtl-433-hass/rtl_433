---
id: 5
group: "testing"
dependencies: [2, 3, 4]
status: "completed"
created: "2026-05-26"
skills:
  - python
  - pytest
---
# Test suite realignment for the single-hub model

## Objective
Update the test suite to the nested model and add the high-value tests called out in the plan's Self Validation: single-hub setup, nested device + entity creation, dynamic late-device/late-field add, restore, remove→retransmit re-add, hub-device removal refusal, the hub/device options flow, and the 0.1.0 → nested migration with preserved unique_ids/entity_ids.

## Skills Required
- `python`, `pytest`: Home Assistant custom-component testing with `pytest-homeassistant-custom-component`.

## Acceptance Criteria
- [ ] `tests/conftest.py`: the per-device entry builder is removed; the hub builder accepts a `devices` map (`entry.data["devices"]`).
- [ ] `tests/test_config_flow.py`: discovery/confirm/ignore tests removed; added coverage for the hub user step (unchanged), the hub options step, the device options step (set/clear timeout override into `entry.data["devices"]`), and `async_remove_config_entry_device` (returns False for hub device, True + map/coordinator eviction for a nested device).
- [ ] `tests/test_lifecycle.py`: single hub entry setup; nested device + entity creation with correct unique_ids/classes/units; dynamic add of a brand-new device with discovery on (and NOT added with discovery off); dynamic late-field add persists across reload; `RestoreEntity` restore; remove device → evicted from coordinator → re-transmits with discovery on → re-appears.
- [ ] A migration test builds the 0.1.0 shape (hub entry + two device entries, with pre-seeded registry devices + entities) and asserts: only the hub entry remains, devices are associated with the hub, and entity `unique_id`/`entity_id` are unchanged.
- [ ] `tests/test_diagnostics_repairs.py` / `tests/test_coordinator.py` updated only as needed to match the single-hub diagnostics path.
- [ ] `uv run pytest --cov=custom_components/rtl_433 tests/` passes.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `tests/conftest.py`, `tests/test_config_flow.py`, `tests/test_lifecycle.py`, and minimal edits to `tests/test_diagnostics_repairs.py`, `tests/test_coordinator.py` if they reference removed surfaces.
- Mantra: "write a few tests, mostly integration." Focus on the integration's own logic (device-map consolidation, dynamic add, migration), not framework behavior.

## Input Dependencies
- Tasks 2, 3, 4 (the runtime, flows, and migration under test).

## Output Artifacts
- A green test suite proving the refactor and migration.

## Implementation Notes

<details>
<summary>Detailed steps & Meaningful Test Strategy</summary>

**Meaningful Test Strategy Guidelines (keep in mind):** Test YOUR logic, not the framework. WRITE tests for: device-map consolidation/migration, dynamic device/field add gated by the discovery toggle, remove→re-add eviction, unique_id/history preservation. DON'T write: tests for HA registry internals, trivial getters, or each translation string.

**`conftest.py`:**
- Remove `build_device_entry` / `device_entry_builder`.
- Extend `build_hub_entry` to accept `devices: dict | None = None` and place it at `data["devices"]` (shape: `{device_key: {"model": str, "fields": [..], "timeout_override": int?}}`). Keep the hub unique_id `hub:{host}:{port}`.

**`test_lifecycle.py` (the heavy hitter — adapt the existing helpers):**
- `_setup_hub` builds ONE hub entry (optionally pre-seeded `devices`) and `async_setup`s it; the `_no_socket` patch on `_connect_loop` stays; `_feed` still injects via `coordinator._handle_text_frame`.
- Assert: seeding `devices` recreates entities (unique_id `{hub_entry_id}:{device_key}:{suffix}`) on the hub entry; the nested device has identifier `(DOMAIN, "{hub_entry_id}:{device_key}")` and `via_device` the hub.
- Discovery on: feed an event for an unseen device → assert a nested device + entities appear and the device is added to `entry.data["devices"]`. Discovery off (`build_hub_entry(discovery_enabled=False)`): feed an unseen device → assert no new device/entities.
- Late field: feed an event adding a new mapped field to a known device → assert the new entity appears; reload the entry → assert it persists (from the devices map).
- Restore: use `mock_restore_cache`; assert restored native value shows.
- Remove/re-add: call `async_remove_config_entry_device` for a nested device → assert entities gone, device_key gone from `entry.data["devices"]`, and `coordinator.forget_device` removed runtime state; with discovery on, feed the device again → assert it re-appears. Also assert `async_remove_config_entry_device` returns False for the hub device.
- Migration test: create a hub `MockConfigEntry(version=1)` + two device `MockConfigEntry(version=1, data={entry_type: device, hub_entry_id, device_key, model}, options={observed_fields:[...], availability_timeout:...})`; pre-create their registry devices (`device_registry.async_get_or_create(config_entry_id=child.entry_id, identifiers={(DOMAIN, f"{hub_id}:{device_key}")})`) and a couple of entities (`entity_registry.async_get_or_create("sensor", DOMAIN, f"{hub_id}:{device_key}:T", config_entry_id=child.entry_id)`); `add_to_hass` all; `async_setup(hub.entry_id)`; `await hass.async_block_till_done()`. Assert: `len(hass.config_entries.async_entries(DOMAIN)) == 1`; the two devices now list the hub entry in `config_entries`; the seeded entities still exist with the same `unique_id`/`entity_id` and `config_entry_id == hub.entry_id`; `hub.data["devices"]` contains both device_keys with their folded fields and timeout override.

**`test_config_flow.py`:**
- Keep `test_user_step_success_creates_hub` and `cannot_connect`.
- Remove `integration_discovery`/`confirm`/`ignore` tests.
- Add: hub options step persists discovery toggle + timeout to `entry.options`; device options step writes/clears `timeout_override` into `entry.data["devices"][device_key]`; `no_devices` abort when the map is empty.
- Add a direct unit test of `async_remove_config_entry_device` (construct a fake `device_entry` with the hub identifier → False; with a nested identifier → True and map/coordinator side effects).

**Run:** `uv run pytest --cov=custom_components/rtl_433 tests/`. If a pre-existing test relies on a removed surface (`build_device_entry`, `ENTRY_TYPE_DEVICE` discovery), update or delete it.
</details>
