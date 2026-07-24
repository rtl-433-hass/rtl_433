---
id: 7
group: "tests-and-docs"
dependencies: [5]
status: "pending"
created: "2026-07-23"
skills:
  - documentation
  - home-assistant
---
# Documentation & screenshots: location/receiver model + union behavior

## Objective
Align all human-facing surfaces with the location/receiver model and the union behavior, and refresh the screenshots.

## Skills Required
- `documentation`: README/AGENTS prose; `home-assistant`: accurate model description.

## Acceptance Criteria
- [ ] `README.md` describes: the location entry with multiple receivers, adding/moving receivers into a location to get union, one device/one entity set with data from any receiver, per-receiver diagnostics, and that multiple location entries are for genuinely distant sites. No "hub" vocabulary remains.
- [ ] `AGENTS.md` updated: location + per-receiver-subentry topology, the aggregation layer, and the union/dedup/availability invariants.
- [ ] `translations/en.json` final pass: receiver vocabulary, subentry-flow strings, duplicate-history Repairs text.
- [ ] Screenshots in `docs/images/` recaptured to show a location with multiple receivers, a merged device with one entity set, and per-receiver diagnostics; every README-referenced image exists. (Prose is the always-deliverable; recapture is isolated/non-blocking if the harness cannot run — flag any images left to recapture.)

Use your internal Todo tool to track these.

## Technical Requirements
- Files: `README.md`, `AGENTS.md`, `translations/en.json`, `docs/images/*`.
- File-disjoint from Task 6 (tests), so the two may run in parallel.

## Input Dependencies
- Tasks 1–5 (final model/behavior to document).

## Output Artifacts
- Updated docs, translations, and screenshots.

## Implementation Notes
- If the screenshot harness cannot run in the environment, deliver the prose and explicitly list the images still needing recapture with the exact harness command.
