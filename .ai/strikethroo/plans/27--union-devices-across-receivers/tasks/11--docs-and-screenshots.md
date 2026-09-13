---
id: 11
group: "union-devices-across-receivers"
dependencies: [8]
status: "completed"
created: 2026-09-12
skills:
  - technical-writing
---
# Documentation, AGENTS.md guardrails, and screenshots

## Objective
Align every human- and AI-facing surface with the location/receiver model.

## Skills Required
technical-writing

## Acceptance Criteria
- [ ] `README.md` describes the location/receiver model, adding receivers to one location, the union behaviour, the union add-device page, per-receiver signal detail, and that multiple locations are for genuinely distant sites — led by the glossary line *a receiver is a computer running rtl_433; it contains a radio*
- [ ] `AGENTS.md` documents the topology, the aggregation layer, the union/dedup/availability invariants, location-scoped adoption, the receiver-vs-radio vocabulary, the extension of the `device_replace.py` sanctioned-re-key rule, the subentry-ownership rule, and the reserved-`receiver`-token rule
- [ ] `custom_components/rtl_433/device_replace.py`'s module docstring is rewritten — it currently hardcodes the v2 templates and claims nothing in COMPATIBILITY_CONTRACT.md moves, both of which this plan falsifies
- [ ] `docs/availability.md`, `docs/device-discovery.md`, `docs/hub-entities.md` (renamed), `docs/configuration.md`, `docs/index.md` updated
- [ ] `translations/en.json` carries the subentry-flow strings and the duplicate-history Repairs text
- [ ] Screenshots in `docs/images/` are recaptured to show a location with multiple receivers, the union add-device page, a merged device with one entity set, and the per-receiver signal detail; every image the README references exists
- [ ] No user-facing string, panel string or runtime identifier uses "hub" outside migration-legacy reads

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
The screenshot harness lives in `tests/integration/`.

## Input Dependencies
Task 008's shipped behaviour.

## Output Artifacts
Documentation that matches the shipped model.

## Implementation Notes
Treat the prose rewrite as the always-deliverable; screenshot recapture is isolated and non-blocking if the harness cannot run in this environment. File-disjoint from task 010.
