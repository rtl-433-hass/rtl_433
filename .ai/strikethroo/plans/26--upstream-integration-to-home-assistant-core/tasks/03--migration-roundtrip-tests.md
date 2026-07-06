---
id: 3
group: "compatibility"
dependencies: [2]
status: "pending"
created: 2026-07-06
skills:
  - pytest
  - home-assistant
---
# Add Config-Entry Round-Trip Migration Tests

## Objective
Add tests in this repository that load a full-schema ("old"/full-build) config entry through the migration path and assert it round-trips without producing duplicate or orphaned entities or devices — proving the frozen compatibility contract holds in the direction that matters most (full build → minimal build).

## Skills Required
- **pytest**: `pytest-homeassistant-custom-component` fixtures, `MockConfigEntry`, entity/device registry assertions.
- **home-assistant**: config-entry migration and registry semantics.

## Acceptance Criteria
- [ ] A test constructs a `MockConfigEntry` at the current `version=2, minor_version=7` with representative options and pre-seeded registry entities/devices using the contract's exact `unique_id` and identifier formats.
- [ ] A test asserts `async_migrate_entry` (or setup) leaves those entities/devices intact — no duplication, no orphaning — when the entry is already at the latest version.
- [ ] A test constructs an older entry (e.g. `version=1` and/or a lower `minor_version`) and asserts migration reaches `version=2, minor_version=7` monotonically without downgrading and without destroying pre-existing entities/devices.
- [ ] Tests run green under the repo's Python 3.14 / `uv` test stack.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Follow the repo's existing test conventions (see `tests/` for `MockConfigEntry` usage and registry fixtures).
- Use the exact `unique_id` and device-identifier templates recorded in `COMPATIBILITY_CONTRACT.md` (Task 2) so the test encodes the ABI, not an approximation.
- Test philosophy: "write a few tests, mostly integration." Meaningful tests verify custom business logic (the migration ladder and identity preservation) — not framework behaviour. Combine related scenarios into a single test where sensible; do not write one test per minor-version bump unless a bump has distinct custom logic worth isolating. Favor a critical-path round-trip test over exhaustive per-field unit tests. Question whether trivial no-op bumps need dedicated coverage.

## Input Dependencies
- Task 2: `COMPATIBILITY_CONTRACT.md` (the exact formats and version ladder to encode).

## Output Artifacts
- New migration/round-trip test(s) under this repo's `tests/` tree, guarding the compatibility contract in CI.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

- Locate existing migration tests (search `tests/` for `async_migrate_entry`, `minor_version`, `MockConfigEntry`) and mirror their fixture/style so the new tests slot in cleanly.
- Build a helper that seeds the entity registry with entries whose `unique_id` matches `f"{entry_id}:{device_key}:{object_suffix}"` and the device registry with `identifiers={(DOMAIN, entry_id)}` and `{(DOMAIN, f"{entry_id}:{device_key}")}`, matching the contract.
- Round-trip assertion: capture the set of `(unique_id)` and device `identifiers` before and after migration/setup; assert the "after" set is a superset with no removed identity and no duplicated identity (same physical entity must not appear under two unique_ids).
- For the older-entry case, set `version=1` and assert the terminal state is `version=2, minor_version=7` and that `entry.version` never decreases through the path.
- Run with the repo's documented stack: `uv run pytest -k migration` (adjust selector to the new test names). Confirm green before marking complete.
- Do not test framework migration plumbing itself — only the integration's custom ladder and its identity-preservation guarantees.
</details>
