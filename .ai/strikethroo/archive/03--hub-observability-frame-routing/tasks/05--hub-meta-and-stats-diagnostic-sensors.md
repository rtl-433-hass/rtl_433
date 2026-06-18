---
id: 5
group: "entities"
dependencies: [2, 4]
status: "completed"
created: "2026-05-26"
skills:
  - python
  - home-assistant
complexity_score: 5
complexity_notes: "Ten homogeneous DIAGNOSTIC sensors plus extra-state attributes in one file (sensor.py); repetitive but broad. Kept as one task because all sensors share the hub-entity base and read sibling coordinator state, and splitting would serialize edits to the same file with no parallelism gain."
---
# Hub meta/SDR + server-stats diagnostic sensors

## Objective
Add `EntityCategory.DIAGNOSTIC` sensors on the hub device that render the
coordinator's HTTP-getter-sourced hub state. SDR/meta sensors (from
`coordinator.meta`): **center frequency**, **sample rate**, **conversion mode**,
**hop interval**, **gain** (empty ⇒ "auto"), **frequency correction (ppm)**.
Server-stats sensors (from `coordinator.stats`): **decoded events**
(`total_increasing`), **OOK frames**, **FSK frames**, **enabled decoders**. The
array-valued `frequencies`/`hop_times` are extra-state attributes on a meta
sensor; the per-protocol `stats[]` array and the tz-naive `since` string are
extra-state attributes on the decoded-events sensor. All read live coordinator
state and refresh on `signal_hub_update`.

## Skills Required
- `python` — entity subclassing, value extraction, attribute exposure.
- `home-assistant` — `SensorEntity`, device classes/units/state classes, `EntityCategory`.

## Acceptance Criteria
- [ ] Six SDR/meta DIAGNOSTIC sensors are registered on the hub device, each reading from `coordinator.meta`: center frequency (`center_frequency`, Hz, `SensorDeviceClass.FREQUENCY`), sample rate (`samp_rate`, Hz), conversion mode (`conversion_mode`, integer), hop interval (`hop_interval`, seconds), gain (`gain`; empty string rendered as `"auto"`), frequency correction (`ppm_error`, integer). Missing keys ⇒ native value `None` (state `unknown`).
- [ ] Four server-stats DIAGNOSTIC sensors are registered on the hub device, reading from `coordinator.stats`: decoded events (`frames.events`, `state_class=total_increasing`), OOK frames (`frames.count`), FSK frames (`frames.fsk`), enabled decoders (`enabled`).
- [ ] `coordinator.meta["frequencies"]` and `coordinator.meta["hop_times"]` are exposed as extra-state attributes on one meta sensor (center frequency or hop interval).
- [ ] `coordinator.stats["stats"]` (per-protocol array) and `coordinator.stats["since"]` are extra-state attributes on the decoded-events sensor. `since` is **not** a timestamp sensor.
- [ ] Each sensor has a stable `unique_id` (`f"{entry_id}:hub:<suffix>"`), uses the `Rtl433HubEntity` base (Task 4), and refreshes on `signal_hub_update`.
- [ ] All ten hub sensors are added in `sensor.py`'s `async_setup_entry`, in addition to the existing per-device `async_setup_hub_platform` call.
- [ ] Tests in `tests/test_lifecycle.py` drive mocked hub state (set `coordinator.meta` / `coordinator.stats`, dispatch `signal_hub_update`) and assert the meta sensors (including `gain="auto"` for empty string and the ppm value) and stats sensors (events/OOK/FSK/enabled) populate, and that `frequencies`/`hop_times` and `stats[]`/`since` appear as attributes.
- [ ] `uv run pytest tests/` passes; `uv run ruff check custom_components/rtl_433` is clean.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- File: `custom_components/rtl_433/sensor.py`; tests in `tests/test_lifecycle.py`.
- Read from `coordinator.meta` / `coordinator.stats` (Task 2) via the `Rtl433HubEntity` base (Task 4).

## Input Dependencies
- Task 2: `coordinator.meta` / `coordinator.stats` populated by the HTTP getters.
- Task 4: `Rtl433HubEntity` base class in `entity.py` and the `signal_hub_update` refresh mechanism.

