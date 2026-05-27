---
id: 4
group: "control-entities"
dependencies: [1, 2]
status: "pending"
created: 2026-05-27
skills:
  - home-assistant
  - python
complexity_score: 5
complexity_notes: "Three new HA platforms sharing one hub-control base, plus the PLATFORMS change. Single skill domain and a repeated pattern modelled on the existing Rtl433HubSensor, kept as one cohesive 'control layer' deliverable."
---
# Control entity platforms: number / select / switch (managed mode)

## Objective
Create the three new control platforms that expose the registry's SDR fields as
first-class HA entities on the **hub device**: a `number` platform (center frequency,
sample rate, ppm, gain dB, hop interval), a `select` platform (conversion mode), and a
`switch` platform (Auto gain). Each entity is `EntityCategory.CONFIG`, reads the
coordinator's desired/actual state, and writes through `coordinator.set_sdr(...)`. Add
`Platform.NUMBER`, `Platform.SELECT`, `Platform.SWITCH` to `const.py`'s `PLATFORMS`
**in this task** (so the integration only forwards to platforms that now exist). All
control entities are created **only when `coordinator.manage_settings` is True**.

## Skills Required
- `home-assistant` — `NumberEntity`/`SelectEntity`/`SwitchEntity`, entity descriptions,
  `EntityCategory.CONFIG`, hub-device attachment, dispatcher refresh.
- `python` — a small shared base class and three thin platform modules.

## Acceptance Criteria
- [ ] `custom_components/rtl_433/number.py`, `select.py`, `switch.py` created, each with
      an `async_setup_entry` that returns immediately (creates no entities) when
      `coordinator.manage_settings` is False, and otherwise statically registers the
      hub control entities for that platform from the `sdr_settings` registry.
- [ ] A shared hub-control base (in `entity.py`, alongside `Rtl433HubEntity`) centralizes
      device attachment to the hub device, `EntityCategory.CONFIG`, the
      `signal_hub_update` subscription (to reflect read-back/actual changes), the stable
      unique_id `f"{entry_id}:hub:{object_suffix}"`, and the write→`coordinator.set_sdr`
      call.
- [ ] **Number** entities: center frequency, sample rate, ppm, gain (dB), hop interval —
      with the registry's min/max/step/unit, `NumberMode.BOX`, device class where
      defined. `async_set_native_value` calls `coordinator.set_sdr(<key>, value)`; the
      displayed `native_value` is the desired value, falling back to the coordinator's
      actual (`coordinator.meta`) when no desired value exists yet.
- [ ] **Select** entity: conversion mode with options `native`/`si`/`customary`;
      `async_select_option` maps the label → int and calls `coordinator.set_sdr(...)`;
      `current_option` reflects desired (or actual) mapped back to a label.
- [ ] **Switch** entity: "Auto gain"; `async_turn_on`/`async_turn_off` set the
      `gain_auto` desired value via `coordinator.set_sdr(...)`; `is_on` reflects it. When
      auto is on the paired gain-dB Number still exists but its value is not sent (the
      coordinator composes the empty `arg`).
- [ ] `PLATFORMS` in `const.py` includes `Platform.NUMBER`, `Platform.SELECT`,
      `Platform.SWITCH` (in addition to the existing SENSOR/BINARY_SENSOR/EVENT).
- [ ] Setting any control issues the correct `/cmd` command/argument (verified by Task
      6): `center_frequency` (val Hz), `sample_rate` (val Hz), `ppm_error` (val),
      `convert` (mapped int), `hop_interval` (val s), `gain` (`arg`=dB string when Auto
      off, empty `arg` when Auto on).
- [ ] `uv run ruff check custom_components/rtl_433` passes; the integration loads (no
      import errors) when set up with `manage_settings` both on and off.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Model the static hub registration on `sensor.py`'s `Rtl433HubSensor` /
  `async_setup_entry` (which does `async_add_entities(Rtl433HubSensor(coordinator,
  entry.entry_id, desc) for desc in HUB_SENSORS)`), but iterate the `sdr_settings`
  registry filtered by `platform`. Do **not** route through
  `async_setup_hub_platform` (that is the per-device dynamic flow; these are static
  hub-device entities).
- unique_id scheme: `f"{entry_id}:hub:{setting.object_suffix}"` — distinct per platform
  domain, so reusing a suffix string that a (now-suppressed) Plan 3 sensor used is safe.
