---
id: 1
group: "settings-registry"
dependencies: []
status: "pending"
created: 2026-05-27
skills:
  - python
  - home-assistant
---
# Settings Registry module + new constants

## Objective
Create the single declarative **settings registry** that describes each of the six
controllable SDR fields once (internal key → getter source, `/cmd` setter command,
value⇄command transform, HA platform + entity-description parameters, capability
gate), and add the new `const.py` constants the rest of Plan 6 depends on
(management-toggle key + default, `Store` key/version). This task creates the
*contract* that the coordinator (Task 2), the control platforms (Task 4), and the
config flow (Task 3) all import. It must **not** modify `PLATFORMS` (that happens in
Task 4, together with the platform files, so the integration never forwards to a
platform module that does not exist yet).

## Skills Required
- `python` — module-level data structures, small pure transform functions, typing.
- `home-assistant` — knowing the `number`/`select`/`switch` entity-description fields
  (`EntityCategory`, `NumberMode`, units, min/max/step, select options) so the
  registry can carry exactly the metadata those platforms need.

## Acceptance Criteria
- [ ] A new module `custom_components/rtl_433/sdr_settings.py` defines a registry of
      the six logical fields: **center frequency**, **sample rate**, **ppm
      (frequency correction)**, **gain**, **conversion mode**, **hop interval**.
- [ ] Each registry entry declares, at minimum: a stable internal `key`; how to read
      the current value out of `coordinator.meta` (the Plan 3 getter result); the
      `/cmd` setter `command` name and whether the desired value is sent as `arg`
      (string) or `val` (integer); the value⇄command transform; the target HA
      `platform` (`"number"`/`"select"`/`"switch"`); the entity-description
      parameters needed to build the entity (name, `object_suffix`, unit, device
      class, bounds/step or options, `EntityCategory.CONFIG`); and a
      **capability gate** callable (today always returns `True`).
- [ ] **Gain** is modelled as the clarified *Number (dB) + "Auto gain" Switch* pair:
      the registry yields a `number` entity (gain in dB) and a `switch` entity
      ("Auto gain"), and exposes a helper that composes the single outbound `gain`
      command argument: `arg = ""` when auto is on, else `arg = str(<dB value>)`.
- [ ] **Conversion mode** is a `select` whose options map labels ⇄ integers:
      `native`→0, `si`→1, `customary`→2, and the `convert` command sends `val=<int>`.
- [ ] `const.py` gains: `CONF_MANAGE_SETTINGS` (`"manage_settings"`),
      `DEFAULT_MANAGE_SETTINGS = True`, and `Store` constants
      (`SDR_STORE_VERSION = 1` and an `sdr_store_key(entry_id)` helper or
      `SDR_STORAGE_KEY` template) for the coordinator's desired-state `Store`.
- [ ] `PLATFORMS` is **unchanged** in this task.
- [ ] `uv run ruff check custom_components/rtl_433` passes.

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- The module must import only from the standard library, Home Assistant helpers, and
  `.const`. It must **not** import `coordinator/*`, `entity.py`, or the platform
  modules — it sits *below* them in the dependency graph (mirrors how `mapping.py`
  is import-disjoint). The coordinator imports it as `from ..sdr_settings import ...`
  and the platforms as `from .sdr_settings import ...`.
- Exact outbound `/cmd` commands (from `WEBSOCKET_API.md`; do not invent fields):
  - center frequency → `center_frequency`, `val` = Hz (live).
  - sample rate → `sample_rate`, `val` = Hz (live).
  - ppm → `ppm_error`, `val` = integer (live).
  - gain → `gain`, `arg` = dB string, empty string = auto (live).
  - conversion mode → `convert`, `val` = integer 0/1/2 (config-setter).
  - hop interval → `hop_interval`, `val` = seconds (config-setter).
