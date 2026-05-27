---
id: 1
group: "event-platform"
dependencies: []
status: "pending"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# Event platform: const, persistence helper, entity, and wiring

## Objective
Add the third HA entity platform, `event`, to the integration. This is the
production-code deliverable and spans three existing files plus one new file:

1. **`const.py`** — append `Platform.EVENT` to `PLATFORMS` (so it is forwarded
   for the hub entry) and add a new devices-map sub-key constant
   `DEVICE_EVENT_TYPES`.
2. **`entity.py`** — add a new idempotent persistence helper
   `async_upsert_event_types(...)` that unions observed event types into the
   hub devices map (the existing `async_upsert_device` signature is left
   unchanged).
3. **`event.py`** (new) — a thin platform wrapper (mirroring `binary_sensor.py`)
   defining `PLATFORM = "event"`, the `Rtl433Event(Rtl433Entity, EventEntity)`
   entity, and an `async_setup_entry` that delegates to
   `async_setup_hub_platform`.

The `Rtl433Event` entity fires an HA event once per genuine transmission, with
the stringified field value as the `event_type` and **no extra attributes**. It
auto-populates `event_types` from observed values, persists them, dedupes the
coordinator watchdog's re-dispatch by **object identity**, stays **always
available**, and does **not** replay the coordinator's last event on
construction.

## Skills Required
- `python` — multiple-inheritance entity subclassing, frozen-dataclass identity
  semantics (`is`), list copy vs. in-place append, async task scheduling.
- `home-assistant` — `EventEntity` (`event_types`, `_trigger_event`,
  `EventDeviceClass`, the `@final` restore/`capability_attributes` contract),
  the existing `Rtl433Entity` base, `async_setup_hub_platform`, config-entry
  `async_update_entry`.

## Acceptance Criteria
- [ ] `const.py`: `PLATFORMS` is `[Platform.SENSOR, Platform.BINARY_SENSOR, Platform.EVENT]`.
- [ ] `const.py`: `DEVICE_EVENT_TYPES: Final = "event_types"` is defined next to `DEVICE_FIELDS`/`DEVICE_TIMEOUT_OVERRIDE`, with a comment that it holds `{field_key: sorted list[str]}` per device record.
- [ ] `entity.py`: `async_upsert_event_types(hass, entry, device_key, field_key, types)` exists. It deep-copies the devices map, unions `types` into `rec[DEVICE_EVENT_TYPES][field_key]`, stores the result **sorted**, and calls `hass.config_entries.async_update_entry(...)` **only when the stored set actually grows** (no-op write otherwise). It tolerates a record with no `DEVICE_EVENT_TYPES` key yet (treats it as `{}`).
- [ ] `async_upsert_device`'s signature and behavior are unchanged.
- [ ] New file `event.py` defines `PLATFORM = "event"` and `Rtl433Event(Rtl433Entity, EventEntity)`.
- [ ] `Rtl433Event.__init__(self, coordinator, hub_entry_id, device_key, model, descriptor)` (the shared five-arg signature) calls `super().__init__(...)`, sets `_attr_device_class` from `descriptor.device_class` (an `EventDeviceClass` value or `None`), and sets `_attr_event_types` to a **copy** of the persisted list read from `coordinator.entry.data[CONF_DEVICES][device_key][DEVICE_EVENT_TYPES][field_key]` (defaulting to `[]`). It does **not** seed/replay `coordinator.devices[device_key]`.
- [ ] `Rtl433Event` initializes `self._last_fired_event = None` in `__init__`.
- [ ] `Rtl433Event` **overrides `_handle_dispatch`** (not just `_apply_value`): if `event is self._last_fired_event` it calls `async_write_ha_state()` and returns without firing; otherwise, if `self._descriptor.field_key in event.fields`, it records `self._last_fired_event = event`, computes `event_type = str(value)`, appends it to `_attr_event_types` and schedules persistence **if absent**, calls `self._trigger_event(event_type)` (no attribute dict), and `async_write_ha_state()`. If the field is absent it just writes state.
- [ ] Persistence on a newly seen type is scheduled via `self.hass.async_create_task(async_upsert_event_types(self.hass, self.coordinator.entry, self._device_key, self._descriptor.field_key, [event_type]))` (callback-safe, mirroring the field listener).
- [ ] `available` is overridden to return `True` unconditionally.
- [ ] `_async_restore_state` is overridden as a **no-op** (`async def ... : return`/docstring only). `async_internal_added_to_hass` and `async_added_to_hass` are **not** overridden.
- [ ] `_apply_value` is implemented as a harmless no-op (the base declares it abstract; `Rtl433Event` never calls it because it overrides `_handle_dispatch`).
- [ ] `event.py`'s `async_setup_entry` delegates to `await async_setup_hub_platform(hass, entry, async_add_entities, PLATFORM, Rtl433Event)` (no `per_device_factory`). No hub-level event entity is created.
- [ ] `uv run ruff check custom_components/rtl_433` is clean. (Full tests land in Task 3.)

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `custom_components/rtl_433/const.py`, `custom_components/rtl_433/entity.py`,
  new `custom_components/rtl_433/event.py`.
