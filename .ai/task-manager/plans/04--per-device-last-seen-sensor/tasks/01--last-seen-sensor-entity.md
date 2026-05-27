---
id: 1
group: "entities"
dependencies: []
status: "pending"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# Synthetic per-device "Last seen" sensor entity

## Objective
Add a dedicated `Rtl433LastSeenSensor(Rtl433Entity, SensorEntity)` class in
`custom_components/rtl_433/sensor.py` that exposes, per device, the
`device_class=timestamp` "Last seen" datetime. It is **not** driven by a real
rtl_433 field: it is built from a small synthetic `FieldDescriptor`, holds its
**own** `native_value`, sources that value from `coordinator.last_seen` only when
a real event has been seen (`coordinator.devices[device_key]` present),
restores a tz-aware datetime across restarts, updates on dispatch, and stays
**available whenever it has a value** (overriding the base's timeout-based
availability). This task adds only the class (and its synthetic descriptor); the
wiring that creates one per device is Task 2.

## Skills Required
- `python` — entity subclassing, dataclass instance construction, datetime handling.
- `home-assistant` — `SensorEntity`, `SensorDeviceClass.TIMESTAMP`, `RestoreEntity`/`async_get_last_state`, `dt_util`, `EntityCategory`.

## Acceptance Criteria
- [ ] A module-level synthetic `FieldDescriptor` exists with a sentinel `field_key` that can never match a real rtl_433 JSON key (e.g. `"__last_seen__"`), `platform="sensor"`, `name="Last seen"`, `object_suffix="last_seen"`, `device_class="timestamp"`, `entity_category="diagnostic"`, `enabled_by_default=True`.
- [ ] `Rtl433LastSeenSensor(Rtl433Entity, SensorEntity)` is defined with `__init__(self, coordinator, hub_entry_id, device_key, model)` (no descriptor argument — it passes the synthetic descriptor to `super().__init__`). Its `unique_id` therefore ends `:last_seen` and its `EntityCategory` is `DIAGNOSTIC`, enabled by default.
- [ ] `_attr_device_class = SensorDeviceClass.TIMESTAMP` is set on the class (or in `__init__`); `state_class` and `native_unit_of_measurement` are left unset/`None`.
- [ ] At construction, `native_value` is seeded from `coordinator.last_seen.get(device_key)` **only if** `coordinator.devices.get(device_key) is not None`; otherwise it is left `None`.
- [ ] `_async_restore_state` is overridden: if `native_value` is still `None`, it restores the prior value from `await self.async_get_last_state()` by parsing the prior state string with `dt_util.parse_datetime(...)` into a tz-aware datetime (skipping `None`/`"unknown"`/`"unavailable"`/unparseable). A live seeded value wins over a restored one.
- [ ] `_handle_dispatch` is overridden to set `native_value = coordinator.last_seen.get(device_key)` and then call `async_write_ha_state()` (it does **not** call `_apply_value`, since the sentinel `field_key` never appears in an event).
- [ ] `available` is overridden to return `True` whenever `native_value is not None`, independent of the per-device silence timeout.
- [ ] `_apply_value` is implemented as a harmless no-op (the base declares it abstract, but this entity never calls it).
- [ ] `uv run ruff check custom_components/rtl_433` is clean. (Wiring + full tests land in Tasks 2 and 3.)

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `custom_components/rtl_433/sensor.py`.
- Reuse the existing base `Rtl433Entity` (in `entity.py`): it owns identity,
  `DeviceInfo`/`via_device`, the dispatcher subscription lifecycle, and the
  "restore then time out" baseline in `async_added_to_hass`. This entity diverges
  from it in exactly three places — value source, restore type, and availability.
- `FieldDescriptor` is imported from `.mapping` (already imported under
  `TYPE_CHECKING`; you will need it at runtime here, so import it normally).

## Input Dependencies
None — this is a leaf class. Task 2 imports and wires it.

## Output Artifacts
- `Rtl433LastSeenSensor` and the synthetic descriptor, consumed by Task 2's
  per-device creation hook and asserted by Task 3's tests.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Why an "own value" instead of reading `coordinator.last_seen` live
The base `Rtl433Entity.async_added_to_hass` baselines
`coordinator.last_seen[device_key] = dt_util.utcnow()` when the coordinator has
no entry yet (the "restore then time out" rule, `entity.py:168`). If this sensor
read `coordinator.last_seen` live for its displayed value, every restart would
show "now" instead of the restored prior time. So it must hold its **own**
`native_value` and only adopt `coordinator.last_seen` when a *real* event is
known to have occurred — which `coordinator.devices.get(device_key) is not None`
reliably signals (the devices map is written only by `_process_event` on a real
event, `coordinator/base.py:440`).

