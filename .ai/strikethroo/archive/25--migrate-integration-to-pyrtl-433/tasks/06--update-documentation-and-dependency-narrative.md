---
id: 6
group: "docs"
dependencies: [4]
status: "completed"
created: 2026-07-04
skills:
  - technical-writing
---
# Update AGENTS.md and the dependency narrative

## Objective
Make the human- and AI-facing documentation describe the new `pyrtl_433` dependency and the
coordinator-as-adapter architecture, and correct any text that still claims the integration has
no third-party runtime dependency.

## Skills Required
- **technical-writing**: accurate, concise docs aligned to the shipped architecture.

## Acceptance Criteria
- [ ] Repo-root `AGENTS.md` documents: the `pyrtl_433==0.1.0` runtime dependency, the coordinator wrapping `pyrtl_433.Rtl433Client` (injected HA session + callbacks→dispatcher), the local `_safe_token` and SDR adapter seams, and the `_send_cmd` private-API wart.
- [ ] Any README/HACS/`requirements.txt` narrative asserting "no third-party runtime dependency" is corrected (note: `requirements.txt` comment is handled in Task 1 — verify consistency, don't duplicate).
- [ ] Docs no longer describe the integration as carrying its own copy of the transport/normalizer/replay/SDR logic.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- The only agent-facing doc is the repo-root `AGENTS.md` (~48 KB); there is no `CLAUDE.md` and
  no `AGENTS.md` inside `custom_components/rtl_433/`.
- Cross-check with the final state produced by Task 4 so the described architecture matches the
  code.

## Input Dependencies
- Task 4: final coordinator/adapter architecture (so the docs describe what actually shipped).

## Output Artifacts
- Updated `AGENTS.md` and corrected dependency narrative.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

1. Read the current `AGENTS.md` sections that describe the transport/coordinator, the normalizer,
   the SDR settings, and any "zero dependency" claim. Update them to reflect: the integration now
   depends on `pyrtl_433==0.1.0`; the coordinator is a thin HA adapter that owns a
   `Rtl433Client` (session injected via `async_get_clientsession`, events delivered by
   `on_event`/`on_hub_update` callbacks that feed the HA dispatcher); the pure helpers come from
   `pyrtl_433.normalizer`/`replay`/`sdr`; `_safe_token` and the SDR name/shape adapter are local
   shims; the `/cmd` setter is the library's underscore `_send_cmd`.
2. Note the retired mutmut ratchet for the extracted transport (now covered in `pyrtl_433`) if
   `AGENTS.md` documents the mutation-testing setup.
3. Grep the repo for "no third-party" / "dependency-free" / "self-contained" narrative in README,
   `hacs.json` context, or docs and correct it. Keep `requirements.txt` consistent with Task 1
   (don't re-edit if already done — just verify).
4. Keep edits factual and scoped; do not invent features. This is documentation only.
</details>
