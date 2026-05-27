---
id: 3
group: "testing"
dependencies: [1, 2]
status: "completed"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# Tests for the per-device "Last seen" sensor

## Objective
Add focused integration tests (in `tests/test_lifecycle.py`) that prove the
Last-seen sensor's behavior end-to-end through `async_setup_entry` and the live
coordinator: creation in both setup paths (including a device with no mapped
fields), the value/update behavior, the **restore-not-baseline** correctness
point, the always-available override after the silence timeout, that the
restored/displayed value is a `timestamp`, and that the `binary_sensor` platform
creates none.

## Skills Required
- `python` — pytest, `freeze_time`/timedelta, registry lookups, `dt_util`.
- `home-assistant` — `pytest-homeassistant-custom-component`, `mock_restore_cache`, entity/device registries, dispatcher.

## Acceptance Criteria
- [ ] **Creation in both paths, incl. no-field device:** With a seeded devices map containing one device with mapped fields and one device with **no** mapped fields, exactly one `:last_seen` sensor exists per device, enabled by default, with `device_class == "timestamp"` and `EntityCategory.DIAGNOSTIC`. A brand-new device fed via a live event (discovery on) also gets exactly one `:last_seen` sensor.
- [ ] **Value/update:** Feeding a fresh event for a device sets the Last-seen sensor's state to the coordinator's updated `last_seen` for that device (a tz-aware datetime; compare via `dt_util.parse_datetime(state.state)` equal to `coordinator.last_seen[device_key]`).
- [ ] **Always-available after timeout:** After advancing time past the effective timeout and running the watchdog (`coordinator._async_watchdog(...)`), the device's measurement sensor reads `unavailable` while the `:last_seen` sensor is still available and shows the unchanged prior timestamp.
- [ ] **Restore-not-baseline:** With a prior state seeded via `mock_restore_cache` (an ISO datetime string) and **no** live event, the `:last_seen` sensor reports the restored prior timestamp — not "now"/the base baseline. Then feeding a real event updates it to the fresh `coordinator.last_seen` value.
- [ ] **No binary_sensor Last-seen:** No `:last_seen` entity exists on the `binary_sensor` platform.
- [ ] `uv run pytest tests/` passes; `uv run ruff check custom_components/rtl_433` is clean.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `tests/test_lifecycle.py` (reuse its existing helpers: `_setup_hub`,
  `_coordinator`, `_feed`, the `_no_socket` autouse fixture, and the
  `hub_entry_builder` fixture).
- Look entities up by `unique_id` via the entity registry
  (`er.async_get(hass).async_get_entity_id(...)`) rather than guessing entity_ids.

## Input Dependencies
- Task 1: `Rtl433LastSeenSensor` behavior (own value, restore, dispatch, availability).
- Task 2: per-device creation wired into both setup paths.

## Output Artifacts
- New tests covering Plan Success Criteria #1–#6 and Self-Validation #3–#5.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Meaningful Test Strategy
"Write a few tests, mostly integration." Test **this** integration's custom
logic — the own-value/restore-not-baseline design and the always-available
override — not Home Assistant's RestoreEntity or SensorEntity machinery. Prefer
a small number of end-to-end tests through `async_setup_entry` + the live
coordinator (as the rest of `test_lifecycle.py` does) over many unit tests.

### Reusable scaffolding already in `tests/test_lifecycle.py`
- `_setup_hub(hass, hub_entry_builder, *, devices=None, **kwargs)` — sets up a hub
  with `availability_timeout=600` and returns it.
- `_coordinator(hass, hub)` — the live coordinator.
- `_feed(coordinator, event)` — inject one event exactly as the socket path would.
- The autouse `_no_socket` fixture stubs `_connect_loop` (no real socket).
- Look up by unique_id: `er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{hub.entry_id}:{device_key}:last_seen")`.

### Test: creation in both paths (incl. a no-field device)
```python
async def test_last_seen_created_for_every_device(hass, hub_entry_builder):
    mapped_key = "EnergyMeter-2000-1234"
    bare_key = "MysteryThing-7"  # no library-mapped fields
    hub = await _setup_hub(
        hass, hub_entry_builder,
        devices={
            mapped_key: {CONF_MODEL: "EnergyMeter-2000", DEVICE_FIELDS: ["power_W"]},
            bare_key: {CONF_MODEL: "MysteryThing", DEVICE_FIELDS: []},
        },
    )
    ent_reg = er.async_get(hass)
    for key in (mapped_key, bare_key):
        eid = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{hub.entry_id}:{key}:last_seen")
        assert eid is not None, key
        entry = ent_reg.async_get(eid)
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert entry.disabled_by is None  # enabled by default
    # device_class is on the state once added:
    eid = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{hub.entry_id}:{mapped_key}:last_seen")
    # (state may be unknown until an event; device_class is still in attributes)
```
Import `EntityCategory` from `homeassistant.helpers.entity`.

