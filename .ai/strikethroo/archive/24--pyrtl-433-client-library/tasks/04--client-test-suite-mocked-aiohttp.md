---
id: 4
group: "client"
dependencies: [3]
status: "completed"
created: 2026-07-04
skills:
  - pytest
  - aiohttp
complexity_score: 8
complexity_notes: "Must build a scriptable fake aiohttp session/WS and drive every transport path (connect/read/reconnect/backoff/cmd) hard enough to reach base.py's ~0.79 mutation floor in isolation — the main risk driver of the plan."
---
# Client test suite against a mocked aiohttp session

## Objective
Give `Rtl433Client` an isolated test suite that drives its full transport surface
against a **mocked aiohttp session**, following the project's three-tier
convention, strong enough that the migrated client methods reach the same
per-module mutation floor `coordinator/base.py` holds today (task 6 verifies).

## Skills Required
- **pytest**: fixtures, async tests, the three-tier `test_* / test_mut_* /
  test_mut_*_floor` convention, exact-value/both-branch assertions.
- **aiohttp**: building test doubles for `ClientSession`, `ws_connect`, `WSMessage`/
  `WSMsgType`, and `session.get` responses.

## Acceptance Criteria
- [ ] `tests/conftest.py` provides a scriptable fake session fixture: a fake `ws_connect` yielding a caller-supplied sequence of `WSMessage`s (TEXT/CLOSE/ERROR/PING) and a fake `session.get` returning caller-scripted `/cmd` JSON, plus a fixture-loader for `tests/fixtures/*.json` and an injectable fake clock.
- [ ] `tests/test_client.py` (behavioral) drives: connect → seed meta/stats/dev_info over `/cmd` → read a `meta` frame, a replayed event, and a live event → assert the client emits a **live** `NormalizedEvent` and classifies the replayed one as replay; `{"shutdown": ...}` flips connectivity; empty/malformed/non-dict frames are dropped.
- [ ] `tests/test_mut_client.py` + `tests/test_mut_client_floor.py` assert exact request params and both branches of every conditional: `set gain` issues `GET /cmd?cmd=gain&arg=32.8`; `set center_frequency` sends `val` in Hz; backoff doubles `1→2→…→60` and caps at 60; frame routing per `WSMsgType`; `unwrap_result` on wrapped vs bare payloads; `validate_connection` returns True on success and raises `CannotConnect` on failure; malformed-JSON error-dedup logging.
- [ ] A forced WS `ERROR`/`CLOSE` frame triggers a reconnect attempt with the expected backoff timing (using the fake clock / patched sleep), asserted deterministically.
- [ ] `uv run pytest -q` passes with warnings-as-errors; the client's line coverage (`--cov=pyrtl_433`) covers the connect/read/cmd paths.

## Technical Requirements
- Test doubles must exercise error/exception paths (connection failure, CLOSE/ERROR frames, malformed JSON, `/cmd` non-200/HTML) since those are where `base.py`'s surviving mutants concentrate.
- Reference the parent's `tests/test_coordinator.py` and
  `tests/test_mut_coordinator_base*.py` for the *scenarios* to cover, but rewrite
  them against `Rtl433Client` + the fake session (no `hass`/`MockConfigEntry`).
- Port the JSON event fixtures under `tests/fixtures/` as needed.

## Input Dependencies
- Task 3: `pyrtl_433.Rtl433Client`, `CannotConnect`.
- Task 2 fixtures (if already copied) or copy the remaining `tests/fixtures/*.json`.

## Output Artifacts
- `tests/conftest.py`, `tests/test_client.py`, `tests/test_mut_client.py`,
  `tests/test_mut_client_floor.py`, `tests/fixtures/*.json`. Consumed by task 6.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

Build a fake session that a test can script:
```python
class FakeWS:
    def __init__(self, messages): self._messages = list(messages); self.closed = False
    def __aiter__(self): return self
    async def __anext__(self):
        if not self._messages: raise StopAsyncIteration
        return self._messages.pop(0)          # aiohttp.WSMessage(type, data, extra)
    async def close(self): self.closed = True

class FakeSession:
    def __init__(self, ws_messages=(), cmd_responses=None, fail_connect=False): ...
    def ws_connect(self, url, **kw): ...       # async context manager -> FakeWS (or raise)
    def get(self, url, params=None, **kw): ...  # async ctx mgr -> FakeResp(json=...)
```
Use `aiohttp.WSMessage`/`aiohttp.WSMsgType` for realistic message objects. For
backoff timing, patch `asyncio.sleep`/`wait_for` or inject a fake clock so the
reconnect delay is asserted without real waiting.

Drive the scenarios the parent's coordinator tests cover, translated to the new
API: on-connect meta+replay seeding, live vs replay vs stale-gap classification,
`/cmd` getter unwrap, `/cmd` setter param construction for every SDR command,
shutdown handling, reconnect/backoff, and `validate_connection` both outcomes.

**Test philosophy (mandatory restatement).** Meaningful tests verify custom
business logic, critical paths, and edge cases specific to this application — test
*your* code, not the framework. Write tests for: custom business logic/algorithms;
critical workflows and data transformations; edge cases and error conditions;
integration points; complex validation. Do **not** test third-party library
functionality, framework features, trivial getters/setters, or obviously-correct
code. Favor integration/critical-path coverage over per-method unit tests; combine
related scenarios into one test. **Exception governing this plan:** the work order
requires the *same mutation testing coverage* for the migrated client, so the
`test_mut_*`/`_floor` tiers deliberately assert exact values and both branches to
kill mutants — the task 6 mutation floor is the binding acceptance bar, so this
rigor is a direct requirement, not gold-plating.

Verify `uv run pytest -q` green before handing off to task 6.
</details>
