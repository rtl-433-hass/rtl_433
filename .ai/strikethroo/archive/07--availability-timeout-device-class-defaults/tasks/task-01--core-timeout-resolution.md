---
id: 1
group: "core-resolution"
dependencies: []
status: "completed"
created: "2026-05-31"
skills: ["python"]
---

# Core: device-class-aware + never-expire timeout resolution

## Objective

Centralize availability-timeout resolution so it is device-class-aware and honors `0` = "never expire", and make both the watchdog and the entity `available` property use it. This is the heart of the feature.

## Skills Required

- `python` (Home Assistant custom component internals)

## Output

Edits to:
- `custom_components/rtl_433/const.py` — new constants.
- `custom_components/rtl_433/__init__.py` — class-aware, explicit-vs-unset hub default handling in resolution.
- `custom_components/rtl_433/coordinator/base.py` — class-aware lookup using `self.latest`, and never-expire handling in `_async_watchdog`.
- `custom_components/rtl_433/entity.py` — never-expire handling in `available`.

## Implementation Notes

<details>
<summary>Detailed implementation guidance</summary>

**Verified current state (file:line approximate):**
- `const.py`: `CONF_AVAILABILITY_TIMEOUT = "availability_timeout"` (L47), `DEVICE_TIMEOUT_OVERRIDE = "timeout_override"` (L49), `DEFAULT_AVAILABILITY_TIMEOUT = 600` (L50), `DEFAULT_MOTION_CLEAR_DELAY = 90` (L53), `CONF_DEVICES = "devices"` (L59), and `BINARY_DEVICE_CLASS_KEYS` dict (L68) mapping rtl_433 keys → `BinarySensorDeviceClass`: `"motion"`/`"occupancy"` → MOTION, `"contact"`/`"opened"` → OPENING, `"door"` → DOOR, `"window"` → WINDOW, `"tamper"` → TAMPER, `"battery_ok"` → BATTERY. `BinarySensorDeviceClass` is imported at the top of const.py.
- `__init__.py`: `_hub_availability_timeout(entry)` returns `int(entry.options.get(CONF_AVAILABILITY_TIMEOUT, entry.data.get(CONF_AVAILABILITY_TIMEOUT, DEFAULT_AVAILABILITY_TIMEOUT)))`. `effective_timeout_resolver(entry)` returns a `_resolve(device_key)` closure: returns `int(override)` if `entry.data[CONF_DEVICES][device_key][DEVICE_TIMEOUT_OVERRIDE]` is not None, else `_hub_availability_timeout(entry)`. The closure is passed to the coordinator as the `timeout_resolver` kwarg in `async_setup_entry`.
- `coordinator/base.py`: `_WATCHDOG_INTERVAL = timedelta(seconds=30)` (L75). `__init__(self, hass, entry, *, timeout_resolver)` stores `self.last_seen: dict[str, datetime]` (L107), `self.available: dict[str, bool]` (L108), `self.latest: dict[str, dict[str, Any]]` (L109), `self._timeout_resolver = timeout_resolver` (L110). `self.latest[device_key] = payload` is set when an event arrives (~L130). `_effective_timeout(self, device_key)` (~L204) returns `self._timeout_resolver(device_key)`. `_async_watchdog` iterates `self.last_seen`, computes `stale = (now - seen) > timedelta(seconds=timeout)` and flips `self.available`.
- `entity.py`: `available` property reads `self._coordinator.last_seen.get(self._device_key)`; returns False if None; else `(dt_util.utcnow() - last_seen) <= timedelta(seconds=timeout)` where `timeout = self._coordinator._effective_timeout(self._device_key)`.

**Steps:**

