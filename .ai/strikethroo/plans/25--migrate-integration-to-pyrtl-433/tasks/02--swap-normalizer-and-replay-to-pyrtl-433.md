---
id: 2
group: "helpers"
dependencies: [1]
status: "pending"
created: 2026-07-04
skills:
  - python
  - home-assistant
---
# Swap normalizer and replay classifier to pyrtl_433; re-home _safe_token

## Objective
Delete the integration's duplicated event normalizer and replay classifier and source them
from `pyrtl_433.normalizer` and `pyrtl_433.replay`, keeping every consumer's behavior
identical. Re-home the `_safe_token` entity-slug helper locally, since the library does not
export it.

## Skills Required
- **python**: module refactor, dataclass field parity, import rewiring.
- **home-assistant**: the integration's entity/coordinator import graph.

## Acceptance Criteria
- [ ] `custom_components/rtl_433/normalizer.py` no longer defines `normalize`, `device_key`, `NormalizedEvent`, or `DEFAULT_SKIP_KEYS`; these are imported from `pyrtl_433.normalizer` at every prior consumer site.
- [ ] `_safe_token` is re-homed into a small local module and `entity.py` imports it from there; entity-id slugging behavior is unchanged.
- [ ] The replay classifier in `coordinator/_events.py` (`classify_replay`, `ReplayVerdict`, `_parse_event_time`) is replaced by `pyrtl_433.replay` equivalents (`classify_replay`, `ReplayVerdict`, module-level `parse_event_time`); `_EventProcessingMixin` behavior is preserved.
- [ ] `NormalizedEvent` field parity is confirmed (`device_key`, `model`, `identity`, `fields`, `is_replay`, `event_time`, `is_repaint`); no consumer breaks.
- [ ] No remaining local duplicate of the normalizer/replay logic; imports resolve.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- `pyrtl_433.normalizer` exports `device_key`, `normalize`, `NormalizedEvent`,
  `IDENTITY_KEYS`, `DEFAULT_SKIP_KEYS`. It does **not** export `_safe_token`.
- `pyrtl_433.replay` exports `classify_replay`, `ReplayVerdict`, `parse_event_time`,
  `REPLAY_STALE_THRESHOLD`, `DISCOVERY_BACKLOG_GRACE`. Note `parse_event_time` is a
  module-level function, whereas the integration has it as `_EventProcessingMixin._parse_event_time`.
- Consumers to rewire (from the migration map): `coordinator/base.py` (imports
  `DEFAULT_SKIP_KEYS`, `NormalizedEvent` from `..normalizer`), `coordinator/_events.py`
  (imports `NormalizedEvent`, `normalize`), `entity.py:68` (`_safe_token`), plus
  TYPE_CHECKING imports in `event.py`, `sensor.py`, `binary_sensor.py`, `entity.py`.

## Input Dependencies
- Task 1: `pyrtl_433==0.1.0` installed and importable.

## Output Artifacts
- Normalizer/replay logic sourced from `pyrtl_433`; a local `_safe_token` helper.
- A stable `NormalizedEvent`/classifier surface for Task 4 (transport) to build on.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

1. **Normalizer**: Compare the integration `normalizer.py` against `pyrtl_433.normalizer`
   field-by-field. They share the same `NormalizedEvent` shape and `normalize`/`device_key`
   semantics. Replace usages: at each site importing from `custom_components.rtl_433.normalizer`,
   import from `pyrtl_433.normalizer` instead (`from pyrtl_433.normalizer import normalize,
   device_key, NormalizedEvent, DEFAULT_SKIP_KEYS`).
2. **`_safe_token`**: This private helper is used by `entity.py:68` for entity-id slugging and
   is not in the library's public API. Move the existing implementation verbatim into a small
   local module (e.g. keep a trimmed `normalizer.py` that contains only `_safe_token`, or a new
   `_slug.py`). Point `entity.py` at it. Do not reimplement — copy the current logic so entity
   ids are byte-identical.
3. **Replay classifier**: In `coordinator/_events.py`, delete the local `ReplayVerdict`,
   `classify_replay`, and the `_parse_event_time` staticmethod body. Import
   `classify_replay`, `ReplayVerdict` from `pyrtl_433.replay`; replace `self._parse_event_time(raw)`
   calls with the module-level `parse_event_time(raw)` (import it:
   `from pyrtl_433.replay import parse_event_time`). Keep `_process_event`, `_trace_unmapped_fields`,
   `_maybe_register_device` — the HA-specific mixin behavior — in place, now calling the library
   functions.
4. Delete any now-empty duplicate definitions. Ensure no import still points at a removed local
   symbol.
5. Verify by importing the integration package and running a quick normalize/classify smoke
   check on a sample event dict; full test rewiring happens in Task 5.

Keep this task disjoint from the SDR work (Task 3) — different modules, so they can proceed in
parallel.
</details>
