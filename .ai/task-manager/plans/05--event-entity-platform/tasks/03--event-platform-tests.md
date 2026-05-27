---
id: 3
group: "testing"
dependencies: [1, 2]
status: "pending"
created: "2026-05-26"
skills:
  - pytest
  - home-assistant
---
# Tests for the event platform and schema

## Objective
Add meaningful tests covering the new `event` platform's custom behavior and the
three shipped example mappings, without re-testing HA framework internals. Two
test surfaces:

1. **Schema tests** in `tests/test_mapping.py` — assert the three example fields
   resolve to `platform == "event"` descriptors with the expected
   `EventDeviceClass`, that no existing descriptor changed platform, and that
   none of the three field keys is in the skip set.
2. **Lifecycle/platform tests** in `tests/test_lifecycle.py` — set up a hub, feed
   events through the live coordinator, and assert the firing/auto-populate/
   persistence/always-available/no-double-fire/restore behaviors.

## Skills Required
- `pytest` — `pytest-homeassistant-custom-component`, fixtures, `freeze_time`,
  capturing fired events / entity state, reload assertions.
- `home-assistant` — entity & event registries, `state`/`event_type`,
  `_async_watchdog`, `async_fire_time_changed`, config-entry reload.

## Acceptance Criteria

### Schema tests (`tests/test_mapping.py`)
- [ ] A test loads the shipped library and asserts each of the three example fields resolves to a `FieldDescriptor` with `platform == "event"` and the expected `device_class` (`button`/`motion`/`doorbell`).
- [ ] A test asserts none of the three field keys is in the skip-key set (`should_skip(key, skip) is False`).
- [ ] A test asserts a representative existing field (e.g. `temperature_C`, `tamper`) still resolves to its original platform (no descriptor changed platform).

### Lifecycle tests (`tests/test_lifecycle.py`)
- [ ] **Creation + value-as-type firing + growth:** set up a hub with a device whose observed fields include the mapped event field; deliver value `A`, then `B`, then `A`; assert an `event` entity exists, that it fired event_types `"A"`, `"B"`, `"A"` in order, that `event_types` grew to include both `A` and `B`, and that the fired event state exposes no attributes beyond the standard `event_type` (no custom payload).
- [ ] **Single-value momentary:** deliver the single-value field (motion/doorbell example) twice; assert each transmission fires that one type.
- [ ] **Persistence + restart:** assert observed types are written to the devices map under `DEVICE_EVENT_TYPES`; then in a fresh setup pre-seed `entry.data[CONF_DEVICES][key][DEVICE_EVENT_TYPES][field]` and assert the rebuilt entity exposes those `event_types` **before** any new event arrives.
- [ ] **Always available:** advance time past the 600 s timeout and run `coordinator._async_watchdog(...)`; assert the event entity state is **not** `unavailable` (while a sibling measurement sensor on the same device *is* unavailable).
- [ ] **No double-fire on watchdog re-dispatch:** record the fired-event count, advance past the timeout, run the watchdog (which re-dispatches the cached last event), and assert the event count is **unchanged** (the identity dedupe held).
- [ ] **Restore across reload:** fire an event, reload the entry, and assert HA restored the last fired event (the entity's `state` reflects the last fire) — i.e. construction did not wipe it and did not double-fire.
- [ ] `uv run pytest tests/` passes in full, with existing sensor/binary_sensor tests unchanged.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Test files: `tests/test_mapping.py`, `tests/test_lifecycle.py`.
- Reuse the existing `tests/test_lifecycle.py` harness: the `_no_socket` autouse
  fixture, `_setup_hub(hass, hub_entry_builder, devices=...)`, `_feed(coordinator,
  event)` (injects a JSON event via `_handle_text_frame`), `_coordinator(...)`,
  and the `hub_entry_builder` fixture (`tests/conftest.py`).
- The event field must be a **mapped, observed** field for an entity to be
  created — seed the device's `DEVICE_FIELDS` to include the event field (and/or
  feed an event carrying it with discovery on), exactly as the existing
  binary-sensor lifecycle test does.

