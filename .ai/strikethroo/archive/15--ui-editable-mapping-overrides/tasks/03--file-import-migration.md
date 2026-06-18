---
id: 3
group: "migration"
dependencies: [1, 2]
status: "completed"
created: 2026-05-28
skills:
  - python
  - home-assistant
---
# One-time legacy-file import migration + minor version bump

## Objective
On first setup after upgrade, seed every existing hub's `entry.data[CONF_USER_MAPPINGS]` from any existing `<config>/rtl_433_mappings.yaml` (normalized JSON-safe), then never read the file again. New hubs added later start empty.

## Skills Required
- `python`, `home-assistant`: `async_migrate_entry`, config-entry version/minor-version, executor file I/O.

## Acceptance Criteria
- [ ] Config flow `MINOR_VERSION` is introduced/bumped (e.g. `VERSION = 2`, `MINOR_VERSION = 2`) so the migration runs once for pre-existing entries.
- [ ] `async_migrate_entry(hass, entry)` reads `<config>/rtl_433_mappings.yaml` in the executor, parses with PyYAML, runs it through `normalize_overrides` (Task 1), and writes the result into `entry.data[CONF_USER_MAPPINGS]` via `async_update_entry`, bumping the entry's minor version.
- [ ] Every existing hub entry is migrated (each gets its own copy of the file contents).
- [ ] Missing/empty/invalid file → entry migrates to `{}` (empty mappings), never raising.
- [ ] The file is NOT modified or deleted.
- [ ] `ruff` passes.

## Technical Requirements
- HA calls `async_migrate_entry` when the stored entry version/minor-version is lower than the class `VERSION`/`MINOR_VERSION`. Each entry is migrated independently, which naturally gives the "seed every hub" behaviour.
- Reuse `USER_OVERRIDE_FILENAME` from `mapping.py` for the filename; read via `hass.config.path(USER_OVERRIDE_FILENAME)`.

## Input Dependencies
- Task 1: `normalize_overrides`, `USER_OVERRIDE_FILENAME`.
- Task 2: `CONF_USER_MAPPINGS` const and the per-entry merge that consumes the stored object.

## Output Artifacts
- Migrated `entry.data[CONF_USER_MAPPINGS]` consumed by the per-entry merge (Task 2) and the options step (Task 4). Verified by Task 5 tests.

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

Read `config_flow.py` (`Rtl433ConfigFlow.VERSION = 2`) and `__init__.py` setup/migration area.

**Version bump:** in `config_flow.py`, on `Rtl433ConfigFlow` add `MINOR_VERSION = 2` (keep `VERSION = 2`). HA treats an existing entry created at VERSION 2 with no minor version as minor 1, so bumping to minor 2 triggers migration once.

**`async_migrate_entry` in `__init__.py`:**
```
async def async_migrate_entry(hass, entry) -> bool:
    if entry.version > 2:  # future-proof guard
        return False
    if entry.version == 2 and (entry.minor_version or 1) >= 2:
        return True
    # read + parse + normalize the legacy file in the executor
    overrides = await hass.async_add_executor_job(_read_legacy_overrides, hass.config.path(USER_OVERRIDE_FILENAME))
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_USER_MAPPINGS: overrides},
        minor_version=2,
        version=2,
    )
    return True
```
(Match the exact attribute names HA exposes: `entry.version`, `entry.minor_version`. Confirm against the installed HA version in `.venv`.)

**`_read_legacy_overrides(path: str) -> dict` (sync, executor):**
- if file missing → return `{}`.
- open + `yaml.safe_load`; on `OSError`/`yaml.YAMLError` → log warning, return `{}`.
- if parsed is None or not a dict → return `{}`.
- return `normalize_overrides(parsed)` (import from `.mapping`).

Add `from .mapping import normalize_overrides` and `USER_OVERRIDE_FILENAME` to the imports (and `import yaml` if not already in `__init__.py` — it likely is not; keep the YAML read inside the small helper).

**Do not** delete or rewrite the file. **Do not** read the file anywhere in `async_setup_entry` (the merge only reads `entry.data`). A brand-new hub (added post-upgrade) is created at the current `MINOR_VERSION`, so `async_migrate_entry` does not run for it and it starts with no `CONF_USER_MAPPINGS` key → empty mappings. Good.

Register `async_migrate_entry` — HA discovers it by name at module level in `__init__.py`; ensure it is a module-level coroutine (not nested).

Run `ruff check` + `ruff format` on edited files.
</details>
