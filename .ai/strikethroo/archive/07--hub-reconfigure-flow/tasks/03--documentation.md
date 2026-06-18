---
id: 3
group: "reconfigure-flow"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - technical-writing
---
# Document the reconfigure flow (README + AGENTS.md)

## Objective
Update human and agent docs to describe the new in-place reconfigure flow and the split between connection-target editing (Reconfigure) and behavioral settings (Configure/options).

## Skills Required
- `technical-writing` — concise Markdown edits to `README.md` and `AGENTS.md`.

## Acceptance Criteria
- [ ] `README.md` **Configuration** section (~`:83-101`) and **Editing options** section (~`:166-179`) note that a hub's connection target (host/port/path/secure) can be changed in place via **Settings → Devices & Services → rtl_433 → the hub → Reconfigure**, that nested devices and history are preserved, and clarify the split: connection target → Reconfigure; discovery/timeout/manage-settings → Configure (options).
- [ ] `AGENTS.md` (~`:43-46`, near the config-flow description) notes that `Rtl433ConfigFlow` now implements `async_step_reconfigure` for editing connection params in place (unique_id recomputed from `host:port`; nested-device map preserved via `data_updates=` merge).

## Technical Requirements
- Files: `README.md`, `AGENTS.md`. Match existing tone/heading style. Keep additions short.

## Input Dependencies
- Task 1: describe behavior as actually shipped.

## Output Artifacts
- Updated `README.md` and `AGENTS.md`.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

- Keep each addition to 1-3 sentences. Do not restructure existing sections.
- Verify line ranges before editing (they may have shifted); search for the "Configuration", "Editing options", and config-flow descriptions rather than trusting exact line numbers.
- Frame Reconfigure as "update this hub's connection settings (same server, new address)", consistent with the plan's "preserve, do nothing" stance on stale devices.
</details>
