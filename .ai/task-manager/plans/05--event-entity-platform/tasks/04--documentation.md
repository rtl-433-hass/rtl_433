---
id: 4
group: "documentation"
dependencies: [1, 2]
status: "pending"
created: "2026-05-26"
skills:
  - technical-writing
---
# Document the event platform and schema

## Objective
Document the new `event` platform across the three authoritative docs so the
data-driven model and its non-obvious invariants survive future refactors:

- **`docs/device-library.md`** — the authoritative schema reference: the `event`
  platform, auto-populated value-as-type model (single-value = momentary), the
  type-only / no-extra-attributes fired event, `device_class` = `EventDeviceClass`,
  the new `events.yaml` themed file, and the three shipped examples.
- **`README.md`** — add `event` to the entity types the integration produces with
  a one-line description of when momentary devices become event entities.
- **`AGENTS.md`** — record the event platform's value-as-type/auto-populate model,
  the `DEVICE_EVENT_TYPES` devices-map persistence, the always-available override,
  and the watchdog-re-dispatch identity-dedupe, so future refactors of
  `async_setup_hub_platform`, the watchdog, and the devices map preserve them.
- **`WEBSOCKET_API.md`** — no change (no new API usage). Confirm and leave as-is.

## Skills Required
- `technical-writing` — clear, accurate reference/user docs matching the existing
  voice and structure of these files.

## Acceptance Criteria
- [ ] `docs/device-library.md` documents the `event` platform: that `platform: event` is a third valid platform value; that `event_types` are **auto-populated** from observed values (not declared); that the fired `event_type` is the **stringified field value** and a field that emits one distinct value is a **momentary** single-type trigger; that the fired event carries **no extra attributes**; that `device_class` is an `EventDeviceClass` (`button`/`motion`/`doorbell`); the new `events.yaml` themed file; and the three shipped examples. The schema/attribute tables are updated so `platform` lists `event` and `device_class` notes the `EventDeviceClass` interpretation for event entries.
- [ ] `README.md` lists `event` among the produced entity types with a one-line "when momentary/fire-and-forget devices (remotes, doorbells, motion, key fobs) become event entities" description, consistent with the existing sensor/binary_sensor/Last-seen prose.
- [ ] `AGENTS.md` gains a short section (near the "Per-device Last seen sensor" / device-library sections) capturing: value-as-type + auto-populate, `DEVICE_EVENT_TYPES` persistence via `async_upsert_event_types`, always-available override, the **identity-based** watchdog-re-dispatch dedupe (and why it must not become value-equality), and that the entity does not replay on `__init__` (HA restores the last event).
- [ ] `WEBSOCKET_API.md` is reviewed and intentionally left unchanged.
- [ ] Field names, `device_class` values, and the `events.yaml` filename in the docs match what Task 2 actually shipped (verify against the file, do not assume).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Files: `docs/device-library.md`, `README.md`, `AGENTS.md` (and a no-op review of
  `WEBSOCKET_API.md`).
- Match existing structure: `docs/device-library.md` has `## Mapping entry
  schema` → `### Attributes`, `### Value transforms`, `### Binary payloads`
  sections — add an event subsection alongside these. `AGENTS.md` has a `##
  Per-device "Last seen" sensor` section and a `## Device-library YAML format
  (summary)` section to mirror.

## Input Dependencies
- Task 1 — the implemented behavior being documented (persistence key name,
  helper name, dedupe mechanism, always-available).
- Task 2 — the exact three field keys, `device_class` values, and `events.yaml`
  filename to reference.

## Output Artifacts
- Updated `docs/device-library.md`, `README.md`, `AGENTS.md`.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

### docs/device-library.md
In `### Attributes`, note that `platform` accepts `sensor | binary_sensor |
event`, and that for `event` entries `device_class` is interpreted as an
`EventDeviceClass` (`button`, `motion`, `doorbell`). Add a focused subsection
(e.g. `### Event entities`) explaining:
- one HA Event fires per genuine transmission; the `event_type` is `str(value)`;
- `event_types` are auto-populated from observed values and persisted per device
  so the entity rebuilds with its known types after a restart (no declaration);
- a single-distinct-value field is a momentary trigger that fires that one type
  every time (doorbell/motion);
- the fired event carries no extra attributes (type only);
- no `payload`/`value_transform` applies to event entries (the value is
  stringified directly);
- point to `device_library/events.yaml` and list the three shipped examples.

### README.md
The entity-types prose currently mentions "sensors and binary sensors" (line ~12)
and a "Last seen" bullet (lines ~138). Add `event` to the produced types and a
short bullet: momentary/fire-and-forget RF (remote buttons, doorbells, motion,
key fobs) becomes a native HA **event** entity (no faked "off"); the event type
is the transmitted value and event entities stay available between presses.

### AGENTS.md
Add a sibling section to "Per-device Last seen sensor". Key invariants to record
verbatim so refactors preserve them:
- `Rtl433Event` (`event.py`) is field-driven via `async_setup_hub_platform`
  (`Platform.EVENT` in `PLATFORMS`), no `per_device_factory`.
- It overrides `_handle_dispatch` to dedupe the watchdog re-dispatch by **object
  identity** (`event is self._last_fired_event`) — NOT value-equality, because a
  genuine repeat of the same value is a distinct frozen `NormalizedEvent` that
  must fire; only the watchdog re-sends the same cached object.
- `event_types` auto-populate from `str(value)`, persisted per device-field under
  `entry.data[CONF_DEVICES][key][DEVICE_EVENT_TYPES][field]` via
  `async_upsert_event_types` (idempotent union write); the entity reads them in
  `__init__` from `coordinator.entry.data` (the shared 5-arg constructor is
  unchanged).
- `available` is always `True`; `_async_restore_state` is a no-op (HA's
  `EventEntity.async_internal_added_to_hass` restores the last event); the entity
  does **not** replay `coordinator.devices[key]` on construction.

### Voice
Keep the terse, declarative style of these docs. Don't restate the whole plan;
capture the invariants and the user-facing behavior.
</details>
