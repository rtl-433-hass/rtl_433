---
id: 2
group: "replay-suppression"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - python
  - pytest
---
# Tests for replay suppression (+ event fixture)

## Objective
Add tests that lock in replay-suppression behavior, plus a new event-device fixture (none exists today).

## Skills Required
- `python`, `pytest` — `pytest-homeassistant-custom-component`, existing coordinator/lifecycle test harness.

## Acceptance Criteria
- [ ] A new event-device fixture is created under `tests/fixtures/` (e.g. a `button`/doorbell/`secret_knock` frame) since the existing fixtures are sensor/binary only.
- [ ] **Gap-event suppression**: feed a live event (advances mark), simulate reconnect, feed a frame with `time` older than `REPLAY_STALE_THRESHOLD` but unseen → event entity does NOT fire (spy `_trigger_event` or the HA event bus) AND an INFO log line recorded the suppression (`caplog`).
- [ ] **No double-fire on a blip**: feed a live event (fires once), replay the SAME frame (`time <= mark`) → does NOT fire again.
- [ ] **Fresh event at reconnect fires**: with mark set, feed a frame `time ≈ now` (within threshold) → fires. Steady-state repeats of the same value also fire.
- [ ] **Offline not resurrected**: advance past timeout + run watchdog (as `test_coordinator.py:225`), feed replayed/stale frames → `available[key]` stays `False`, `last_seen[key]` unchanged; then a live frame restores availability.
- [ ] **Sensors seed from replay**: feeding only replayed/stale frames for a fresh sensor device → `coordinator.devices[key].fields` reflects values and a dispatched sensor wrote state, while `last_seen` was NOT stamped.
- [ ] **Timestamp parsing variance** (unit): `"2026-05-25 10:00:00"` (local naive), an ISO-8601/`Z` value, and blank/garbage → expected replay/stale/live decision each; blank/garbage treated as live and never raises.
- [ ] **`time` absent from fields**: a `test_normalizer.py` assertion that `"time"` is not in `NormalizedEvent.fields`.
- [ ] `python -m pytest tests/test_coordinator.py tests/test_normalizer.py tests/test_lifecycle.py -q` and full `pytest -q` pass; `ruff` clean.

## Technical Requirements
- Files: `tests/test_coordinator.py` (classification/parse unit + offline + sensor-seed), `tests/test_lifecycle.py` (event firing/suppression — there is NO `tests/test_event.py`; create one only if cleaner), `tests/test_normalizer.py` (`time` absence), `tests/fixtures/<new event fixture>.json`.
- Feed frames via `coordinator._handle_text_frame(...)` (see `test_coordinator.py:83,176`, watchdog `:225`).

## Input Dependencies
- Task 1 (mechanism must exist).

## Output Artifacts
- New/updated tests and an event fixture.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

- Study `tests/test_coordinator.py` (frame-feeding, watchdog advancing time) and `tests/test_lifecycle.py` (how event entities are built + how firing is observed) and `tests/conftest.py` fixtures before writing.
- For the event fixture, model a real rtl_433 button/doorbell frame: include `model`, an id, a momentary field that maps to an `event` entity, and a `time`. Check `device_library/` mappings + `normalizer.py` to pick a field that becomes an `event` platform entity.
- Control `time` per frame by setting the `time` value in the fed dict; control `now` via the same time-advancing approach the watchdog tests use (freeze/advance HA time) so age comparisons are deterministic.
- To assert firing/non-firing, spy on `Rtl433Event._trigger_event` or capture `homeassistant` event bus events for the entity's event type. Use `caplog.at_level(logging.INFO)` for the suppression log assertion.

### Meaningful Test Strategy Guidelines
Your critical mantra: "write a few tests, mostly integration".
- Test YOUR classification/suppression logic and the seed-vs-liveness split — not HA's event entity internals.
- Combine related scenarios into single flow tests where natural; the parser variance check is the one good pure unit test.
</details>