1. **const.py** — add constants near the availability block:
   - `AVAILABILITY_TIMEOUT_NEVER: Final = 0` (sentinel for "never expire").
   - `DEFAULT_EVENT_DEVICE_TIMEOUT: Final = 7200`  # 2h, for event-driven binary-sensor classes.
   - A frozenset/set of the rtl_433 payload keys that mark a device as event-driven. Reuse the keys of `BINARY_DEVICE_CLASS_KEYS` but restrict to the open/close/motion classes (motion, occupancy, contact, opened, door, window). Suggested: `EVENT_DRIVEN_DEVICE_CLASS_KEYS: Final = frozenset({"motion", "occupancy", "contact", "opened", "door", "window"})`. (Exclude `tamper`/`battery_ok` since those alone don't make a device event-driven.)

2. **Classification helper** — add a small pure function that, given a payload dict (the rtl_433 JSON for a device), returns the appropriate *class default* timeout. Put it where it can be reused (e.g. a new helper in `coordinator/base.py` or a `_class_default_timeout(payload)` module function in `__init__.py`/a util). Logic: if `payload` is a dict and any key in `EVENT_DRIVEN_DEVICE_CLASS_KEYS` is present in the payload → return `DEFAULT_EVENT_DEVICE_TIMEOUT`; else return `DEFAULT_AVAILABILITY_TIMEOUT`.

3. **__init__.py — explicit-vs-unset hub default.** Change `_hub_availability_timeout` so callers can tell whether the user explicitly set a hub default. Implement a helper like `_explicit_hub_timeout(entry) -> int | None` that returns the stored value only if `CONF_AVAILABILITY_TIMEOUT` is present in `entry.options` or `entry.data` (i.e. `in`, not `.get(..., default)`), else `None`. Keep `_hub_availability_timeout` working (it can return the explicit value or `DEFAULT_AVAILABILITY_TIMEOUT`) for any existing callers, but the resolver must use the explicit-or-None form so the unset case can fall through to the class default.

4. **Resolver — make it class-aware.** The resolver runs per `device_key` and must consult the device's cached payload. Two acceptable designs (pick one, keep it simple):
   - (Preferred) Move the class-aware decision into the coordinator's `_effective_timeout`, since `self.latest[device_key]` is directly available there. Have `effective_timeout_resolver(entry)` return a closure that, given a `device_key`, returns: per-device override if explicitly set; else explicit hub default if set; else `None` (meaning "no explicit value — use class default"). Then in `coordinator._effective_timeout(device_key)`: call the resolver; if it returns a concrete int, use it; if it returns `None`, compute the class default from `self.latest.get(device_key)` via the helper from step 2.
   - (Alternative) Pass a `classifier: Callable[[str], int]` into the resolver that looks up `coordinator.latest`. Avoid circular-import/ordering problems if you do this.
   Whichever design: the entity's `available` property calls `coordinator._effective_timeout(device_key)`, so routing the class-aware logic through `_effective_timeout` guarantees both the watchdog and the entity see identical results. Update `entity.py` only if needed for never-expire (step 6).

5. **Never-expire in `_async_watchdog`.** After computing `timeout = self._effective_timeout(device_key)`, if `timeout == AVAILABILITY_TIMEOUT_NEVER` (0), the device must NOT be marked unavailable due to silence: skip the staleness check (treat as not stale / available). Ensure a device currently unavailable that resolves to never-expire is handled sanely (it won't be flipped to unavailable; the existing "becomes available again on event" path still applies). Do not break the existing branch that flips devices back to available.

6. **Never-expire in `entity.available`.** After computing `timeout`, if `timeout == 0`, return `True` when `last_seen is not None` (i.e. once the device has been seen at least once it stays available). Keep returning `False` when `last_seen is None` (never seen yet) to match existing behavior. Import `AVAILABILITY_TIMEOUT_NEVER` from const.

7. **Resolution order (must match plan):** per-device override (explicit) → explicit hub default → device-class default (from payload) → `DEFAULT_AVAILABILITY_TIMEOUT` (600). A `0` at any explicit level means never-expire and is returned as `0` (do not fall through). Note: an *explicit* hub default of `0` makes ALL non-overridden devices never-expire.

8. Keep imports tidy; add new constants to the `from .const import (...)` lists in `__init__.py`, `coordinator/base.py`, and `entity.py` as needed.

**Do NOT** change config_flow.py / translations / README here (those are task 2 and 3). **Do NOT** write tests here (task 4).

**Validation:** Do not validate syntax with python3.13. Functional verification happens in task 4 via the test suite (`uv` on Python 3.14).

</details>
