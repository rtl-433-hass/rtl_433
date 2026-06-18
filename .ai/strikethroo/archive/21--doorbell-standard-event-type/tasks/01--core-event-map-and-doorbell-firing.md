---
id: 1
group: "implementation"
dependencies: []
status: "completed"
created: 2026-06-04
skills:
  - python
---
# Add declarative `event_map` and make the doorbell entity fire `ring` / `secret_knock`

## Objective
Introduce an optional `event_map` attribute on event descriptors and teach `Rtl433Event` to (a) seed `event_types` so a doorbell entity always advertises `DoorbellEventType.RING` (`"ring"`), and (b) fire the mapped event type per transmission. Ship the map on the `secret_knock` descriptor so a regular press (`0`) fires `ring` and a secret knock (`1`) fires `secret_knock`.

## Skills Required
- `python` (Home Assistant custom integration internals)

## Acceptance Criteria
- [ ] `FieldDescriptor` (in `custom_components/rtl_433/mapping.py`) has a new optional `event_map: dict[str, str] | None = None` attribute that the YAML loader accepts.
- [ ] `event_map` values are normalized defensively (keys and values coerced to `str`; a non-mapping `event_map` is ignored), mirroring the existing `payload` normalization in `_descriptor_from_entry`.
- [ ] `device_library/events.yaml` `secret_knock` descriptor carries `event_map: {"0": ring, "1": secret_knock}`, and its misleading comment is corrected to state the field is emitted on every press (`0` = regular press, `1` = secret knock / triple press).
- [ ] `Rtl433Event.__init__` seeds `_attr_event_types` from the union of `event_map` values (declared first, stable order) and the persisted list, and guarantees `"ring"` is present whenever `device_class == EventDeviceClass.DOORBELL`.
- [ ] `Rtl433Event._handle_dispatch` resolves the event type via `event_map.get(str(value), str(value))` when a map exists, and keeps `str(value)` when no map exists (button path unchanged).
- [ ] Declared `event_map` types are persisted when the entity is added (so device triggers list them across restarts even before the first press).
- [ ] `uv run python` can `load_library()` and the resulting `secret_knock` descriptor exposes `device_class == "doorbell"` and `event_map == {"0": "ring", "1": "secret_knock"}`.

## Technical Requirements
- Edit `custom_components/rtl_433/mapping.py`, `custom_components/rtl_433/event.py`, `custom_components/rtl_433/device_library/events.yaml`.
- HA constants: `from homeassistant.components.event import DoorbellEventType, EventDeviceClass` (`DoorbellEventType.RING.value == "ring"`, `EventDeviceClass.DOORBELL.value == "doorbell"`).

## Input Dependencies
None.

## Output Artifacts
- The `event_map` descriptor attribute and the doorbell firing behavior consumed by the tests (task 3) and described in the docs (task 4).

## Implementation Notes

<details>
<summary>Step-by-step implementation</summary>

**1. `mapping.py` — add the descriptor attribute.**
- In the `FieldDescriptor` dataclass (around line 63-86), add a new field after the other optional attributes:
  `event_map: dict[str, str] | None = None`
- `_DESCRIPTOR_ATTRS` is computed from the dataclass fields, so the loader auto-accepts `event_map` from YAML — no change needed there.
- In `_descriptor_from_entry` (around line 119-150), after the existing `payload` normalization block, add a normalization for `event_map`:
  - If `"event_map" in known`: if it is not a `dict`, log a debug message and `known.pop("event_map")`; otherwise rebuild it as `{str(k): str(v) for k, v in known["event_map"].items()}`.
- (Optional, low priority) `_validate_entry` already tolerates unknown/extra attributes, so no validator change is required.

**2. `device_library/events.yaml` — update the `secret_knock` descriptor.**
- Replace the misleading comment block above `secret_knock`. New comment must convey: the Honeywell ActivLink doorbell decoder emits `secret_knock` on every press; value `0` is a normal single press, value `1` is a "secret knock" (3 rapid presses). It maps to the HA doorbell standard: `0 -> ring`, `1 -> secret_knock`.
- Add to the descriptor:
  ```yaml
  event_map:
    "0": ring
    "1": secret_knock
  ```
  Keep `device_class: doorbell`, `name: Doorbell`, `object_suffix: secret_knock`, `platform: event`. Quote the numeric keys so YAML parses them as strings.

**3. `event.py` — `Rtl433Event.__init__` (around line 50-76).**
- Add the import: `from homeassistant.components.event import DoorbellEventType, EventDeviceClass` (top of file, alongside `EventEntity`).
- After reading `persisted` (the persisted list), build the seed list as declared-first union:
  - `declared = list((self._descriptor.event_map or {}).values())` — preserve insertion order, de-duplicate while keeping order.
  - `seed = declared + [t for t in persisted if t not in declared]`.
  - If `self._attr_device_class == EventDeviceClass.DOORBELL` (compare against `EventDeviceClass.DOORBELL` / its `.value` `"doorbell"`; `device_class` is the plain string) and `DoorbellEventType.RING` not in `seed`, insert `DoorbellEventType.RING` (the string `"ring"`) at the front.
  - `self._attr_event_types = seed`.
- Note `self._descriptor` is the descriptor (see base `Rtl433Entity`); confirm the attribute name used by the base class and reuse it (the existing code references `self._descriptor.field_key`).

**4. `event.py` — `Rtl433Event._handle_dispatch` (around line 123-140).**
- Where it currently computes `event_type = str(event.fields[field_key])`, change to:
  - `raw = event.fields[field_key]`
  - `event_map = self._descriptor.event_map`
  - `event_type = event_map.get(str(raw), str(raw)) if event_map else str(raw)`
- Keep the existing "append-if-new + schedule persist + `_trigger_event(event_type)`" logic unchanged. Unmapped values still append/persist as before.

**5. `event.py` — persist declared types on add.**
- Override `async_added_to_hass` (call `await super().async_added_to_hass()` first). For each declared `event_map` value not yet persisted, schedule `async_upsert_event_types(self.hass, self._coordinator.entry, self._device_key, field_key, [...declared...])`. `async_upsert_event_types` is already imported and is idempotent (no write if the set doesn't grow), so passing all declared types at once is safe. This ensures `device_trigger._event_types_for_entry`'s persisted-preferred lookup returns `ring`/`secret_knock` across restarts.
- Be careful: the base `Rtl433Entity` may already define `async_added_to_hass`; if so, extend it rather than shadowing — call the base implementation and add the persistence step.

**6. Verify.** Run a quick `uv run python -c "import asyncio; from custom_components.rtl_433.mapping import load_library; reg, skip = load_library(); d = reg.flat['secret_knock']; print(d.device_class, d.event_map)"` (adjust to the actual `load_library` signature/return) and confirm `doorbell {'0': 'ring', '1': 'secret_knock'}`.

Do NOT change `apply_transform` — event types are resolved in the entity, not the transform pipeline. Do NOT add translations.
</details>
