---
id: 2
group: "device-triggers"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - python
  - pytest
---
# Tests for device triggers

## Objective
Add `tests/test_device_trigger.py` covering enumeration and end-to-end firing, including the same-value-repeat behavior the custom listener exists to provide.

## Skills Required
- `python`, `pytest` — `pytest-homeassistant-custom-component`, the existing hub/feed harness.

## Acceptance Criteria
- [ ] **Enumeration**: a hub seeded with an event device (`{device_key: {model, fields: ["button"], event_types: {"button": ["A","B"]}}}`); resolve the device via `dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{hub_entry_id}:{device_key}")})`; `async_get_triggers(hass, device_id)` returns the per-entity base trigger + the `A`/`B` subtypes.
- [ ] **Base fires on every transmission incl. same-value repeat**: feed button `A` then `A` → action fires twice.
- [ ] **Subtyped fires on every matching press incl. repeat**: `subtype: A`, feed `A`,`A` → two fires.
- [ ] **Subtyped does not fire for non-matching type**: `subtype: A`, feed `B` → zero fires.
- [ ] `PLATFORMS` unchanged; no conditions/actions defined (can assert by importing the module and checking absent attrs, or grep).
- [ ] `python -m pytest tests/test_device_trigger.py -q` and full `pytest -q` pass; `ruff` clean.

## Technical Requirements
- File: `tests/test_device_trigger.py` (new).
- `hub_entry_builder` is a shared fixture (`tests/conftest.py:102`), but `_setup_hub`/`_feed` are module-local in `tests/test_lifecycle.py` (~:83-110) — import them (`from tests.test_lifecycle import _setup_hub, _feed`) or promote to conftest; do NOT assume fixture injection.

## Input Dependencies
- Task 1.

## Output Artifacts
- `tests/test_device_trigger.py`.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Attach triggers via the automation engine (set up an `automation` with a `device` trigger) OR call `async_attach_trigger` directly with a captured-calls action; either is fine — pick what the harness supports cleanly. A simple `@callback` appending to a list as the `action` works with `async_attach_trigger`.
- Feed transmissions via `_feed` through the coordinator; `await hass.async_block_till_done()` before asserting.
- For the same-value-repeat cases, feed the identical event twice; assert two fires (this is the key regression guard — delegating the subtyped path to the core state trigger would fail it).
- Use the existing event fixture if one is suitable, or construct button frames inline with `event_type` `A`/`B`.

### Meaningful Test Strategy Guidelines
"write a few tests, mostly integration". Test enumeration + the three firing behaviors; don't test HA's device-automation framework itself.
</details>
