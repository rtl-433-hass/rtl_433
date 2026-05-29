---
id: 2
group: "mapping-core"
dependencies: [1]
status: "completed"
created: 2026-05-28
skills:
  - python
  - home-assistant
---
# Per-entry override merge + consumer rewiring + reload-on-change

## Objective
Move the user-override merge from a single global cache to a **per-hub** merge: keep the shipped library globally cached, merge each hub's stored `entry.data[CONF_USER_MAPPINGS]` over it at setup, expose the per-entry `(registry, skip_keys)` to all four consumers, and reload the hub in place when the stored mappings change.

## Skills Required
- `python`, `home-assistant`: config-entry lifecycle, `hass.data` layout, executor jobs, update listeners.

## Acceptance Criteria
- [ ] New const `CONF_USER_MAPPINGS = "user_mappings"` in `const.py`; `DATA_LIBRARY` now documents/holds the **shipped-only** `(registry, skip_keys)`.
- [ ] `__init__.py` no longer imports or calls `load_user_overrides`; instead it merges `entry.data.get(CONF_USER_MAPPINGS)` via `merge_overrides` per entry and caches the merged `(registry, skip_keys)` per entry.
- [ ] Entity build (`entity.py`), the options-flow registry getter (`config_flow.py::_registry`), and diagnostics (`diagnostics.py`) read the **per-entry** merged registry/skip_keys, not the global `DATA_LIBRARY`.
- [ ] The coordinator receives the per-entry `skip_keys` (unchanged call shape, new source).
- [ ] `_async_update_listener` reloads the hub when the stored mappings differ from a setup-time snapshot (mirroring the calibration/manage-settings pattern); routine devices-map upserts do NOT trigger a reload.
- [ ] `ruff` passes; the integration imports cleanly (`python -c "import custom_components.rtl_433"` style check or existing import test).

## Technical Requirements
- Per-entry storage location: store the merged tuple where each consumer can reach it by entry. Recommended: `hass.data[DOMAIN][entry.entry_id]` already holds the coordinator object — instead introduce a sibling map, e.g. `hass.data[DOMAIN].setdefault(DATA_ENTRY_LIBRARY, {})[entry.entry_id] = (registry, skip_keys)` (add a `DATA_ENTRY_LIBRARY` const), and clean it up in `async_unload_entry`. Alternatively attach to the coordinator. Pick one and use it consistently across all consumers.
- The shipped-library global cache (`DATA_LIBRARY`) keeps being loaded once in the executor (`load_library`).

## Input Dependencies
- Task 1: `load_user_overrides` removed from `mapping.py`; `merge_overrides` remains.

## Output Artifacts
- Per-entry `(registry, skip_keys)` accessor used by Tasks 3, 4, 5.
- `CONF_USER_MAPPINGS` const used by Tasks 3, 4, 5.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

Read `__init__.py` (esp. `_async_load_library`, `async_setup_entry`, `_async_update_listener`, `async_unload_entry`, `_calibration_map`), `entity.py` around line 448, `config_flow.py::_registry` (~line 309), `diagnostics.py` (~line 68), and `const.py` (`DATA_LIBRARY`, `CONF_DEVICES`).

**const.py**
- Add `CONF_USER_MAPPINGS: Final = "user_mappings"` near the other `entry.data` sub-keys, with a comment that it holds the per-hub normalized override object.
- Add `DATA_ENTRY_LIBRARY: Final = "_entry_library"` near `DATA_LIBRARY`. Update the `DATA_LIBRARY` comment to say it now caches the **shipped library only** (no user overrides).

**__init__.py**
- Remove `load_user_overrides` from the `from .mapping import ...` line; keep `Registry, load_library, merge_overrides` (add `merge_overrides`).
- Change `_async_load_library` to load+cache ONLY the shipped library on `DATA_LIBRARY` (drop the `load_user_overrides` executor call). It returns the shipped `(registry, skip_keys)`.
- Add a helper `_merge_entry_library(hass, entry, shipped_registry, shipped_skip_keys) -> tuple[Registry, set[str]]` that:
  - reads `overrides = entry.data.get(CONF_USER_MAPPINGS) or {}`,
  - returns `merge_overrides(shipped_registry, shipped_skip_keys, overrides)` (pure, fast — no executor needed),
  - on any unexpected error logs a warning and returns a copy of the shipped inputs (never crash setup).
- In `async_setup_entry`: after `_registry, skip_keys = await _async_load_library(hass)`, compute `entry_registry, entry_skip_keys = _merge_entry_library(...)`, store them in `hass.data[DOMAIN][DATA_ENTRY_LIBRARY][entry.entry_id]`, pass `entry_skip_keys` to the coordinator (replace the `skip_keys=skip_keys` arg), and snapshot the mappings for change-detection: `coordinator.user_mappings_snapshot = entry.data.get(CONF_USER_MAPPINGS) or {}`.
- In `_async_update_listener`: add, alongside the existing manage-settings/calibration checks, a comparison `if (entry.data.get(CONF_USER_MAPPINGS) or {}) != coordinator.user_mappings_snapshot: await hass.config_entries.async_reload(entry.entry_id); return`.
- In `async_unload_entry`: pop `hass.data[DOMAIN][DATA_ENTRY_LIBRARY].pop(entry.entry_id, None)`.

**entity.py (~448)**: replace `registry = hass.data[DOMAIN].get(DATA_LIBRARY, (None, None))[0]` with a read from the per-entry map for the entry being set up: `hass.data[DOMAIN].get(DATA_ENTRY_LIBRARY, {}).get(entry.entry_id, (None, None))[0]`. The platform setup has the `ConfigEntry`; thread `entry.entry_id` in. Keep the `lookup(field_key, model, registry)` calls.

**config_flow.py `_registry`**: change to read the per-entry library for `self.config_entry.entry_id` from `DATA_ENTRY_LIBRARY` instead of `DATA_LIBRARY`. Update the docstring.

**diagnostics.py (~68)**: change `registry, skip_keys = domain_data.get(DATA_LIBRARY, (None, set()))` to read from `DATA_ENTRY_LIBRARY` keyed by the entry being diagnosed (the diagnostics function receives the entry). Keep the rest.

**Coordinator**: it already accepts `skip_keys`; just pass the per-entry value. Add a plain attribute `user_mappings_snapshot` on the coordinator (set in setup) — if the coordinator class needs the attribute declared, add it next to `calibration_snapshot`.

Verify: grep for any remaining `DATA_LIBRARY` reads that should have become per-entry (only `_async_load_library` should still touch `DATA_LIBRARY`). Run `ruff check` + `ruff format` on every edited file.
</details>
