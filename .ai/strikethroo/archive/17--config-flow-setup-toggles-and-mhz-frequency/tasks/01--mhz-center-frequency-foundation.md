---
id: 1
group: "mhz-foundation"
dependencies: []
status: "completed"
created: "2026-06-01"
skills:
  - python
---
# Convert Center-frequency to MHz and add the SDR Store upgrade path

## Objective
Move the human-facing Center-frequency representation to MHz (the persisted desired-state value, the editable `number` control, and the read-only diagnostic `sensor`) while keeping the rtl_433 `/cmd` wire protocol and `coordinator.meta` in Hz. Bump the SDR desired-state Store version and add a one-time Hz→MHz migration so existing managed hubs keep the correct frequency. Also add the `CONF_INITIAL_FREQUENCY` constant for downstream config-flow use, and the `SDR_STORE_VERSION` bump — both in `const.py` — so later parallel tasks need not touch `const.py`.

## Skills Required
- `python` — Home Assistant entity/registry patterns, `homeassistant.helpers.storage.Store` migration.

## Acceptance Criteria
- [ ] The `KEY_CENTER_FREQUENCY` `SdrSetting` presents MHz (`UnitOfFrequency.MEGAHERTZ`) with MHz `native_min`/`native_max`/`native_step`; its `read` converts `meta` Hz→MHz and its `to_command` converts the desired MHz value to an integer Hz `val`.
- [ ] The editable Center-frequency `number` control displays MHz (inherited from the registry) and still writes via `set_sdr`.
- [ ] The diagnostic Center-frequency `sensor` displays MHz (`native_unit` + value lambda divide by 1e6), retaining `device_class=FREQUENCY` and its `frequencies`/`hop_times` attributes.
- [ ] `SDR_STORE_VERSION` is bumped to `2` in `const.py`.
- [ ] The coordinator's SDR Store migrates a version-1 payload by converting `values["center_frequency"]` Hz→MHz; missing/empty/non-numeric values pass through unchanged; an already-MHz (version-2) payload is untouched.
- [ ] `CONF_INITIAL_FREQUENCY = "initial_frequency"` is added to `const.py`.
- [ ] `ruff`/project linting passes; no behavioural change to non-Center-frequency settings or to `coordinator.meta` (still Hz).

## Technical Requirements
- Files: `custom_components/rtl_433/sdr_settings.py`, `custom_components/rtl_433/number.py` (verify only — likely no change since unit comes from the registry), `custom_components/rtl_433/sensor.py`, `custom_components/rtl_433/const.py`, `custom_components/rtl_433/coordinator/base.py`.
- The wire/meta layer stays Hz; conversion is confined to the Center-frequency setting's `read` (Hz→MHz) and `to_command` (MHz→Hz int).

## Input Dependencies
None.

## Output Artifacts
- MHz Center-frequency setting + entities.
- Versioned SDR Store with Hz→MHz migration.
- `CONF_INITIAL_FREQUENCY` constant and `SDR_STORE_VERSION = 2`.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

**`sdr_settings.py` — Center-frequency setting (the `SdrSetting` with `key=KEY_CENTER_FREQUENCY`):**
- Change `native_unit=UnitOfFrequency.HERTZ` → `UnitOfFrequency.MEGAHERTZ` (already imported `from homeassistant.const import UnitOfFrequency`).
- Change bounds to MHz: `native_min=0`, `native_max=6000`, `native_step=0.001` (1 kHz resolution; document this in the inline comment, replacing the "0 .. 6 GHz" comment).
- Replace `_read_center_frequency` to convert Hz→MHz:
  ```python
  def _read_center_frequency(meta: dict[str, Any]) -> Any:
      """Read center frequency from meta (Hz) as MHz, or None when absent."""
      hz = meta.get("center_frequency")
      if hz is None:
          return None
      try:
          return float(hz) / 1_000_000
      except (TypeError, ValueError):
          return None
  ```
  Note: use the 3.14 PEP 758 form `except (TypeError, ValueError):` (parenthesised) to match the surrounding code style; some existing helpers use the unparenthesised `except A, B:` form — either parses on 3.14, prefer the parenthesised tuple here for clarity.
