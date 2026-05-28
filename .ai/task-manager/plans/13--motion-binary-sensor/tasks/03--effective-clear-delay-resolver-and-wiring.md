---
id: 3
group: "per-device-config"
dependencies: [1]
status: "pending"
created: "2026-05-28"
skills:
  - python
---
# `effective_clear_delay_resolver` + wiring + options→data persist

## Objective
Provide a per-device effective-clear-delay resolver (override → descriptor default) and wire it through exactly as the availability-timeout override is wired, including persisting the options-flow value into the hub devices map at setup.

## Skills Required
`python` — Home Assistant integration setup (`__init__.py`).

## Acceptance Criteria
- [ ] `__init__.py` defines `effective_clear_delay_resolver(device_key)` mirroring `effective_timeout_resolver` (`__init__.py:188`): returns the per-device `DEVICE_MOTION_CLEAR_DELAY` from `entry.data[CONF_DEVICES][device_key]` if set, else the descriptor's `clear_delay` (motion default `DEFAULT_MOTION_CLEAR_DELAY`).
- [ ] The resolver is wired onto the coordinator/entity surface so `Rtl433BinarySensor` can read it (parallel to the timeout resolver wiring at ≈`__init__.py:258`).
- [ ] The options→data persist sync (≈`__init__.py:456-458`, where `DEVICE_TIMEOUT_OVERRIDE` is copied from `child.options`) also copies the motion clear-delay into `record[DEVICE_MOTION_CLEAR_DELAY]` when present in options.
- [ ] `uvx ruff check .` and `uvx ruff format --check .` pass.

## Technical Requirements
- Existing resolver: `effective_timeout_resolver` (`__init__.py:188-200`), wired at `__init__.py:258` (`coordinator.effective_timeout_resolver = ...`).
- Existing persist: `__init__.py:456-458` reads `child.options.get(CONF_AVAILABILITY_TIMEOUT)` → `record[DEVICE_TIMEOUT_OVERRIDE]`.
- Constants from Task 1: `DEVICE_MOTION_CLEAR_DELAY`, `DEFAULT_MOTION_CLEAR_DELAY`.
- The resolver needs the descriptor's `clear_delay` as the fallback — resolve the descriptor for the device's motion field via the registry/`lookup` available in setup, or accept `DEFAULT_MOTION_CLEAR_DELAY` as the fallback constant if a descriptor lookup is not readily available at that point. Prefer the descriptor value when reachable, else the constant.

## Input Dependencies
- Task 1: constants + `clear_delay` attribute.

## Output Artifacts
- `effective_clear_delay_resolver` available to the binary_sensor (consumed by Task 4).
- Options-set motion clear-delay persisted into the devices map (set by Task 5's form).

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. Locate `effective_timeout_resolver` in `__init__.py` (≈line 188). Add a sibling:
   ```python
   def effective_clear_delay_resolver(device_key: str) -> int:
       """Resolve a device's effective motion clear-delay (override > default)."""
       override = (
           entry.data.get(CONF_DEVICES, {})
           .get(device_key, {})
           .get(DEVICE_MOTION_CLEAR_DELAY)
       )
       if override is not None:
           return int(override)
       return DEFAULT_MOTION_CLEAR_DELAY
   ```
   If the loaded registry/`lookup` is in scope here, prefer resolving the motion descriptor's `clear_delay` as the fallback instead of the bare constant; otherwise the constant is acceptable (they are equal by Task 1/Task 2).

2. Wire it where `coordinator.effective_timeout_resolver = effective_timeout_resolver` is set (≈`__init__.py:258`):
   ```python
   coordinator.effective_clear_delay_resolver = effective_clear_delay_resolver
   ```
   Add a matching injectable attribute (default `None`) on the coordinator class docstring/attrs if the timeout one is declared there, so the binary_sensor can read `coordinator.effective_clear_delay_resolver`.

3. In the options→data persist block (≈`__init__.py:456-458`), alongside the timeout copy. Use the **single** key `DEVICE_MOTION_CLEAR_DELAY` (from Task 1) as both the options field key and the record key — there is no separate `CONF_*` const (Task 5 writes options under this same key):
   ```python
   clear_delay = child.options.get(DEVICE_MOTION_CLEAR_DELAY)
   if clear_delay is not None:
       record[DEVICE_MOTION_CLEAR_DELAY] = int(clear_delay)
   ```
   `const.py` is owned by Task 1; this task only imports from it.

4. Import the new constants from `.const`. Run ruff.
</details>