### Test: value/update + always-available after the watchdog
Mirror `test_watchdog_flips_unavailable_then_recovers` in `test_coordinator.py`
for the time-advance technique (`freeze_time` + `coordinator._async_watchdog`):
```python
from freezegun import freeze_time  # already used in tests/test_coordinator.py
from datetime import timedelta
from homeassistant.util import dt as dt_util

async def test_last_seen_updates_and_stays_available(hass, hub_entry_builder):
    device_key = "EnergyMeter-2000-1234"
    hub = await _setup_hub(
        hass, hub_entry_builder,
        devices={device_key: {CONF_MODEL: "EnergyMeter-2000", DEVICE_FIELDS: ["power_W"]}},
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    last_seen_eid = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{hub.entry_id}:{device_key}:last_seen")
    watts_eid = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{hub.entry_id}:{device_key}:watts")

    start = dt_util.utcnow()
    with freeze_time(start):
        _feed(coordinator, {"model": "EnergyMeter-2000", "id": 1234, "power_W": 5.0})
        await hass.async_block_till_done()
    # value equals the coordinator's last_seen (tz-aware datetime)
    assert dt_util.parse_datetime(hass.states.get(last_seen_eid).state) == coordinator.last_seen[device_key]
    assert hass.states.get(last_seen_eid).attributes["device_class"] == "timestamp"

    # advance past the 600s timeout and run the watchdog
    with freeze_time(start + timedelta(seconds=601)):
        await coordinator._async_watchdog(dt_util.utcnow())
        await hass.async_block_till_done()
    assert hass.states.get(watts_eid).state == "unavailable"
    # Last-seen stays available and keeps the prior timestamp
    assert hass.states.get(last_seen_eid).state != "unavailable"
    assert dt_util.parse_datetime(hass.states.get(last_seen_eid).state) == coordinator.last_seen[device_key]
```
Note: the watchdog dispatches to the device's entities; the Last-seen sensor's
`_handle_dispatch` re-reads `coordinator.last_seen` (unchanged since the last real
event), so its value is stable. The measurement sensor flips to `unavailable`
because the coordinator marked the device unavailable and its base `available`
property now reads `False`.

### Test: restore-not-baseline (the subtle correctness point)
Seed a prior state with `mock_restore_cache` and assert the restored value shows
*before* any event, then that a real event overwrites it. Derive the entity_id
from the slugified device name (model + key) + "last seen", or — more robustly —
register via `mock_restore_cache` using the entity_id the registry will assign.
The existing `test_restore_entity_restores_last_state` shows the entity_id naming
(`sensor.<slug(device_name)>_<slug(entity_name)>`). For device_key
`"Acurite-606TX-42"`, model `"Acurite-606TX"`, the Last-seen entity_id is
`sensor.acurite_606tx_acurite_606tx_42_last_seen`.
```python
from homeassistant.core import State
from pytest_homeassistant_custom_component.common import mock_restore_cache

async def test_last_seen_restores_prior_not_baseline(hass, hub_entry_builder):
    device_key = "Acurite-606TX-42"
    restore_eid = "sensor.acurite_606tx_acurite_606tx_42_last_seen"
    prior = "2026-05-20T08:30:00+00:00"
    mock_restore_cache(hass, (State(restore_eid, prior),))

    hub = await _setup_hub(
        hass, hub_entry_builder,
        devices={device_key: {CONF_MODEL: "Acurite-606TX", DEVICE_FIELDS: ["temperature_C"]}},
    )
    coordinator = _coordinator(hass, hub)
    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{hub.entry_id}:{device_key}:last_seen")

    # No live event yet: shows the restored prior time, NOT "now"/baseline.
    assert dt_util.parse_datetime(hass.states.get(eid).state) == dt_util.parse_datetime(prior)

    # A real event updates it to the fresh coordinator.last_seen.
    _feed(coordinator, {"model": "Acurite-606TX", "id": 42, "temperature_C": 21.4})
    await hass.async_block_till_done()
    assert dt_util.parse_datetime(hass.states.get(eid).state) == coordinator.last_seen[device_key]
    assert dt_util.parse_datetime(hass.states.get(eid).state) != dt_util.parse_datetime(prior)
```
If the hardcoded `restore_eid` proves brittle, set up the hub first, read the
assigned entity_id from the registry, then `mock_restore_cache` and reload — but
the hardcoded form matches the existing restore test's approach and should work.

### Test: no Last-seen on binary_sensor
```python
async def test_no_last_seen_on_binary_sensor(hass, hub_entry_builder):
    device_key = "GenericDoor-X1-88"
    hub = await _setup_hub(
        hass, hub_entry_builder,
        devices={device_key: {CONF_MODEL: "GenericDoor-X1", DEVICE_FIELDS: ["closed"]}},
    )
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{hub.entry_id}:{device_key}:last_seen") is None
    # but the sensor-platform Last-seen still exists for the device
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{hub.entry_id}:{device_key}:last_seen") is not None
```

### Imports you will likely add to the test module
`from datetime import timedelta`, `from freezegun import freeze_time`,
`from homeassistant.helpers.entity import EntityCategory`,
`from homeassistant.util import dt as dt_util`,
`from homeassistant.core import State` (already imported),
`from pytest_homeassistant_custom_component.common import mock_restore_cache`
(already imported). Reuse the existing `CONF_MODEL`, `DEVICE_FIELDS`, `DOMAIN`
imports at the top of the file.

### Gotchas
- Disabled-by-default would make the entity have no state; assert
  `entry.disabled_by is None` to prove it ships enabled.
- Compare datetimes, not raw strings — HA may render the timestamp in a
  normalized ISO form that differs textually from your input; parse both sides.
- Run the watchdog as a coroutine: `await coordinator._async_watchdog(dt_util.utcnow())`.
</details>