- Read sources in `coordinator.meta` (populated by Plan 3's `_refresh_meta`):
  `center_frequency`, `samp_rate`, `ppm_error`, `gain` (string, ""→auto),
  `conversion_mode`, `hop_interval` (= `hop_times[0]`), and `frequencies` (list, used
  by Task 2's hop-mode guard — the registry only needs to expose the read keys).

## Input Dependencies
None — this is the foundational task.

## Output Artifacts
- `custom_components/rtl_433/sdr_settings.py` — the registry + transforms, imported by
  Tasks 2, 3, 4.
- New `const.py` symbols: `CONF_MANAGE_SETTINGS`, `DEFAULT_MANAGE_SETTINGS`, the
  `Store` key/version constants.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

**Suggested registry shape.** A frozen dataclass per entry plus a module-level tuple.
Keep it pure data + tiny callables so the coordinator and platforms can both iterate it:

```python
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from homeassistant.components.number import NumberMode
from homeassistant.const import UnitOfFrequency
from homeassistant.helpers.entity import EntityCategory

@dataclass(frozen=True, kw_only=True)
class SdrSetting:
    key: str                      # stable internal key (also Store key)
    name: str                     # entity name (has_entity_name relative)
    object_suffix: str            # stable unique-id token -> f"{entry_id}:hub:{suffix}"
    platform: str                 # "number" | "select" | "switch"
    command: str                  # /cmd command name
    arg_kind: str                 # "val" (int) | "arg" (string)
    # read current value out of coordinator.meta:
    read: Callable[[dict[str, Any]], Any]
    # map a desired python value -> the value/arg actually sent on /cmd:
    to_command: Callable[[Any], Any]
    # number-only:
    native_min: float | None = None
    native_max: float | None = None
    native_step: float | None = None
    native_unit: str | None = None
    mode: NumberMode | None = None
    device_class: str | None = None
    # select-only:
    options: tuple[str, ...] | None = None
    # capability gate (today always True; future: per-server capability):
    capability: Callable[[dict[str, Any]], bool] = lambda meta: True
```

**Defensibly wide number bounds in box mode** (document them as comments):
- center frequency: 0 … 6_000_000_000 Hz, step 1, unit `Hz`, `NumberMode.BOX`,
  `device_class="frequency"`.
- sample rate: 0 … 20_000_000 Hz, step 1, unit `Hz`, BOX.
- ppm: -1000 … 1000, step 1, no unit, BOX.
- gain (dB): 0 … 100, step 0.1, unit `dB`, BOX.
- hop interval: 0 … 86400 s, step 1, unit `s`, BOX.

**Gain pair.** Model gain as two registry entries that share the same `command`
("gain"): a `number` entry (`object_suffix="gain"`, dB) and a `switch` entry
(`object_suffix="gain_auto"`, "Auto gain"). Provide a free function the coordinator
calls to build the single outbound arg from the *combined* desired state, e.g.:

```python
GAIN_DB_KEY = "gain"
GAIN_AUTO_KEY = "gain_auto"

def gain_command_arg(gain_db: float | None, gain_auto: bool) -> str:
    """Empty string = auto; otherwise the dB value as a string."""
    if gain_auto or gain_db is None:
        return ""
    # rtl_433 accepts e.g. "32.8"; trim a trailing ".0" for clean integers.
    return f"{gain_db:g}"
```
The coordinator stores `gain` (dB float) and `gain_auto` (bool) as two desired-state
keys but issues exactly one `gain` `/cmd` per enforcement/write.

**Conversion mode mapping.** Provide both directions:
```python
CONVERSION_MODES = ("native", "si", "customary")  # index == /cmd val
def conversion_label_to_val(label: str) -> int: return CONVERSION_MODES.index(label)
def conversion_val_to_label(val: int) -> str | None:
    return CONVERSION_MODES[val] if 0 <= val < len(CONVERSION_MODES) else None
```

**const.py additions** — place near the existing `CONF_*` / `DEFAULT_*` / signal
blocks, matching the file's comment style:
```python
# Per-hub toggle: let Home Assistant manage (adopt + enforce) the SDR settings.
CONF_MANAGE_SETTINGS: Final = "manage_settings"
DEFAULT_MANAGE_SETTINGS: Final = True

# Desired-state Store (helpers.storage.Store) for managed SDR settings, keyed by
# the hub entry_id so a value change never churns the config entry.
SDR_STORE_VERSION: Final = 1
def sdr_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.sdr_{entry_id}"
```

**Do not** add `Platform.NUMBER/SELECT/SWITCH` to `PLATFORMS` here — Task 4 does that
alongside creating `number.py`/`select.py`/`switch.py`.

Run `uv run ruff check custom_components/rtl_433` before finishing.
</details>
