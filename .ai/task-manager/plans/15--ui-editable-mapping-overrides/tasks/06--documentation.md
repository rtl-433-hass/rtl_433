---
id: 6
group: "verification"
dependencies: [2, 3, 4]
status: "pending"
created: 2026-05-28
skills:
  - technical-writing
---
# Documentation updates (device-library, README, AGENTS)

## Objective
Update the human- and assistant-facing docs to describe the in-UI, per-hub mapping editor and the one-time file import, replacing the "drop a file" instructions.

## Skills Required
- `technical-writing`: clear, accurate user/maintainer docs.

## Acceptance Criteria
- [ ] `docs/device-library.md` "User overrides" section rewritten: editing happens in hub options → "Device mappings" using HA's YAML editor; stored per hub; validated before save; applied via automatic reload (no restart). Documents the one-time import on upgrade, that the file is otherwise ignored and not deleted, and the loss of comments once edited in the UI. Remove the "Changes are picked up on the next reload of the integration (or HA restart). A full graphical mapping editor is intentionally out of scope" paragraph.
- [ ] `README.md` feature bullet (~lines 37–38) and "Device library and user overrides" section (~296–318) updated to describe the in-UI editor + per-hub storage instead of dropping a file.
- [ ] `AGENTS.md` updated where it describes the override loading path / `DATA_LIBRARY` semantics to reflect shipped-global-cache + per-entry merge + the new options step + the migration.
- [ ] Markdown lints clean (repo uses markdownlint/prettier per `.pre-commit-config.yaml`).

## Technical Requirements
- Keep the override YAML schema/examples accurate — the schema is unchanged; only the editing surface and storage changed.

## Input Dependencies
- Tasks 2, 3, 4 (final behaviour to document).

## Output Artifacts
- Updated docs; part of the PR.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

Read the current `docs/device-library.md` "User overrides" section (~line 395+), `README.md` (~37–38 and ~296–318), and grep `AGENTS.md` for `mappings.yaml`, `DATA_LIBRARY`, `load_user_overrides`, "user override".

- device-library.md: keep the example override YAML (still valid), but frame it as "paste this into the Device mappings editor". State the migration behaviour and that the file is import-only/never deleted. Note model-scoped + skip_keys still work in the editor.
- README.md: the feature bullet should now read like "Per-installation user overrides — add or correct mappings from the hub's options using Home Assistant's built-in YAML editor; no file editing or restart required." Update the longer section similarly; mention validation-before-save and per-hub scope.
- AGENTS.md: update the data-flow description: `DATA_LIBRARY` now caches the shipped library only; per-hub overrides live in `entry.data[CONF_USER_MAPPINGS]` and are merged per entry into `DATA_ENTRY_LIBRARY`; `load_user_overrides` removed; one-time `async_migrate_entry` import; new `async_step_mappings` options step.
- Run the repo's markdown lint (`pre-commit run --files <changed md>` or `prettier`/`markdownlint` as configured) and fix issues.
</details>
