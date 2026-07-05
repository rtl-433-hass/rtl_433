---
id: 3
group: "helpers"
dependencies: [1]
status: "completed"
created: 2026-07-04
skills:
  - python
  - home-assistant
complexity_score: 5
complexity_notes: "Library renamed SdrSetting→SdrCommand and SDR_SETTINGS→SDR_COMMANDS with a different field shape; a local adapter must reconcile names/fields so entity generation and /cmd arg composition stay byte-identical."
---
# Replace SDR transforms with pyrtl_433.sdr via a local adapter

## Objective
Delete the integration's duplicated SDR transform registry (`sdr_settings.py`) and source it
from `pyrtl_433.sdr`, bridged by a thin local adapter that reconciles the library's renamed,
reshaped API (`SdrCommand`/`SDR_COMMANDS`) onto exactly what the integration's consumers use
today (`SdrSetting`/`SDR_SETTINGS`). Entity generation and `/cmd` argument composition must be
behaviorally identical.

## Skills Required
- **python**: adapter design, dataclass/field mapping, registry reconciliation.
- **home-assistant**: how `_sdr.py`, `entity.py`, `select.py`, and `repairs.py` consume the
  SDR registry to build entities and handle settings.

## Acceptance Criteria
- [ ] `custom_components/rtl_433/sdr_settings.py` no longer defines the duplicated transforms/registry; the logic comes from `pyrtl_433.sdr`.
- [ ] A local adapter exposes exactly the names the integration consumes today (`SDR_SETTINGS`, `SDR_SETTINGS_BY_KEY`, `gain_command_arg`, `conversion_label_to_val`, `conversion_val_to_label`, `KEY_*`, and the `SdrSetting`-shaped access used by `_sdr.py`/`entity.py`), mapped from `pyrtl_433.sdr`'s `SdrCommand`/`SDR_COMMANDS`/`SDR_COMMANDS_BY_KEY`.
- [ ] Consumers `coordinator/_sdr.py`, `entity.py:69`, `select.py:32`, and `repairs.py:53` (`KEY_SAMPLE_RATE`) work unchanged in behavior against the adapter.
- [ ] Generated SDR entities and composed `/cmd` args are identical to pre-migration for a representative meta fixture (e.g. gain, sample rate, center frequency, conversion mode, hop interval).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- `pyrtl_433.sdr` (submodule, not re-exported from the package top level) provides:
  `SdrCommand` (fields `key`, `command`, `arg_kind`, `read`, `to_command`, `capability`,
  `available`), `SDR_COMMANDS`, `SDR_COMMANDS_BY_KEY`, `gain_command_arg`,
  `conversion_label_to_val`, `conversion_val_to_label`, `frequency_count`,
  `available_when_not_hopping`, `available_when_hopping`, the `read_*` meta readers,
  `int_command`, `mhz_to_hz_command`, `KEY_*`, and `CONVERSION_MODES`.
- The integration's `SdrSetting` has fields `command`, `read`, `to_command`, etc.; the
  library's `SdrCommand` is a superset with renamed/added fields. The adapter maps between
  them (e.g. surface `SDR_SETTINGS`/`SDR_SETTINGS_BY_KEY` aliases and, if consumers reference
  `SdrSetting`, alias the class or wrap instances).
- Import from the submodule: `from pyrtl_433.sdr import ...` (not `from pyrtl_433 import ...`).

## Input Dependencies
- Task 1: `pyrtl_433==0.1.0` installed and importable.

## Output Artifacts
- A local SDR adapter presenting the integration's expected API over `pyrtl_433.sdr`.
- A stable SDR registry surface for Task 4 (coordinator settings management) to build on.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

1. Enumerate exactly what each consumer imports from `sdr_settings` (from the map):
   - `coordinator/_sdr.py:25` — `KEY_CENTER_FREQUENCY, KEY_GAIN_AUTO, KEY_GAIN_DB, SDR_SETTINGS,
     SDR_SETTINGS_BY_KEY, gain_command_arg`.
   - `entity.py:69` — `SDR_SETTINGS`.
   - `select.py:32` — `conversion_label_to_val, conversion_val_to_label`.
   - `repairs.py:53` — `KEY_SAMPLE_RATE`.
2. Create the adapter (simplest: repurpose `sdr_settings.py` as a thin adapter module that
   imports from `pyrtl_433.sdr` and re-exposes the integration's names). Map:
   - `SDR_SETTINGS = SDR_COMMANDS`, `SDR_SETTINGS_BY_KEY = SDR_COMMANDS_BY_KEY` (verify iteration
     order / key set match what `entity.py` and `_sdr.py` expect).
   - Re-export `gain_command_arg`, `conversion_label_to_val`, `conversion_val_to_label`, and all
     `KEY_*` constants directly.
   - If any consumer accesses `SdrSetting`-specific attributes that differ on `SdrCommand`,
     provide a shim (alias `SdrSetting = SdrCommand` if field access is compatible, else wrap).
3. Check field-access compatibility: `_sdr.py`/`entity.py` read `.read`, `.to_command`,
   `.command`, and availability/capability predicates off each setting. Confirm `SdrCommand`
   exposes equivalents (`read`, `to_command`, `command`, `capability`, `available`). Adapt any
   attribute-name gap.
4. Delete the duplicated transform/registry definitions from the old `sdr_settings.py`, leaving
   only the adapter.
5. Prove parity: for a representative `meta` dict, assert the set of generated settings, their
   keys, availability, and composed `/cmd` args match pre-migration. Full test rewiring is Task 5.

Keep this disjoint from the normalizer/replay work (Task 2) — different modules, parallelizable.
</details>