- Reuse the existing base `Rtl433Entity` (`entity.py`) — it owns identity,
  `DeviceInfo`/`via_device`, the dispatcher subscription lifecycle, and the
  "restore then time out" `last_seen` baseline. `Rtl433Event` diverges from it
  in exactly four places: it overrides `_handle_dispatch` (to dedupe + fire),
  `available` (always True), `_async_restore_state` (no-op), and seeds
  `_attr_event_types`/`_attr_device_class` in `__init__`.
- HA imports: `from homeassistant.components.event import EventEntity, EventDeviceClass`.
- The coordinator exposes its `ConfigEntry` as `coordinator.entry` (used by the
  watchdog and elsewhere) — the entity reads persisted types from
  `coordinator.entry.data` and passes `coordinator.entry` to the persistence helper.

## Input Dependencies
None — leaf production task. The example mappings (Task 2) and tests (Task 3)
consume this code but are not needed to write it.

## Output Artifacts
- `Platform.EVENT` forwarded; `DEVICE_EVENT_TYPES` const; `async_upsert_event_types`
  helper; `event.py` with `Rtl433Event` + `async_setup_entry`. Consumed by Task 3's
  tests and documented by Task 4.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Why override `_handle_dispatch`, not `_apply_value` (the watchdog dedupe)
The coordinator's watchdog re-dispatches the **cached last event** when a device
goes stale (`coordinator/base.py` `_async_watchdog` → `_dispatch(device_key,
self.devices[device_key])`). The base `_handle_dispatch` (`entity.py:191`) would
re-run the fire path and emit a duplicate HA event. A live transmission, by
contrast, is a **fresh** `normalize()` object (`coordinator/base.py:434`). So
`Rtl433Event` dedupes by **object identity**:

```python
@callback
def _handle_dispatch(self, event: NormalizedEvent) -> None:
    # Watchdog re-dispatch of the cached last event -> same object -> don't re-fire.
    if event is self._last_fired_event:
        self.async_write_ha_state()
        return
    field_key = self._descriptor.field_key
    if field_key in event.fields:
        self._last_fired_event = event
        event_type = str(event.fields[field_key])
        if event_type not in self._attr_event_types:
            self._attr_event_types.append(event_type)
            self.hass.async_create_task(
                async_upsert_event_types(
                    self.hass,
                    self._coordinator.entry,
                    self._device_key,
                    field_key,
                    [event_type],
                )
            )
        self._trigger_event(event_type)  # no attributes (YAGNI)
    self.async_write_ha_state()
```

Use identity (`is`), **not** value-equality: `NormalizedEvent` is a frozen
dataclass, so a genuine repeat of the same value is a *distinct* object that must
fire (a doorbell pressed twice in 30 s fires twice); only the watchdog re-sends
the same object reference.

### `__init__` — seed `_attr_event_types` from persisted types, do NOT replay
HA's `@final` `capability_attributes` reads `event_types` (backed by
`_attr_event_types`) and raises if it is unset, so `_attr_event_types` **must**
be set in `__init__`. Seed it with a **copy** so in-place growth never mutates
the persisted dict:

```python
def __init__(self, coordinator, hub_entry_id, device_key, model, descriptor) -> None:
    super().__init__(coordinator, hub_entry_id, device_key, model, descriptor)
    self._attr_device_class = descriptor.device_class  # an EventDeviceClass str or None
    persisted = (
        coordinator.entry.data.get(CONF_DEVICES, {})
        .get(device_key, {})
        .get(DEVICE_EVENT_TYPES, {})
        .get(descriptor.field_key, [])
    )
    self._attr_event_types = list(persisted)
    self._last_fired_event: NormalizedEvent | None = None
```