### Construction order vs. the base lifecycle
The base `async_added_to_hass` (do **not** override it) runs, in order:
`super()` (RestoreEntity) → baseline `last_seen=now` if missing → `await
self._async_restore_state()` → subscribe to `signal_device_update`. Because the
baseline writes `coordinator.last_seen` (not this entity's `native_value`), the
restore path below is unaffected by it.

### Imports to add at the top of `sensor.py`
```python
from datetime import datetime  # if you annotate native_value

from homeassistant.util import dt as dt_util
from .mapping import FieldDescriptor  # promote from TYPE_CHECKING to a real import
```
`SensorDeviceClass` is already imported. `apply_transform` is already imported
(used by `Rtl433Sensor`) — the Last-seen sensor does not use it.

### The synthetic descriptor + class
```python
# Sentinel field_key that no rtl_433 event can carry, so the base's
# field-driven _apply_value path is never triggered for this entity.
_LAST_SEEN_FIELD = "__last_seen__"

LAST_SEEN_DESCRIPTOR = FieldDescriptor(
    field_key=_LAST_SEEN_FIELD,
    platform="sensor",
    name="Last seen",
    object_suffix="last_seen",
    device_class="timestamp",
    entity_category="diagnostic",
    enabled_by_default=True,
)


class Rtl433LastSeenSensor(Rtl433Entity, SensorEntity):
    """Per-device diagnostic timestamp of when the device was last heard from.

    Synthetic (not field-driven): holds its own ``native_value``, seeded from a
    real event when one exists, restored otherwise, and updated on dispatch.
    Stays available once it has a value even after the device falls silent, so
    "last_seen older than X" staleness automations keep working.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, hub_entry_id, device_key, model) -> None:
        super().__init__(
            coordinator, hub_entry_id, device_key, model, LAST_SEEN_DESCRIPTOR
        )
        # Seed only when a *real* event has been seen this session; the presence
        # of a devices-map entry distinguishes a true timestamp from the base's
        # startup baseline (which never sets coordinator.devices).
        if coordinator.devices.get(device_key) is not None:
            self._attr_native_value = coordinator.last_seen.get(device_key)

    def _apply_value(self, raw_value) -> None:
        """No-op: the sentinel field_key never appears in an event."""

    async def _async_restore_state(self) -> None:
        """Restore the prior timestamp as a tz-aware datetime, if not seeded.

        A live value seeded from a real event wins over a restored one.
        """
        if self._attr_native_value is not None:
            return
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        restored = dt_util.parse_datetime(last_state.state)
        if restored is not None:
            self._attr_native_value = restored

    @callback
    def _handle_dispatch(self, event) -> None:
        """Adopt the coordinator's last_seen on a real/watchdog dispatch.

        Overrides the base: the synthetic field is never in ``event.fields``, so
        instead of the field-driven path we read coordinator.last_seen, which the
        non-dispatching baseline never reaches.
        """
        self._attr_native_value = self._coordinator.last_seen.get(self._device_key)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available once a real/restored timestamp exists, ignoring the timeout."""
        return self._attr_native_value is not None
```
Import `callback` from `homeassistant.core` (already imported in `sensor.py` via
`from homeassistant.core import HomeAssistant` — add `callback` to that import).

### Restore type — why `dt_util.parse_datetime`, not RestoreSensor
A `timestamp` sensor needs a tz-aware datetime, and the existing string-assign
restore in `Rtl433Sensor._async_restore_state` would yield a `str`. The base
already extends `RestoreEntity`, so the lowest-risk path is to parse the prior
ISO state string with `dt_util.parse_datetime` inside the overridden
`_async_restore_state` (avoids `RestoreSensor` MRO surgery on the shared base).
`coordinator.last_seen` is already `dt_util.utcnow()` (tz-aware), so live/dispatch
values need no conversion.

### Gotchas
- Do **not** override `async_added_to_hass`; the base already does the right
  thing and calls your `_async_restore_state`.
- Do **not** read `coordinator.last_seen` for the displayed value anywhere except
  `_handle_dispatch` and the guarded constructor seed.
- Keep `object_suffix="last_seen"` — confirmed unused by any shipped descriptor
  (the `time` field uses `UTC`), so no unique_id collision.
- Do not set `entity_registry_enabled_default=False`; it ships enabled.
</details>