- The Center-frequency setting currently uses the shared `to_command=_int_command`. Give it a dedicated converter that maps MHz→Hz int:
  ```python
  def _mhz_to_hz_command(value: Any) -> int:
      """Map a desired center frequency in MHz to the integer Hz ``val``."""
      return int(round(float(value) * 1_000_000))
  ```
  and set `to_command=_mhz_to_hz_command` on that setting only. Leave `sample_rate` etc. on `_int_command` (they remain Hz).
- Do NOT change `_available_when_not_hopping` / `_frequency_count` / `_available_when_hopping` — they read `meta` (Hz/list) and are unaffected.

**`number.py`:** the Center-frequency control inherits `native_unit`, `native_min/max/step` from the setting, so no code change is expected. Verify `Rtl433NumberControl.__init__` copies them from `setting` (it does). `native_value` returns `get_desired(key)` (now MHz) falling back to `read(meta)` (now MHz) — consistent. No change needed; confirm and leave as is.

**`sensor.py`:** the `HubSensorDesc` with `suffix="center_frequency"`:
- Change `native_unit=UnitOfFrequency.HERTZ` → `UnitOfFrequency.MEGAHERTZ`.
- Change `value=lambda c: _meta(c, "center_frequency")` to divide by 1e6, guarding None:
  ```python
  value=lambda c: (
      None if (_hz := _meta(c, "center_frequency")) is None
      else _hz / 1_000_000
  ),
  ```
  (Use whatever the existing `_meta` helper returns; if it already returns None for missing, this guard is sufficient.) Keep `device_class=SensorDeviceClass.FREQUENCY` and the `attrs` lambda unchanged (the `frequencies`/`hop_times` attributes stay raw from meta). Ensure `UnitOfFrequency` is imported in `sensor.py` (it already imports it for the Hz usage).

**`const.py`:**
- Bump `SDR_STORE_VERSION: Final = 1` → `2`. Update the surrounding comment to note version 2 stores `center_frequency` in MHz (migrated from Hz in version 1).
- Add near the other `CONF_*` hub keys:
  ```python
  # Optional one-shot initial center frequency (MHz) chosen at add time and
  # seeded into the hub's managed desired state on first connect. Absent means
  # "adopt the server's current frequency". Only applied when manage_settings is on.
  CONF_INITIAL_FREQUENCY: Final = "initial_frequency"
  ```

**`coordinator/base.py` — SDR Store migration:**
- The Store is constructed at `self._store = Store(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))` (~line 278).
- Define a small `Store` subclass in this module that migrates version 1 → 2 by converting the persisted `center_frequency` from Hz to MHz:
  ```python
  class _SdrStore(Store[dict[str, Any]]):
      """SDR desired-state store with a Hz->MHz center_frequency migration."""

      async def _async_migrate_func(
          self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
      ) -> dict[str, Any]:
          if old_major_version < 2:
              values = old_data.get("values")
              if isinstance(values, dict) and "center_frequency" in values:
                  hz = values["center_frequency"]
                  if isinstance(hz, (int, float)):
                      values["center_frequency"] = float(hz) / 1_000_000
          return old_data
  ```
  Then construct `self._store = _SdrStore(hass, SDR_STORE_VERSION, sdr_store_key(entry.entry_id))`. (HA's `Store.async_load` calls `_async_migrate_func` when the stored version is older than the constructed version and rewrites on next save; loading is enough for the in-memory value to be correct.)
- Keep `async_load_desired_state` unchanged — it reads `data.get("values", {})`, which is now post-migration (MHz).

**Verification:** run the existing coordinator/number/sensor tests; they may assert Hz values for center frequency and need updating — but assertion updates for *existing* tests belong to Task 4 (tests). If an existing test breaks purely due to the unit change, leave it for Task 4 and note it.
</details>
