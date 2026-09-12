---
id: 2
group: "union-devices-across-receivers"
dependencies: [1]
status: "completed"
created: 2026-09-12
skills:
  - python
  - home-assistant-integration
---
# Location config entry with per-receiver config subentries

## Objective
Model a logical location containing multiple receivers using HA config subentries, keeping one coordinator per receiver, and apply the subentry-ownership rule at every entity-registration site.

## Skills Required
python, home-assistant-integration

## Acceptance Criteria
- [ ] A parent **location** config entry exists; a `ConfigSubentryFlow` carries each receiver's `host`/`port`/`path` and per-receiver settings (managed-radio toggle, initial frequency)
- [ ] `async_step_user` collects host/port/path exactly as today and creates the location entry AND its first receiver subentry together — no receiver-less location is reachable
- [ ] Setup forwards platforms once on the location entry and constructs one coordinator per receiver subentry
- [ ] Receiver-owned entities (radio controls, noise sensors, connectivity) are added with their receiver's `config_subentry_id` and register a receiver device under that subentry
- [ ] Everything destined for a merged device is added with NO `config_subentry_id` (enforced now so later tasks inherit it)
- [ ] Existing entry `unique_id` schemes (`hub:{host}:{port}`, `serial:…`, `usbpath:…`, `template:…`) move to the SUBENTRY; the location entry's own `unique_id` is unset
- [ ] `async_step_reconfigure` and `async_step_hassio*` operate at subentry scope; a Supervisor-discovered server defaults to a new location with an option to attach to an existing one
- [ ] `uv run pytest tests/` passes and lint/format are clean

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
HA 2026.9 `ConfigSubentryFlow`. A device belongs to exactly one config entry and one subentry (registry storage v3). `async_add_entities(..., config_subentry_id=...)` is the ownership lever. The managed-radio desired-state `Store` stays keyed per receiver (`sdr_store_key`).

## Input Dependencies
Task 001's renamed identifiers and hardened parsers.

## Output Artifacts
The two-level topology every later task builds on; one coordinator per receiver.

## Implementation Notes
Prove a minimal location + one-receiver-subentry setup end-to-end (platform forward, coordinator construction, device registration, subentry ownership) before anything else is layered on. See plan Component 2 and Clarifications #8, #17, #19.
