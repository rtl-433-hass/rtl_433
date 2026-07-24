---
id: 1
group: "topology-foundation"
dependencies: []
status: "pending"
created: "2026-07-23"
skills:
  - python
  - home-assistant
---
# Topology foundation: location entry + per-receiver subentries, and the hub→receiver rename

## Objective
Establish the new config-entry topology and vocabulary every later task builds on: a **location** config entry that contains one **config subentry per receiver** (host/port/path), one coordinator per receiver subentry, and the full "hub"→"receiver" rename (user-facing strings and internal identifiers), plus the new constants (location/receiver keys, the `_MERGE_DEBOUNCE` window). No union behavior yet — this task lands the skeleton and renames in place, keeping existing behavior working (one receiver per location by default).

## Skills Required
- `python`: config-entry / subentry flow wiring, constants, mechanical rename across modules.
- `home-assistant`: `ConfigSubentryFlow`, `async_forward_entry_setups`, `hass.data` scoping, dispatcher-signal naming.

## Acceptance Criteria
- [ ] `config_flow.py` implements a location entry plus a `ConfigSubentryFlow` "add a receiver" step collecting host/port/path (the old single-endpoint `async_step_user` becomes the receiver subentry step); `async_step_reconfigure` / `async_step_hassio*` operate at subentry scope; a Supervisor-discovered server defaults to a new location.
- [ ] `__init__.py` sets up a location entry, constructs **one coordinator per receiver subentry**, and forwards platforms once on the location entry.
- [ ] `const.py` adds the location/receiver/subentry keys and `_MERGE_DEBOUNCE` (named constant, ~2–5 s); keeps every legacy key the migration still reads (`CONF_HUB_ENTRY_ID`, `ENTRY_TYPE_*`, the `:hub:` literal knowledge).
- [ ] "hub" is renamed to "receiver" across user-facing strings (`translations/en.json`) and internal identifiers (`hub_entry_id`→receiver/location ids, `signal_hub_update`, `Rtl433HubEntity`/`Rtl433HubControl`, `CONF_HUB_ENTRY_ID` usages), except reads the migration needs.
- [ ] `uv run ruff check custom_components/rtl_433` is clean and the existing suite still passes for a single-receiver-per-location setup (tests updated minimally to construct the new topology).

Use your internal Todo tool to track these.

## Technical Requirements
- Files: `config_flow.py`, `options_flow.py`, `__init__.py`, `const.py`, `coordinator/base.py` (attribute renames only), `entity.py` (hub-entity class renames only), `translations/en.json`.
- Preserve `object_suffix` values byte-identical (AGENTS.md guardrail); the `:hub:` unique-id literal rewrite itself is done in Task 5 (migration), so runtime construction may adopt the new `receiver` literal here but MUST be paired with Task 5.

## Input Dependencies
None.

## Output Artifacts
- Location + receiver-subentry topology and one-coordinator-per-receiver wiring (consumed by Tasks 2, 3, 4).
- New constants incl. `_MERGE_DEBOUNCE` (consumed by Tasks 2, 3).
- Renamed identifiers (consumed by all later tasks).

## Implementation Notes
- Keep the coordinator per-receiver — most existing setup logic is reused unchanged; only the ownership (subentry) and identity prefix change.
- Discovery toggle stays per-receiver; new-device registration will dedupe at the location level in Task 3.
- Do NOT change device identity here (that is Task 2); this task only re-parents entries and renames.
