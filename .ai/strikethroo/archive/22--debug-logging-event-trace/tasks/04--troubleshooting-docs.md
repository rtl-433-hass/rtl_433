---
id: 4
group: "event-trace-instrumentation"
dependencies: [1, 2]
status: "completed"
created: 2026-06-15
skills:
  - technical-writing
---
# Add a concise troubleshooting / debug-logging note to the README

## Objective
Document how to enable DEBUG logging for the integration and how to read the new event trace, mapping the verdict and firing lines onto the three failure hypotheses (rtl_433 vs the integration's startup/replay handling vs the automation). This directly serves the work order's stated goal: letting a user or developer determine where a duplicate/spurious event originates.

## Skills Required
- `technical-writing` — clear, concise user-facing documentation matching the README's existing voice.

## Acceptance Criteria
- [ ] A short new section is added to `README.md` (e.g. "## Troubleshooting" / "Debug logging", placed near "Availability" / "Per-device signal diagnostics").
- [ ] It shows the Home Assistant `logger:` configuration snippet to set `custom_components.rtl_433` to `debug`.
- [ ] It explains the ingestion line and the four verdicts (`LIVE`, `REPLAY`, `STALE-GAP`, `BACKLOG`) and the `fired ... event_type=` / watchdog-dedupe lines in one short table or list.
- [ ] It maps the trace onto the three hypotheses: two `LIVE` for one press ⇒ rtl_433 (bad decode / duplicate); `REPLAY`/`BACKLOG` on startup ⇒ integration correctly suppressed a queued duplicate; single `LIVE` + single fire but multiple automation triggers ⇒ automation.
- [ ] No new configuration options are invented; the note is concise (a short section, not a tutorial).

## Technical Requirements
- File: `README.md`. Existing top-level sections include Overview, Features, Availability, Per-device signal diagnostics, Hub entities — insert the troubleshooting note in a logical place among these.
- Use the **final** log-line wording produced by Tasks 1 and 2 (read the implemented `LOGGER.debug` strings in `coordinator/base.py` and `event.py` before writing, so the documented examples match exactly).

## Input Dependencies
- Task 1 and Task 2: the final, exact text of the DEBUG log lines.

## Output Artifacts
- A README troubleshooting section users can follow to diagnose duplicate/spurious events.

## Implementation Notes
<details>

- Read the actual emitted strings from the implemented code first; quote real examples, e.g.:
  ```
  rtl_433 RX Acme-Doorbell/42 fields={'button': 1} time=2026-06-15T20:00:01 -> LIVE (event_time>high_water)
  rtl_433 fired ring for Acme-Doorbell/42 field=button value=1 -> event_type=ring
  ```
- HA logger snippet to include:
  ```yaml
  logger:
    logs:
      custom_components.rtl_433: debug
  ```
- Keep it tight: enable → what each line means → how to attribute a duplicate. Avoid duplicating the architectural detail from the plan; this is user-facing.
- Match the README's existing heading depth and tone.
</details>
