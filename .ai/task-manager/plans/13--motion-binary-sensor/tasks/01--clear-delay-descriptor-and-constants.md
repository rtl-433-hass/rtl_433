---
id: 1
group: "foundation"
dependencies: []
status: "pending"
created: "2026-05-28"
skills:
  - python
---
# Foundation: `clear_delay` descriptor attribute + motion constants

## Objective
Add the optional `clear_delay` capability to the device-library descriptor model and the per-device/default constants the rest of the feature depends on. This is the foundation every other task builds on.

## Skills Required
`python` — dataclass + module constants in the integration.

## Acceptance Criteria
- [ ] `FieldDescriptor` (in `custom_components/rtl_433/mapping.py`) gains an optional attribute `clear_delay: int | None = None`.
- [ ] The descriptor loader parses `clear_delay` from YAML with no other change (it is derived from `fields(FieldDescriptor)`), and an invalid value (non-positive / non-int) is ignored with a debug log and treated as "no auto-off".
- [ ] `const.py` gains `DEVICE_MOTION_CLEAR_DELAY` (per-device record sub-key, string `"motion_clear_delay"`) and `DEFAULT_MOTION_CLEAR_DELAY` (int `90`), placed alongside the existing `DEVICE_TIMEOUT_OVERRIDE` / `DEFAULT_AVAILABILITY_TIMEOUT`.
- [ ] `uvx ruff check .` and `uvx ruff format --check .` pass.

## Technical Requirements
- `mapping.py`: `FieldDescriptor` is a frozen dataclass (around `mapping.py:61`); `_DESCRIPTOR_ATTRS` is built from `fields(FieldDescriptor)` (≈`mapping.py:88`) and the loader constructs `FieldDescriptor(field_key=field_key, **known)` (≈`mapping.py:131`).
- `const.py`: existing keys `DEVICE_TIMEOUT_OVERRIDE` (`const.py:78`), `DEFAULT_AVAILABILITY_TIMEOUT` (`const.py:131`).

## Input Dependencies
None.

## Output Artifacts
- `clear_delay` attribute available on every descriptor.
- `DEVICE_MOTION_CLEAR_DELAY` and `DEFAULT_MOTION_CLEAR_DELAY` constants.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. In `custom_components/rtl_433/mapping.py`, add to the `FieldDescriptor` dataclass (keep it with the other optional fields, after `icon` is fine):
   ```python
   clear_delay: int | None = None
   ```
   Because `_DESCRIPTOR_ATTRS = frozenset(f.name for f in fields(FieldDescriptor) if f.name != "field_key")`, the loader will accept `clear_delay` from YAML automatically — do **not** hand-edit the parse logic for the happy path.

2. Add light validation where the descriptor is built from a YAML entry (the function around `mapping.py:116-131`, just before `return FieldDescriptor(...)`): if `known.get("clear_delay")` is present but not a positive int, drop it with a debug log, mirroring the tolerant style already used for `payload`/transforms:
   ```python
   if "clear_delay" in known:
       raw = known["clear_delay"]
       if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
           LOGGER.debug("Ignoring invalid 'clear_delay' %r on field %r", raw, field_key)
           known.pop("clear_delay")
   ```
   (`isinstance(raw, bool)` guard because `bool` is an `int` subclass.)

3. In `custom_components/rtl_433/const.py`, next to `DEVICE_TIMEOUT_OVERRIDE`:
   ```python
   DEVICE_MOTION_CLEAR_DELAY: Final = "motion_clear_delay"  # int seconds, or absent/None
   ```
   and next to `DEFAULT_AVAILABILITY_TIMEOUT`:
   ```python
   DEFAULT_MOTION_CLEAR_DELAY: Final = 90
   ```
   Add a one-line comment matching the surrounding documentation style.

4. Run `uvx ruff check .` and `uvx ruff format --check .`.
</details>
