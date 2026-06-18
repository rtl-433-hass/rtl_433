---
id: 3
group: "coordinator"
dependencies: [1]
status: "completed"
created: "2026-06-01"
skills:
  - python
---
# Seed the setup frequency into desired state on first connect

## Objective
Apply an optional initial center frequency (chosen at add time, MHz) to the hub's managed desired state exactly once, on the first-ever connect, layered over server adoption — so it is enforced on every reconnect and survives restarts without being re-applied each boot. Wire the value from `entry.data[CONF_INITIAL_FREQUENCY]` into the coordinator.

## Skills Required
- `python` — the coordinator's desired-state / adoption / enforcement lifecycle.

## Acceptance Criteria
- [ ] `Rtl433Coordinator.__init__` accepts `initial_center_frequency: float | None = None` (MHz) and stores it on `self`.
- [ ] On the first-ever connect (the existing `if not self._desired:` branch, after `_adopt_from_server()`), when `manage_settings` is on and an initial frequency is set, it is written to `self._desired[KEY_CENTER_FREQUENCY]`, marked managed, and persisted — overriding any adopted value and ignoring the hop-mode adoption skip (the user explicitly asked for a single frequency).
- [ ] The seed is applied only when `_desired` was empty (first run / post re-enable); it is never re-applied once desired state is persisted.
- [ ] `__init__.py` reads `CONF_INITIAL_FREQUENCY` from `entry.data` and passes it to the coordinator constructor.
- [ ] Linting passes; `coordinator.meta` and existing adoption behaviour for other fields are unchanged.

## Technical Requirements
- Files: `custom_components/rtl_433/coordinator/base.py`, `custom_components/rtl_433/__init__.py`.
- `KEY_CENTER_FREQUENCY` is already imported in `coordinator/base.py` (used in the adoption hop-mode guard, referenced as the literal `"center_frequency"` — prefer the `KEY_CENTER_FREQUENCY` constant).

## Input Dependencies
- Task 1: MHz semantics for the desired `center_frequency` value and `CONF_INITIAL_FREQUENCY` constant.

## Output Artifacts
- Coordinator one-shot frequency seeding.
- `__init__.py` wiring of the setup frequency.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

**`coordinator/base.py`:**
- Add the constructor parameter (keyword-only, alongside `manage_settings` etc.): `initial_center_frequency: float | None = None`, and store `self.initial_center_frequency = initial_center_frequency`. Document it in the class docstring's "Injectable attributes" list: a one-shot MHz center frequency seeded into desired state on first connect.
- In `_connect_loop`, the existing block is:
  ```python
  if self.manage_settings:
      try:
          if not self._desired:
              await self._adopt_from_server()
          await self._enforce_all()
      except Exception as err:  # noqa: BLE001
          ...
  ```
  Change the inner first-connect branch so the seed layers over adoption:
  ```python
  if not self._desired:
      await self._adopt_from_server()
      if self.initial_center_frequency is not None:
          self._desired[KEY_CENTER_FREQUENCY] = self.initial_center_frequency
          self._managed.add(KEY_CENTER_FREQUENCY)
          await self._persist_desired()
  await self._enforce_all()
  ```
  This guarantees: (a) the value is applied over any adopted/hop-skipped value, (b) it is persisted so it survives restarts, (c) `_enforce_all()` immediately sends it via `/cmd` (Task 1's `to_command` converts MHz→Hz), and (d) because `_desired` is now non-empty, it is never re-seeded on later connects.
- Use the `KEY_CENTER_FREQUENCY` constant (already imported) rather than the string literal.

**`__init__.py`:**
- Import `CONF_INITIAL_FREQUENCY` from `.const` (added in Task 1).
- In the `Rtl433Coordinator(...)` construction (~line 469), add:
  ```python
  initial_center_frequency=entry.data.get(CONF_INITIAL_FREQUENCY),
  ```
  No resolver/helper is needed — it is a plain optional value read from `entry.data` (only consulted when `manage_settings` is on, since the whole desired-state path is gated on `manage_settings`). It is not part of `_async_update_listener` comparisons.
</details>