## Input Dependencies
- Task 1 — the `Rtl433Event` entity, `DEVICE_EVENT_TYPES`, `async_upsert_event_types`,
  and `Platform.EVENT` wiring.
- Task 2 — the three example mappings the schema tests assert against and that
  the lifecycle tests use as real mapped event fields.

## Output Artifacts
- New schema tests in `test_mapping.py` and event-platform lifecycle tests in
  `test_lifecycle.py`.

## Implementation Notes

<details>
<summary>Detailed implementation guidance + Meaningful Test Strategy</summary>

### Meaningful Test Strategy Guidelines
Mantra: **"write a few tests, mostly integration."** Test YOUR code, not HA's:
- **DO test**: the watchdog identity-dedupe (no double-fire), value-as-type
  firing + auto-populated `event_types` growth, persistence into the devices map
  and rebuild-from-persisted, always-available, the no-`__init__`-replay /
  restore-across-reload behavior, and that the three example fields resolve as
  `event`.
- **DON'T test**: that `EventEntity._trigger_event` validates types, that HA
  stores/restores the last event generically, YAML parsing mechanics, or
  framework lifecycle — those are upstream-tested. Combine related scenarios into
  single tests rather than one-assertion-per-test.

### Capturing fired events
The cleanest signal is the entity **state**: an `event` entity's `state` is the
ISO timestamp of the last fire and its `event_type` attribute is the last fired
type. To verify the *sequence* and *count* across multiple transmissions, listen
for HA state changes on the entity_id, or assert on `event_type` after each
`_feed` + `await hass.async_block_till_done()`. For the "no double-fire" check,
the simplest robust approach is to track the entity's last-fired timestamp/state
object before vs. after the watchdog tick (it must not change), or subscribe to
the entity via `async_track_state_change_event` and count change events.

### Delivering events through the live coordinator
Mirror `test_new_device_added_when_discovery_on` / the binary-sensor lifecycle
test:
```python
hub = await _setup_hub(hass, hub_entry_builder, discovery_enabled=True)
coordinator = _coordinator(hass, hub)
_feed(coordinator, {"model": "Foo", "id": 7, "<event_field>": "A"})
await hass.async_block_till_done()
```
For a pre-seeded device (entity exists before any event), pass
`devices={device_key: {CONF_MODEL: "...", DEVICE_FIELDS: ["<event_field>"], ...}}`
to `_setup_hub`. For the **rebuild-from-persisted** test, also seed
`DEVICE_EVENT_TYPES: {"<event_field>": ["A", "B"]}` in that record and assert the
entity's `event_types` capability attribute (or `state_attributes`) lists `A` and
`B` before feeding anything.

### Watchdog / always-available
Reuse the pattern from `test_last_seen_updates_and_stays_available`
(`test_lifecycle.py:744`): capture the last-seen time, then
```python
with freeze_time(seen_at + timedelta(seconds=601)):
    await coordinator._async_watchdog(dt_util.utcnow())
await hass.async_block_till_done()
```
Assert the event entity's state `!= "unavailable"` and that a sibling sensor on
the same device *is* `unavailable`. Assert the event count / last-fire timestamp
is unchanged across this tick (the watchdog re-dispatches `coordinator.devices[
device_key]`, the same object, so the identity dedupe must suppress a re-fire).

### Restore across reload
Fire an event, then
`await hass.config_entries.async_reload(hub.entry_id)` and
`await hass.async_block_till_done()`. Assert the rebuilt entity's `state` reflects
the last fired event (HA's `async_internal_added_to_hass` restores it) and that
reload did **not** itself fire a new event (compare event_type/timestamp). Use
`mock_restore_cache` if needed, following `test_restore_entity_restores_last_state`.

### Persistence assertion
After feeding `A` then `B`, read `hub.entry.data[CONF_DEVICES][device_key][
DEVICE_EVENT_TYPES][field_key]` (note `hub.entry` may be re-fetched via
`hass.config_entries.async_get_entry(hub.entry_id)`) and assert it equals the
sorted `["A", "B"]`.
</details>
