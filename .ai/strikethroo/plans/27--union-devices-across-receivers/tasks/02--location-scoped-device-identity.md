---
id: 2
group: "level-1-grouping"
dependencies: [1]
status: "pending"
created: "2026-07-23"
skills:
  - python
  - home-assistant
---
# Level 1: location-scoped device identity (device grouping)

## Objective
Collapse both receivers' entities in a location onto a single device-registry device by keying the nested-device `DeviceInfo.identifiers` on the **location**, not the receiver. This is the location-scoped internal milestone (Clarification #6): one device card per physical sensor within a location, entity `unique_id`s still receiver-scoped (so each field appears once per receiver until Task 3 unions them). Two distinct locations hearing the same `model+id` must NOT merge.

## Skills Required
- `python`: edit `DeviceInfo` construction and identity helpers in `entity.py`.
- `home-assistant`: device-registry identifier-based merging across config entries/subentries, `via_device`.

## Acceptance Criteria
- [ ] Nested-device `DeviceInfo.identifiers` becomes `(DOMAIN, f"{location_entry_id}:{device_key}")`; `via_device` points to the **location** device.
- [ ] Two receiver subentries in the same location register the same identifier → HA shows one device-registry device grouping both receivers' entities.
- [ ] Two receivers in **different** locations hearing the same `model+id` produce two separate devices (no cross-location merge).
- [ ] Entity `unique_id`s remain receiver-scoped at this milestone (no entity union yet); no history loss.
- [ ] `uv run ruff check` clean; a new grouping test passes; existing tests updated for the location-scoped identity.

Use your internal Todo tool to track these.

## Technical Requirements
- File: `custom_components/rtl_433/entity.py` (`Rtl433Entity.__init__` `DeviceInfo` + `_attr_unique_id`, hub-platform setup identity construction).
- Depends on Task 1's location/receiver ids being available at entity-construction time.

## Input Dependencies
- Task 1: location/receiver topology and ids.

## Output Artifacts
- Location-scoped device identity (consumed by Task 3's aggregator and Task 5's migration re-home).

## Implementation Notes
- Do not yet drop the receiver prefix from entity `unique_id` — that is Task 3 (entity union). Level 1 is device-level only.
- A device has one `via_device`; per-receiver relationship is expressed via the diagnostics in Task 4, not `via_device`.
