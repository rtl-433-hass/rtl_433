---
id: 7
group: "validation"
dependencies: [5, 6]
status: "pending"
created: 2026-07-04
skills:
  - home-assistant
  - integration-testing
---
# Verify end-to-end behavior parity against a live/replayed stream

## Objective
Prove that, after the migration, the integration behaves identically to end users: devices are
discovered, events update entities, SDR settings round-trip via `/cmd`, and entities go
`unavailable` then recover on disconnect/reconnect. This is the primary success criterion of the
plan (behavior parity), verified against the real runtime path, not just unit tests.

## Skills Required
- **home-assistant**: running the integration / test harness, config-entry setup, entity state.
- **integration-testing**: exercising the WebSocket/`/cmd` path against a live or replayed rtl_433 server.

## Acceptance Criteria
- [ ] `import pyrtl_433` and the key public symbols import in the 3.14 env (dependency resolvable) — smoke re-check from Task 1.
- [ ] `rg -n "class CannotConnect|_build_ws_url|_build_cmd_url|_unwrap_result|def classify_replay|def normalize\\b" custom_components/rtl_433/` shows the duplicated definitions are gone (only imports/adapters remain); `rg -n "coordinator\\._http" custom_components/ tests/` returns nothing.
- [ ] With the integration loaded against a live or replayed rtl_433 WebSocket: a device is discovered, an event updates a sensor entity, an SDR setting (e.g. sample rate) round-trips via `/cmd`, and an entity transitions to `unavailable` then recovers on disconnect/reconnect — matching pre-migration behavior. Evidence captured (log lines or entity-state snapshots).
- [ ] Generated entity ids for a representative fixture device set are diffed before vs after and confirmed identical (`_safe_token`/normalizer parity).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Use the project's `uv`-based Python 3.14 invocation.
- Prefer the repo's existing replay/fixture harness for a deterministic run; fall back to a live
  rtl_433 server if available. If neither can drive a true end-to-end run in this environment,
  drive the coordinator through the client seam with recorded frames and document the limitation.
- This task validates; it should not need to change production code. If it uncovers a parity
  defect, follow the error-handling path (document in Noteworthy Events and request direction)
  rather than silently patching scope.

## Input Dependencies
- Task 5: green test suite + rescoped mutmut.
- Task 6: docs updated (so the verified system matches documentation).

## Output Artifacts
- A parity-verification record (evidence of discovery, event update, `/cmd` round-trip,
  availability recovery, and stable entity ids) satisfying the plan's Self Validation section.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

1. Run the smoke import and the `rg` grep checks first — fast gates confirming the deletions and
   the dependency resolution.
2. Drive the runtime path. Options, best first:
   - Use the repo's replay/fixture test harness to feed a recorded rtl_433 stream and observe
     entity states and dispatcher signals end-to-end.
   - If a live/replayed server is reachable, set up a config entry and watch discovery + an event
     updating a sensor.
   - Exercise an SDR `/cmd` round-trip (set sample rate; confirm the coordinator issues the
     client `_send_cmd` and reflects the new meta).
   - Simulate a disconnect and confirm entities go `unavailable`, then reconnect and recover.
3. Capture evidence (log excerpts, entity-state snapshots) for each behavior.
4. Diff entity ids for a representative fixture device set before vs after the migration to
   confirm `_safe_token`/normalizer parity.
5. If any behavior diverges from pre-migration, stop and report it as a Noteworthy Event; do not
   expand scope to fix without direction.
</details>
