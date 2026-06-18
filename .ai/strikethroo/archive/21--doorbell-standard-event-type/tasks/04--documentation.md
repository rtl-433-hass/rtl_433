---
id: 4
group: "documentation"
dependencies: [1]
status: "completed"
created: 2026-06-04
skills:
  - technical-writing
---
# Document the `event_map` attribute and the doorbell `ring` mapping

## Objective
Update the library reference and assistant docs so they accurately describe the new optional `event_map` descriptor attribute and the doorbell `ring`/`secret_knock` mapping.

## Skills Required
- `technical-writing` (Markdown docs)

## Acceptance Criteria
- [ ] `docs/device-library.md` event-entities section documents the optional `event_map` attribute (raw-value-string → event-type) and explains the doorbell mapping (`0 → ring`, `1 → secret_knock`), noting that `ring` is the HA-standard `DoorbellEventType.RING` and is required for `device_class: doorbell` entities.
- [ ] The existing statements that the fired `event_type` is always the stringified value and that `event_types` are never declared are reframed as the **default** (no-`event_map`) case.
- [ ] The `secret_knock` row/example in `docs/device-library.md` reflects the corrected semantics (emitted on every press; `0` regular, `1` secret knock).
- [ ] If `AGENTS.md` documents the event platform / device library inventory, add a one-line note that doorbell fields map `secret_knock` `0→ring`, `1→secret_knock` and that doorbell entities must advertise `ring`. Only update an existing relevant section; do not invent a new top-level section.

## Technical Requirements
- Edit `docs/device-library.md` (event-entities section, ~lines 172-215).
- Optionally edit `AGENTS.md` if it has a relevant event/library section.

## Input Dependencies
- Task 1 (the `event_map` schema and `events.yaml` change define what to document).

## Output Artifacts
- Accurate documentation for the new attribute and behavior.

## Implementation Notes

<details>
<summary>Step-by-step implementation</summary>

- In `docs/device-library.md`, the event-entities section currently states: "The fired `event_type` is the stringified field value (`str(value)`)" and "`event_types` are auto-populated, not declared." Reframe these as the default behavior, then add a short subsection or bullet:
  - Introduce `event_map`: an optional mapping of stringified raw value → event type. When present, the entity fires the mapped type (unmapped values pass through as `str(value)`), and the mapped types are declared up front in `event_types`.
  - Doorbell example: `secret_knock` uses `event_map: {"0": ring, "1": secret_knock}`. `ring` is Home Assistant's standard `DoorbellEventType.RING`; a `device_class: doorbell` entity must advertise `ring` or HA will deprecate it (removed in HA 2027.4).
- Fix the `secret_knock` table row (~line 209) so it no longer says "a single momentary value" — it is emitted on every press (`0` regular, `1` secret knock).
- Search `AGENTS.md` for an event-platform/device-library inventory section; if one exists, add the one-line doorbell-mapping note there. If nothing relevant exists, skip the `AGENTS.md` edit (do not create a new section just for this).
- Keep the prose concise and consistent with the surrounding doc style; respect `.markdownlint.json` / `.prettierrc.yml`.
</details>
