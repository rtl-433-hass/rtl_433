---
id: 2
group: "migration"
dependencies: [1]
status: "completed"
created: 2026-07-04
skills:
  - python
  - pytest
complexity_score: 6
complexity_notes: "Four modules ported at once; replay/sdr/url tests must be extracted from larger HA test files and decoupled from dt_util, not copied verbatim."
---
# Migrate the pure protocol helpers with their tests

## Objective
Move the four already-framework-agnostic protocol helper modules into
`pyrtl_433/`, stripping every Home Assistant coupling, and port their unit tests
so each helper is independently exercised. These are the pieces `Rtl433Client`
(task 3) depends on.

## Skills Required
- **python**: porting pure modules, removing `homeassistant.util.dt` coupling,
  replacing the shared `LOGGER` with `logging.getLogger(__name__)`.
- **pytest**: porting/authoring the behavioral and mutation-killing tests.

## Acceptance Criteria
- [ ] `pyrtl_433/normalizer.py` exists with `NormalizedEvent`, `IDENTITY_KEYS`, `DEFAULT_SKIP_KEYS`, `_safe_token`, `device_key`, `normalize` — behavior identical to the source, no HA imports.
- [ ] `pyrtl_433/replay.py` exists with `ReplayVerdict`, `classify_replay`, `REPLAY_STALE_THRESHOLD`, `DISCOVERY_BACKLOG_GRACE`, and a `parse_event_time` function re-expressed with stdlib `datetime` (no `dt_util`).
- [ ] `pyrtl_433/sdr.py` exists with `gain_command_arg`, `conversion_label_to_val`, `conversion_val_to_label`, `CONVERSION_MODES`, the meta `read_*` functions, `int_command`/`mhz_to_hz_command`, the frequency/hop capability gates, and a slimmed protocol-only command descriptor (fields: `key`, `command`, `arg_kind`, `read`, `to_command`) — **no** `NumberMode`/`device_class`/`EntityCategory`/unit imports.
- [ ] `pyrtl_433/_urls.py` exists with `build_ws_url`, `build_cmd_url`, `unwrap_result`.
- [ ] `grep -rn "homeassistant" pyrtl_433/` returns nothing.
- [ ] Test files `tests/test_normalizer.py`, `tests/test_replay.py`, `tests/test_sdr.py`, `tests/test_urls.py` (plus `test_mut_*` variants where the source had them) exist and pass under `uv run pytest -q`.
- [ ] JSON event fixtures needed by the tests are copied into `tests/fixtures/`.

## Technical Requirements
- Sources (read-only reference) in the `rtl_433` checkout:
  - `custom_components/rtl_433/normalizer.py` (already stdlib-only — copy verbatim, rename package).
  - `custom_components/rtl_433/coordinator/_events.py` → `ReplayVerdict`, `classify_replay`, the two threshold constants, and `_EventProcessingMixin._parse_event_time` (decouple from `dt_util.parse_datetime`/`as_utc`).
  - `custom_components/rtl_433/sdr_settings.py` → the pure protocol half (drop the HA entity-description fields of `SdrSetting`).
  - `custom_components/rtl_433/coordinator/base.py` → `_build_ws_url`, `_build_cmd_url`, `_unwrap_result`.
- Tests to port: `tests/test_normalizer.py` (near-verbatim); extract `classify_replay` tests from `tests/test_coordinator.py`/`tests/test_mut_coordinator_base*.py`; extract SDR-transform tests from `tests/test_sdr_controls.py`/`tests/test_mut_sdr_settings_floor.py`; write small fresh tests for the URL builders + `unwrap_result` (their source tests live inside the coordinator-base test files).
- Fixtures: `tests/fixtures/*.json` in the source repo.

## Input Dependencies
- Task 1: the `pyrtl_433` package skeleton, `tests/` dir, and working pytest toolchain.

## Output Artifacts
- `pyrtl_433/normalizer.py`, `pyrtl_433/replay.py`, `pyrtl_433/sdr.py`, `pyrtl_433/_urls.py` and their tests. Consumed by task 3 (client) and task 6 (baseline).

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

**normalizer.py** — copy `custom_components/rtl_433/normalizer.py` verbatim; it is
stdlib-only. No changes beyond living under `pyrtl_433/`.

**replay.py** — lift `ReplayVerdict`, `classify_replay`, `REPLAY_STALE_THRESHOLD`,
`DISCOVERY_BACKLOG_GRACE` from `coordinator/_events.py` (they are already pure).
Re-home `_parse_event_time` as a module function `parse_event_time(raw: str |
None) -> datetime | None` using stdlib parsing:
- Try ISO-8601 via `datetime.fromisoformat`; for the rtl_433 local
  `"YYYY-MM-DD HH:MM:SS"` form (no tz), parse then attach the local tz and
  convert to UTC, mirroring the source's `dt_util` behavior. Keep "unparseable →
  None (treated live)" semantics.
Replace `from ..const import LOGGER` with `logging.getLogger(__name__)`.

**sdr.py** — port from `sdr_settings.py`:
- Keep: `gain_command_arg`, `conversion_label_to_val`, `conversion_val_to_label`,
  `CONVERSION_MODES`, `_read_center_frequency`→`read_center_frequency` (and the
  other `_read_*` → `read_*`), `_int_command`→`int_command`,
  `_mhz_to_hz_command`→`mhz_to_hz_command`, `_frequency_count`,
  `_available_when_not_hopping`, `_available_when_hopping`, the `KEY_*` constants.
- Replace the HA-coupled `SdrSetting` dataclass with a slim protocol descriptor,
  e.g. `@dataclass(frozen=True) class SdrCommand:` carrying only `key: str`,
  `command: str`, `arg_kind: str`, `read: Callable`, `to_command: Callable` (drop
  `platform`, `name`, `native_*`, `mode`, `device_class`, `options`,
  `EntityCategory`). Build the `SDR_COMMANDS` tuple + `SDR_COMMANDS_BY_KEY` index.
- Note the source uses Python 3.14 `except TypeError, ValueError:` (PEP 758) —
  keep it; run under 3.14.
- Drop imports of `homeassistant.components.number`, `homeassistant.const`,
  `homeassistant.helpers.entity`.

**_urls.py** — `build_ws_url(host, port, path, *, secure=False)`,
`build_cmd_url(host, port, *, secure=False)`, `unwrap_result(payload)` — copied
from `base.py`'s module-level helpers / `_unwrap_result` staticmethod.

**Tests** — port near-verbatim where a 1:1 source test exists
(`tests/test_normalizer.py`). For `replay`/`sdr`/`_urls`, extract the relevant
cases from the coordinator-base and sdr-controls test files and rewrite them to
call the new module functions directly (drop any `hass`/`MockConfigEntry`
scaffolding — these are pure functions). Copy `tests/fixtures/*.json` as needed.

**Test philosophy (mandatory restatement).** Meaningful tests verify custom
business logic, critical paths, and edge cases specific to this application — test
*your* code, not the framework. Write tests for: custom business logic and
algorithms; critical workflows and data transformations; edge cases and error
conditions; integration points; complex validation/calculation. Do **not** write
tests for third-party library functionality, framework features, trivial
getters/setters, or obviously-correct code. Favor integration/critical-path
coverage over per-method unit tests; combine related scenarios into one test.
**Exception governing this plan:** the work order requires the *same mutation
testing coverage* for the migrated code, so exact-value and both-branch
assertions that kill mutants are explicitly in scope here (the mutation floor in
task 6 is the binding acceptance bar); this is not gold-plating.

Verify: `grep -rn homeassistant pyrtl_433/` is empty; `uv run pytest -q` green.
</details>