## Output Artifacts
- The hub diagnostic sensors (referenced by docs in Task 6 and Plan Self-Validation #4/#7).

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Approach: a small description-driven set
Keep it DRY with a lightweight per-sensor description (a frozen dataclass or
`SensorEntityDescription` subclass) carrying: `key`/suffix, name, an extractor
callable `(coordinator) -> value`, optional device_class/unit/state_class, and an
optional extra-attributes callable. Then one `Rtl433HubSensor(Rtl433HubEntity,
SensorEntity)` reads via the description. This avoids ten near-identical classes.

```python
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorStateClass,
)
from homeassistant.const import UnitOfFrequency
from homeassistant.helpers.entity import EntityCategory
from .const import DOMAIN
from .entity import Rtl433HubEntity, Rtl433Entity, async_setup_hub_platform

if TYPE_CHECKING:
    from .coordinator import Rtl433Coordinator


@dataclass(frozen=True, kw_only=True)
class HubSensorDesc:
    suffix: str
    name: str
    value: Callable[[Rtl433Coordinator], Any]
    device_class: SensorDeviceClass | None = None
    native_unit: str | None = None
    state_class: SensorStateClass | None = None
    attrs: Callable[[Rtl433Coordinator], dict[str, Any] | None] | None = None
```

### Extractors (read defensively — missing ⇒ None)
```python
def _meta(coord, key):  # helper
    return coord.meta.get(key)

def _frames(coord, key):
    frames = coord.stats.get("frames")
    return frames.get(key) if isinstance(frames, dict) else None

def _gain(coord):
    gain = coord.meta.get("gain")
    if gain is None:
        return None
    return "auto" if gain == "" else gain
```

### The description list
```python
HUB_SENSORS: tuple[HubSensorDesc, ...] = (
    HubSensorDesc(
        suffix="center_frequency", name="Center frequency",
        value=lambda c: _meta(c, "center_frequency"),
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit=UnitOfFrequency.HERTZ,
        attrs=lambda c: {
            "frequencies": c.meta.get("frequencies"),
            "hop_times": c.meta.get("hop_times"),
        },
    ),
    HubSensorDesc(
        suffix="sample_rate", name="Sample rate",
        value=lambda c: _meta(c, "samp_rate"),
        native_unit="Hz",
    ),
    HubSensorDesc(
        suffix="conversion_mode", name="Conversion mode",
        value=lambda c: _meta(c, "conversion_mode"),
    ),
    HubSensorDesc(
        suffix="hop_interval", name="Hop interval",
        value=lambda c: _meta(c, "hop_interval"),
        native_unit="s",
    ),
    HubSensorDesc(suffix="gain", name="Gain", value=_gain),
    HubSensorDesc(
        suffix="ppm_error", name="Frequency correction",
        value=lambda c: _meta(c, "ppm_error"),
    ),
    HubSensorDesc(
        suffix="decoded_events", name="Decoded events",
        value=lambda c: _frames(c, "events"),
        state_class=SensorStateClass.TOTAL_INCREASING,
        attrs=lambda c: {
            "stats": c.stats.get("stats"),
            "since": c.stats.get("since"),
        },
    ),
    HubSensorDesc(
        suffix="ook_frames", name="OOK frames",
        value=lambda c: _frames(c, "count"),
    ),
    HubSensorDesc(
        suffix="fsk_frames", name="FSK frames",
        value=lambda c: _frames(c, "fsk"),
    ),
    HubSensorDesc(
        suffix="enabled_decoders", name="Enabled decoders",
        value=lambda c: c.stats.get("enabled"),
    ),
)
```

(Confirm `UnitOfFrequency.HERTZ` exists in the installed HA; otherwise use the
string `"Hz"`. Sample rate / hop interval use plain unit strings to avoid
device-class/unit validation pitfalls — only center frequency uses the
`FREQUENCY` device class with `HERTZ`.)

### The entity
```python
class Rtl433HubSensor(Rtl433HubEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, hub_entry_id, desc: HubSensorDesc) -> None:
        super().__init__(coordinator, hub_entry_id)
        self._desc = desc
        self._attr_unique_id = f"{hub_entry_id}:hub:{desc.suffix}"
        self._attr_name = desc.name
        self._attr_device_class = desc.device_class
        self._attr_native_unit_of_measurement = desc.native_unit
        self._attr_state_class = desc.state_class

    @property
    def native_value(self):
        return self._desc.value(self._coordinator)

    @property
    def extra_state_attributes(self):
        if self._desc.attrs is None:
            return None
        attrs = self._desc.attrs(self._coordinator)
        # Drop None values so unknown arrays don't clutter the UI.
        return {k: v for k, v in (attrs or {}).items() if v is not None} or None
```

### Register in `async_setup_entry`
```python
async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        Rtl433HubSensor(coordinator, entry.entry_id, desc) for desc in HUB_SENSORS
    )
    await async_setup_hub_platform(
        hass, entry, async_add_entities, PLATFORM, Rtl433Sensor
    )
```

### Tests (`tests/test_lifecycle.py`)
Set coordinator hub state directly and dispatch the signal (no socket / no HTTP
needed here — the getters are tested in Task 2):

```python
async def test_hub_diagnostic_sensors(hass, hub_entry_builder):
    hub = await _setup_hub(hass, hub_entry_builder)
    coordinator = _coordinator(hass, hub)
    coordinator.meta = {
        "center_frequency": 433920000, "samp_rate": 250000,
        "conversion_mode": 1, "hop_interval": 600,
        "frequencies": [433920000], "hop_times": [600],
        "gain": "", "ppm_error": 0,
    }
    coordinator.stats = {
        "enabled": 5, "since": "2026-05-26T10:00:00",
        "frames": {"count": 12, "fsk": 3, "events": 40},
        "stats": [{"name": "Acurite", "events": 40}],
    }
    async_dispatcher_send(hass, signal_hub_update(hub.entry_id))
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    def state(suffix):
        eid = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{hub.entry_id}:hub:{suffix}")
        assert eid is not None, suffix
        return hass.states.get(eid)

    assert state("center_frequency").state == "433920000"
    assert state("gain").state == "auto"               # empty string -> auto
    assert state("ppm_error").state == "0"
    assert state("decoded_events").state == "40"
    assert state("decoded_events").attributes["state_class"] == "total_increasing"
    assert state("ook_frames").state == "12"
    assert state("fsk_frames").state == "3"
    assert state("enabled_decoders").state == "5"
    # Array fields are attributes, not their own entities.
    assert state("center_frequency").attributes["frequencies"] == [433920000]
    assert "since" in state("decoded_events").attributes
```

### Gotchas
- Diagnostic sensors may be disabled-by-default in some setups; here they should be enabled (do not set `entity_registry_enabled_default=False`).
- Keep `unique_id` suffixes stable.
- Read every key defensively; a missing key must yield `None`, not raise.
- Don't make `since` a timestamp sensor — it is timezone-naive; expose it as an attribute only.
- Avoid pairing a numeric `device_class` with a missing/`None` unit (HA validation errors). Only center frequency carries a device class.
</details>