Do **not** seed from `coordinator.devices[device_key]` the way
`Rtl433Sensor`/`Rtl433BinarySensor` do — replaying on construction would fire a
stale event before the entity is added to hass. The last *displayed* event is
restored for free by HA's `EventEntity.async_internal_added_to_hass` (a `@final`,
HA-owned hook), which is why `_async_restore_state` is a no-op here.

`_attr_device_class` accepts the plain string from the descriptor;
`EventEntity.device_class` is a `cached_property` returning it. You may import
`EventDeviceClass` and leave the raw string as-is (HA accepts the member value),
but importing the enum documents intent and is harmless.

### `available` and restore
```python
@property
def available(self) -> bool:
    """Always available: events are momentary, so timeout-based unavailability
    would hide the entity almost always (mirrors the Last-seen sensor)."""
    return True

async def _async_restore_state(self) -> None:
    """No-op: HA's EventEntity.async_internal_added_to_hass restores the last
    displayed event; there is no steady measurement state to restore."""

def _apply_value(self, raw_value) -> None:
    """No-op: Rtl433Event overrides _handle_dispatch and never calls this."""
```
Do **not** override `async_added_to_hass` or `async_internal_added_to_hass` —
the base's super() chain and HA's restore must stay intact.

### `const.py` edits
```python
from homeassistant.const import Platform
PLATFORMS: Final[list[Platform]] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
]
...
DEVICE_FIELDS: Final = "fields"
DEVICE_TIMEOUT_OVERRIDE: Final = "timeout_override"
DEVICE_EVENT_TYPES: Final = "event_types"  # {field_key: sorted list[str]} per record
```

### `entity.py` — `async_upsert_event_types`
Mirror `async_upsert_device`'s idempotent union-write pattern (write only on
change) so concurrent writes converge. Import `DEVICE_EVENT_TYPES` from `.const`.

```python
async def async_upsert_event_types(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_key: str,
    field_key: str,
    types: Iterable[str],
) -> None:
    """Union observed event types into entry.data[CONF_DEVICES][device_key]
    [DEVICE_EVENT_TYPES][field_key], stored sorted. Writes only when the set grows."""
    devices = {k: dict(v) for k, v in entry.data.get(CONF_DEVICES, {}).items()}
    rec = devices.setdefault(device_key, {CONF_MODEL: "", DEVICE_FIELDS: []})
    by_field = {k: list(v) for k, v in rec.get(DEVICE_EVENT_TYPES, {}).items()}
    merged = sorted(set(by_field.get(field_key, [])) | set(types))
    if merged == by_field.get(field_key, []):
        return
    by_field[field_key] = merged
    rec[DEVICE_EVENT_TYPES] = by_field
    devices[device_key] = rec
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_DEVICES: devices}
    )
```
(Deep-copy the per-field dict like the device-record copy so the stored data is
never mutated in place.)

### `event.py` — the thin wrapper (model after `binary_sensor.py`)
```python
"""Event platform for the rtl_433 hub config entry. ..."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import CONF_DEVICES, DEVICE_EVENT_TYPES, DOMAIN
from .entity import Rtl433Entity, async_setup_hub_platform, async_upsert_event_types
if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator
    from .mapping import FieldDescriptor
    from .normalizer import NormalizedEvent

PLATFORM = "event"

class Rtl433Event(Rtl433Entity, EventEntity):
    ...  # as above

async def async_setup_entry(hass, entry, async_add_entities) -> None:
    await async_setup_hub_platform(hass, entry, async_add_entities, PLATFORM, Rtl433Event)
```
`Rtl433Entity` exposes the coordinator as `self._coordinator` and the device key
as `self._device_key`; use those inside `_handle_dispatch`.

### Gotchas
- `_attr_event_types` must be a real list set in `__init__` (a copy of persisted),
  never left unset — HA's `capability_attributes`/`event_types` will raise otherwise.
- Append the new type to `_attr_event_types` **before** calling `_trigger_event`
  (HA validates against the current list and raises `ValueError` for an unknown type).
- An **empty** `event_types` at creation is valid in HA (state is `None`); no
  deferred-creation path is needed.
- `_trigger_event(event_type)` takes the type only — pass no attribute dict.
</details>