- Entities attach to the hub device via `DeviceInfo(identifiers={(DOMAIN, entry_id)})`
  exactly like `Rtl433HubEntity`.
- Writes are optimistic: update happens via `coordinator.set_sdr`, then the entity
  re-reads on the next `signal_hub_update` (the coordinator emits one after read-back).
  Document the optimistic-then-confirmed contract in a docstring.

## Input Dependencies
- Task 1: the `sdr_settings` registry (entity-description params, command mapping,
  conversion mappers, gain helpers).
- Task 2: `coordinator.manage_settings`, `coordinator.set_sdr(field, value)`,
  `coordinator.get_desired(field)` / `is_managed(field)`, and `coordinator.meta` for the
  actual fallback.

## Output Artifacts
- `number.py`, `select.py`, `switch.py` (new), the shared hub-control base in
  `entity.py`, and the `PLATFORMS` update in `const.py`.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

**Shared base** (in `entity.py`, extend `Rtl433HubEntity` or add a sibling):
```python
class Rtl433HubControl(Rtl433HubEntity):
    _attr_entity_category = EntityCategory.CONFIG
    def __init__(self, coordinator, hub_entry_id, setting):
        super().__init__(coordinator, hub_entry_id)
        self._setting = setting
        self._attr_unique_id = f"{hub_entry_id}:hub:{setting.object_suffix}"
        self._attr_name = setting.name
    # _handle_hub_update from Rtl433HubEntity already re-writes state on read-back.
```
`Rtl433HubEntity` already wires `signal_hub_update`; controls inherit that so a
post-write read-back refresh repaints them.

**number.py** (mirror `sensor.py`'s setup):
```python
PLATFORM = "number"
class Rtl433NumberControl(Rtl433HubControl, NumberEntity):
    def __init__(self, coordinator, hub_entry_id, setting):
        super().__init__(coordinator, hub_entry_id, setting)
        self._attr_native_min_value = setting.native_min
        self._attr_native_max_value = setting.native_max
        self._attr_native_step = setting.native_step
        self._attr_native_unit_of_measurement = setting.native_unit
        self._attr_mode = setting.mode or NumberMode.BOX
        self._attr_device_class = setting.device_class  # if a NumberDeviceClass
    @property
    def native_value(self):
        desired = self._coordinator.get_desired(self._setting.key)
        if desired is not None:
            return desired
        return self._setting.read(self._coordinator.meta)  # actual fallback
    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.set_sdr(self._setting.key, value)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.manage_settings:
        return
    async_add_entities(
        Rtl433NumberControl(coordinator, entry.entry_id, s)
        for s in SDR_SETTINGS if s.platform == "number" and s.capability(coordinator.meta)
    )
```
Note: the gain-dB Number uses `setting.key == "gain"` (the dB value); the Auto switch
uses `"gain_auto"`.

**select.py** — `Rtl433SelectControl(Rtl433HubControl, SelectEntity)` with
`_attr_options = list(setting.options)`, `current_option` from desired/actual via
`conversion_val_to_label`, and `async_select_option` calling
`set_sdr(key, conversion_label_to_val(option))`.

**switch.py** — `Rtl433SwitchControl(Rtl433HubControl, SwitchEntity)`:
`is_on` from `get_desired("gain_auto")` (default the actual `gain == ""`),
`async_turn_on` → `set_sdr("gain_auto", True)`, `async_turn_off` →
`set_sdr("gain_auto", False)`.

**const.py PLATFORMS:**
```python
PLATFORMS: Final[list[Platform]] = [
    Platform.SENSOR, Platform.BINARY_SENSOR, Platform.EVENT,
    Platform.NUMBER, Platform.SELECT, Platform.SWITCH,
]
```

**Optimistic state.** After `set_sdr` persists + sends + reads back, the coordinator
emits `signal_hub_update`; the inherited `_handle_hub_update` calls
`async_write_ha_state`, so the entity reconciles to the read-back value. Until then the
displayed value is the just-set desired value (optimistic). Put a one-line note about
this in each module docstring.

Run `uv run ruff check custom_components/rtl_433` before finishing. A quick import
smoke (`python -c "import custom_components.rtl_433.number, ...select, ...switch"`) under
`uv run` confirms there are no import-time errors.
</details>
