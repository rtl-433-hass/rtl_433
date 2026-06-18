---
id: 4
group: "tests"
dependencies: [1, 2]
status: "completed"
created: "2026-05-31"
skills: ["python", "pytest"]
---

# Tests: never-expire and device-class default resolution

## Objective

Add focused, integration-first tests for the new never-expire (`0`) behavior and device-class-aware default resolution, and update any existing timeout/watchdog/availability tests that assumed a flat 600s default. Get the full suite green on Python 3.14 via uv.

## Skills Required

- `python`, `pytest`

## Meaningful Test Strategy Guidelines

Your critical mantra for test generation is: "write a few tests, mostly integration".

**Definition of "Meaningful Tests":** Tests that verify custom business logic, critical paths, and edge cases specific to the application. Focus on testing YOUR code, not the framework or library functionality.

**When TO Write Tests:** Custom business logic and algorithms; critical workflows and data transformations; edge cases and error conditions for core functionality; integration points between components; complex validation logic.

**When NOT to Write Tests:** Third-party/framework functionality; simple CRUD without custom logic; getters/setters; configuration/static data; obvious functionality that would break immediately if incorrect.

**Test Task Creation Rules:** Combine related scenarios into single tests; focus on integration and critical paths over unit coverage; avoid one test per trivial case.

## Output

New/updated tests under `tests/` covering the feature, plus adjustments to existing affected tests. Full suite passing.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

**How to run tests (verified constraint):** system Python is 3.13 but the test stack needs Python 3.14. Run via uv, e.g.:
```bash
uv run --python 3.14 pytest
```
(Confirm the exact invocation from README.md / AGENTS.md / CLAUDE.md / pyproject.toml; there may be a `uv run pytest` form if the project pins 3.14.) Do NOT validate syntax with python3.13 — `except A, B:` (PEP 758) is valid on 3.14 and python3.13 will report false positives.

**Existing tests to locate and possibly adjust** (grep `tests/` for `timeout`, `watchdog`, `available`, `effective_timeout`): tests around the coordinator watchdog, the resolver, and the entity `available` property. Any that hard-code the assumption "default timeout is 600 for all devices" must be updated to reflect that an event-driven payload now resolves to the longer default. Tests for periodic devices and for explicit overrides should still pass unchanged.

**New test cases to add (combine related scenarios; integration-first, exercising the real resolver/coordinator path):**

1. **Never-expire via per-device override.** Configure a device with `timeout_override = 0`. Set its `last_seen` far in the past. Assert the watchdog does NOT mark it unavailable and `entity.available` is `True`. (Once it has been seen, it stays available regardless of elapsed time.)

2. **Never-expire via hub default.** Set the hub `availability_timeout = 0` (explicit). Assert a device with no override and old `last_seen` stays available.

3. **Class default — event-driven device.** With NO explicit override/hub default, give a device a `latest` payload containing an event-driven key (e.g. `{"motion": 1, ...}` or `{"contact": 0, ...}` or `{"door": 1}`). Assert its effective timeout equals `DEFAULT_EVENT_DEVICE_TIMEOUT` (7200) and that it stays available between 600s and 7200s of silence (i.e. would have been wrongly unavailable under the old flat default).

4. **Class default — periodic device.** With NO explicit override/hub default, give a device a payload with only periodic fields (e.g. `{"temperature_C": 21.5, "humidity": 50}`). Assert effective timeout equals `DEFAULT_AVAILABILITY_TIMEOUT` (600) and it goes unavailable after 600s of silence.

5. **Resolution precedence.** (a) Per-device override wins over an explicit hub default and over the class default. (b) An explicit hub *number* wins over the class default for a non-overridden event-driven device (e.g. hub=300 → event device uses 300, not 7200). (c) "Never seen yet" (`last_seen is None`) still yields `available == False` even when timeout is 0.

6. **Edge: no payload yet.** A device_key present in `last_seen` but absent from `latest` (no cached payload) falls back to the safe 600s default without raising.

**Fixtures:** reuse existing `tests/conftest.py` fixtures (hass, mock config entry, coordinator). Build config entries with `data`/`options` containing `CONF_DEVICES` / `CONF_AVAILABILITY_TIMEOUT` / `DEVICE_TIMEOUT_OVERRIDE` as the production code expects. Prefer driving behavior through the public resolver / coordinator `_effective_timeout` / `entity.available` rather than asserting on internals.

**Completion bar:** the ENTIRE test suite passes under uv/Python 3.14 (not just the new tests). If pre-existing unrelated failures appear, confirm they exist on the base commit before attributing them to this change.

</details>
