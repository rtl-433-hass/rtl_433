---
id: 1
group: "union-devices-across-receivers"
dependencies: []
status: "pending"
created: 2026-09-12
skills:
  - python
  - home-assistant-integration
  - javascript
  - technical-writing
---
# Vocabulary rename (hub→receiver, receiver→radio) and identity parser hardening

## Objective
Establish one unambiguous vocabulary across the whole tree before any structural change lands, and harden the two identity parsers so the four-segment templates introduced later cannot be mis-decoded. No behaviour changes; every existing entity survives.

## Skills Required
python, home-assistant-integration, javascript, technical-writing

## Acceptance Criteria
- [ ] `receiver` means the rtl_433 server everywhere: `hub_entry_id`, `CONF_HUB_ENTRY_ID`, the five `SIGNAL_HUB_*` / `signal_hub_*` helpers, `Rtl433HubEntity` / `Rtl433HubControl`, `hub_settings.py`, `_migrate_hub_entry` are all renamed
- [ ] The pre-existing "receiver" sense (the SDR) reads `radio` in `translations/en.json` (tuning/frequency/sample-rate strings) and their doc counterparts
- [ ] `_device_field_from_unique_id` (`device_trigger.py`) branches on segment count and recognises the literal `receiver` marker; a 4-segment receiver-control or per-receiver signal unique_id is never decoded as a device field
- [ ] The orphan-device prefix scan (`__init__.py`) likewise classifies location, receiver and merged-device identifiers correctly
- [ ] `receiver` is enforced as a reserved token no `device_key` may equal, asserted against `pyrtl_433.naming.safe_token` output and covered by a test
- [ ] The four `:hub:` unique-id construction sites are left building their CURRENT v2 values in this task (the template change ships with the topology in task 002/003); only names, not values, change here
- [ ] `uv run pytest tests/` passes and `uvx ruff@0.16.6 check .` / `format --check .` are clean

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
Renames span `custom_components/rtl_433/**/*.py` (~839 occurrences), `tests/` (~4,935), `*.md` + `docs/` (~491), `frontend/rtl_433-panel.js` (~91) and `translations/` (18). Mechanical identifier renames and semantic string rewrites are SEPARATE passes: identifiers can be swept, but every "receiver"→"radio" string must be read to confirm it means the SDR. Parser sites: `device_trigger.py:178-194` (`split(":", 2)`, requires exactly 3 parts) and `__init__.py:501-504` (`ident.split(":", 1)[1]`).

## Input Dependencies
None — this is the first task.

## Output Artifacts
A tree that speaks one vocabulary, and two parsers that tolerate 3- and 4-segment identity strings.

## Implementation Notes
Keep all *device-field* `object_suffix` values byte-identical (COMPATIBILITY_CONTRACT §2, AGENTS.md guardrail). Sequence this first and commit it alone so every later structural diff is readable. See plan Component 1 and Clarifications #3, #10, #11, #18.
