---
id: 10
group: "testing"
dependencies: [5, 6, 7, 8, 9]
status: "completed"
created: 2026-05-25
skills:
  - pytest
  - home-assistant
---
# Unit Tests + Project-Authored JSON Fixtures

## Objective
Provide full-coverage unit tests using `pytest-homeassistant-custom-component` that drive a small set of project-authored JSON event fixtures through normalization, the mapping library, and the entity/lifecycle paths — asserting device identity, entity set, classes/units, value transforms, binary payload handling, skip-field exclusion, dynamic late-field creation, restart restore, cascade removal, and availability transitions.

## Skills Required
- `pytest` — fixtures, async tests, parametrization, coverage
- `home-assistant` — `pytest-homeassistant-custom-component`, config-entry/test helpers

## Acceptance Criteria
- [ ] `tests/conftest.py` provides shared fixtures (event loop, mock hass via the plugin, `enable_custom_integrations`, sample config entries).
- [ ] `tests/fixtures/*.json` contains a small set of **project-authored** JSON event fixtures (one per representative model: e.g. an Acurite temp/humidity sensor, a wind/rain station, a power sensor, a contact/leak binary device), modeled on the documented rtl_433 field vocabulary. No upstream-derived capture data.
- [ ] Mapping tests: assert `lookup`/`apply_transform` produce the correct `device_class`/`unit`/`state_class`/value for representative fields; `should_skip` excludes `SKIP_KEYS`; the user-override file (temp dir) overrides/adds an entry.
- [ ] Normalizer tests: device key is deterministic/stable; handles missing `id` (channel-only / model-only); separates measurement vs skip fields.
- [ ] Coordinator tests: parses JSON frames, ignores empty/malformed frames, updates last-seen, dispatches; availability watchdog flips to unavailable past the timeout and recovers on a new event; per-device override beats hub default.
- [ ] Config-flow tests: hub user step (success + `cannot_connect`), discovery step creates a device entry, ignore prevents re-prompt, options flow updates discovery toggle + timeout.
- [ ] Lifecycle tests: dynamic late-field creates a new entity and persists across a reload; restart restore via `RestoreEntity`; **cascade removal** of a hub removes child device entries + devices/entities (no orphans).
- [ ] `pytest tests/ -q` passes locally; coverage of `custom_components/rtl_433` is high (target the full mapping + lifecycle paths). Run in the background and poll (see plan Execution Notes) if the suite is slow.
- [ ] A single conventional commit (e.g. `test: add unit tests and JSON fixtures`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Use `pytest-homeassistant-custom-component` (in `requirements_test.txt` from Task 1). Use its `hass` fixture and `MockConfigEntry`.
- Follow the **Meaningful Test Strategy**: "write a few tests, mostly integration." Focus on YOUR code (mapping, normalization, lifecycle, availability) — do not test HA framework internals or trivial getters. Combine related scenarios into single tests where natural.
- Mock the WebSocket connection (e.g. patch the aiohttp session / feed frames directly to the coordinator's parse method) rather than opening real sockets.
- Fixtures must be valid JSON and resemble real rtl_433 events (`{"time":..., "model":"Acurite-606TX", "id":..., "temperature_C":..., "battery_ok":...}`).

## Input Dependencies
- Tasks 5, 6, 7, 8, 9 — the full integration must exist to test it.

## Output Artifacts
- `tests/` suite + fixtures verifying Success Criteria #4, #5, #9, #10, #11 (and the mapping-only part of #6). Consumed by CI (Task 2) on the remote.

## Meaningful Test Strategy Guidelines
Your critical mantra: "write a few tests, mostly integration." Test custom business logic, critical paths, and edge cases specific to THIS integration (mapping semantics, device-key derivation, availability transitions, dynamic field creation, cascade removal, restore). Do NOT write tests for third-party/framework functionality, simple CRUD, getters/setters, config/static data, or things that would break immediately if wrong. Combine related scenarios into single tests; prefer integration-level tests over micro unit tests.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. **conftest.py**: enable custom integrations:
   ```python
   import pytest
   @pytest.fixture(autouse=True)
   def auto_enable_custom_integrations(enable_custom_integrations):
       yield
   ```
   Point the test harness at `custom_components/` (the plugin discovers `custom_components/rtl_433` when the repo root is the working dir; ensure the package is importable — a `tests/__init__.py` may be needed and the integration dir on the path).
2. **Fixtures**: author ~4-6 JSON files. Include at least one with an intermittent field appearing only in a later event (for the dynamic-creation test) — model this as two JSON objects (first without `battery_ok`, later with it).
3. **Mapping tests**: load library, assert a handful of representative descriptors; round-trip `apply_transform`. Override test: write a temp YAML, call `load_user_overrides`, assert merge.
4. **Coordinator tests**: instantiate the coordinator with a fake frame source. Directly invoke its frame-handling method with JSON strings (text frames), assert dispatcher sends (patch `async_dispatcher_send`) and last-seen updates. Advance time with `freezegun`/`async_fire_time_changed` to trigger the watchdog and assert availability.
5. **Config-flow tests**: use `hass.config_entries.flow.async_init` with the right source; patch `validate_connection`. For discovery, init with `SOURCE_INTEGRATION_DISCOVERY` and discovery data; assert a device entry is created. For ignore, simulate the ignore flow and assert re-discovery aborts.
6. **Lifecycle tests**: set up a hub + device `MockConfigEntry`; feed events; assert entities appear in the entity registry with correct unique_ids/classes; reload and assert persistence; remove the hub and assert child entries/devices/entities are gone.
7. Run with `pytest tests/ -q`. If slow, launch in background (`run_in_background`) and poll. Fix failures until green.
8. Only write under `tests/` (and a possible `tests/__init__.py`). This is parallel with Task 11 which writes under `tests/integration/` + docker — keep `tests/integration/` ownership to Task 11; this task owns `tests/` unit files and `tests/fixtures/`. Coordinate paths to stay disjoint.
9. Commit `test:`.
</details>
