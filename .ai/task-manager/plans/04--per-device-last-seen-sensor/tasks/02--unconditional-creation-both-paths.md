---
id: 2
group: "entities"
dependencies: [1]
status: "pending"
created: "2026-05-26"
skills:
  - python
  - home-assistant
---
# Unconditional per-device creation in both setup paths

## Objective
Make exactly one `Rtl433LastSeenSensor` appear for every nested device — present
or future — without depending on mapped fields, by adding an optional
per-device "extra entity" hook to the shared
`async_setup_hub_platform` (`entity.py`) and passing the Last-seen sensor from
`sensor.py`'s `async_setup_entry`. The `binary_sensor` platform passes nothing
and creates no Last-seen sensor. The existing field-driven measurement-sensor
path must be left functionally unchanged.

## Skills Required
- `python` — extending a shared helper with an optional callable parameter, dedup-set bookkeeping.
- `home-assistant` — entity-platform setup, `async_add_entities`, dispatcher-driven dynamic add, per-device teardown.

## Acceptance Criteria
- [ ] `async_setup_hub_platform` gains an optional parameter (e.g. `per_device_factory: Callable[[Rtl433Coordinator, str, str, str], Rtl433Entity] | None = None`) that, when set, builds one extra per-device entity from `(coordinator, entry_id, device_key, model)`.
- [ ] The extra entity is created **once per device**, deduped by a dedicated `device_key` set (e.g. `extra_created`), in **both** creation paths: the initial devices-map iteration **and** the `_handle_new_device` new-device handler.
- [ ] The extra entity is **not** created in the late-field listener (`_handle_new_fields`) — that path is only for newly mapped fields on an already-created device.
- [ ] `_remove_device` clears the device's entry from the `extra_created` set so the extra entity is recreated cleanly if the device returns (matching the existing `created`/`field_unsubs` teardown).
- [ ] `sensor.py`'s `async_setup_entry` passes `per_device_factory=Rtl433LastSeenSensor` to `async_setup_hub_platform`; `binary_sensor.py` passes nothing (or `None`).
- [ ] No circular import is introduced (`entity.py` must not import from `sensor.py`); the factory is supplied by the caller.
- [ ] The field-driven measurement-sensor build/dedup/persist behavior is unchanged; the existing lifecycle test suite still passes.
- [ ] `uv run ruff check custom_components/rtl_433` is clean.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `custom_components/rtl_433/entity.py` (the helper) and
  `custom_components/rtl_433/sensor.py` (pass the factory). `binary_sensor.py`
  may be left as-is (it already calls the helper without the new arg).
- Reuse the existing `created` dedup cache, `field_unsubs`, `_remove_device`, and
  `coordinator.device_removers` machinery rather than inventing parallel
  structures.

## Input Dependencies
- Task 1: `Rtl433LastSeenSensor` (imported by `sensor.py`) with the
  `(coordinator, hub_entry_id, device_key, model)` constructor signature, which
  is exactly the factory shape this hook calls.

## Output Artifacts
- One Last-seen sensor per device in both setup paths, exercised by Task 3 tests
  and described by Task 4 docs.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### Why a caller-supplied factory (not an import in entity.py)
`sensor.py` imports from `entity.py`, so `entity.py` importing `Rtl433LastSeenSensor`
back would be a circular import. Instead, `async_setup_hub_platform` accepts an
optional factory and the *sensor platform* supplies it. This keeps the helper
generic (binary_sensor passes nothing) and dependency-direction clean.

### `entity.py` — signature + dedup set
```python
async def async_setup_hub_platform(
    hass,
    entry,
    async_add_entities,
    platform,
    entity_cls,
    per_device_factory=None,  # NEW: (coordinator, entry_id, device_key, model) -> Rtl433Entity
) -> None:
    ...
    created: dict[str, set[str]] = {}
    field_unsubs: dict[str, Callable[[], None]] = {}
    extra_created: set[str] = set()  # NEW: device_keys with their extra entity made
```

Add a small builder near `_build`:
```python
    def _build_extra(device_key: str, model: str) -> list[Rtl433Entity]:
        """Build the optional once-per-device extra entity (e.g. Last-seen)."""
        if per_device_factory is None or device_key in extra_created:
            return []
        extra_created.add(device_key)
        return [per_device_factory(coordinator, entry.entry_id, device_key, model)]
```

### Wire it into both creation paths
Initial devices-map loop (currently `async_add_entities(_build(device_key, model, union))`):
```python
    for device_key, rec in entry.data.get(CONF_DEVICES, {}).items():
        model = rec.get(CONF_MODEL, "")
        union = set(rec.get(DEVICE_FIELDS, [])) | coordinator.device_fields.get(
            device_key, set()
        )
        async_add_entities(_build(device_key, model, union) + _build_extra(device_key, model))
        await async_upsert_device(hass, entry, device_key, model=model, fields=union)
        _register_field_listener(device_key, model)
```

New-device handler (`_handle_new_device`):
```python
    @callback
    def _handle_new_device(device_key: str, model: str) -> None:
        fields = coordinator.device_fields.get(device_key, set())
        new_entities = _build(device_key, model, fields) + _build_extra(device_key, model)
        if new_entities:
            async_add_entities(new_entities)
        _register_field_listener(device_key, model)
        hass.async_create_task(
            async_upsert_device(hass, entry, device_key, model=model, fields=fields)
        )
```
(`_build` may return `[]` for a device with no mapped sensor fields; `_build_extra`
still adds the Last-seen sensor, so `new_entities` is non-empty and the device is
created — this is the "device with no mapped fields still gets Last-seen" case.)

### Teardown
In `_remove_device`, alongside the existing `created.pop` / `field_unsubs.pop`:
```python
    @callback
    def _remove_device(device_key: str) -> None:
        created.pop(device_key, None)
        extra_created.discard(device_key)  # NEW
        unsub = field_unsubs.pop(device_key, None)
        if unsub is not None:
            unsub()
```

### `sensor.py` — pass the factory
```python
async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        Rtl433HubSensor(coordinator, entry.entry_id, desc) for desc in HUB_SENSORS
    )
    await async_setup_hub_platform(
        hass, entry, async_add_entities, PLATFORM, Rtl433Sensor,
        per_device_factory=Rtl433LastSeenSensor,
    )
```
`binary_sensor.py` is unchanged: it calls `async_setup_hub_platform(...)` without
`per_device_factory`, so no Last-seen sensor is created there.

### Gotchas
- Do **not** add the extra entity in `_handle_new_fields`; that would risk a
  second creation attempt (the `extra_created` guard prevents duplicates, but the
  late-field path is conceptually only for new mapped fields).
- Keep the `per_device_factory` parameter optional with a `None` default so
  `binary_sensor.py` and any other caller keep working untouched.
- Preserve the order/identity of the existing measurement-sensor build; only
  *append* the extra entity to the list passed to `async_add_entities`.
</details>
