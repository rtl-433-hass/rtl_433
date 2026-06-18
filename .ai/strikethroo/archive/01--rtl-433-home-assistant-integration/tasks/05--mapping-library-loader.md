---
id: 5
group: "core"
dependencies: [3, 4]
status: "completed"
created: 2026-05-25
skills:
  - python
  - home-assistant
---
# Mapping Library Loader (+ user override)

## Objective
Implement the thin Python loader that parses and validates the themed YAML device-library files (Task 4) into in-memory entity descriptors, exposes lookup by rtl_433 field name, excludes skip-keys, and layers an optional user-supplied override YAML from the Home Assistant config directory on top of the shipped library.

## Skills Required
- `python` — YAML parsing, dataclasses, validation
- `home-assistant` — config-dir access, descriptor shapes aligned to HA entity attributes

## Acceptance Criteria
- [ ] `custom_components/rtl_433/mapping.py` loads all `device_library/*.yaml` files into a registry of descriptors keyed by field name.
- [ ] A `FieldDescriptor` dataclass (or TypedDict) captures: `field_key`, `platform`, `device_class`, `unit_of_measurement`, `state_class`, `name`, `value_transform`, `payload` (binary), `object_suffix`.
- [ ] `SKIP_KEYS` is loaded from `_skip_keys.yaml`; a `is_skip_key(key)` / `should_skip(key)` helper exists.
- [ ] A `lookup(field_key) -> FieldDescriptor | None` function returns the descriptor or `None` for unmapped fields.
- [ ] An `apply_transform(descriptor, raw_value)` helper applies declarative `value_transform` (round/scale) and binary `payload` mapping, returning the HA-facing state.
- [ ] A user-override file at `<hass.config.path>("rtl_433_mappings.yaml")` is loaded if present and layered over the shipped library (override or add entries), without crashing if absent or malformed (log a warning).
- [ ] Loading is resilient: a malformed individual YAML file logs an error and is skipped rather than crashing the import.
- [ ] `ruff check` passes; the module imports without HA running (the YAML parse must not require a live `hass`, but the override load takes `hass`/config path as an argument).
- [ ] A single conventional commit (e.g. `feat: add device-library YAML loader`).

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
- Use `PyYAML` (`yaml.safe_load`) — already available in the HA environment, no new requirement.
- The shipped library files are packaged under `custom_components/rtl_433/device_library/`; resolve their path relative to `__file__` (use `pathlib`).
- The override load is a separate function that accepts the HA config path so it can be called during setup (Task 9) and is unit-testable with a temp dir (Task 10).
- File I/O for the shipped library should be done off the event loop when called from async code; provide a sync `load_library()` plus an async wrapper or document that the caller must `hass.async_add_executor_job`.

## Input Dependencies
- Task 3: `const.py` (DOMAIN, any shared keys), package skeleton.
- Task 4: the YAML schema and `device_library/*.yaml` + `_skip_keys.yaml` files (defines the exact attribute names this loader must read).

## Output Artifacts
- `mapping.py` with `load_library()`, `lookup()`, `should_skip()`, `apply_transform()`, and `load_user_overrides()`. Consumed by entities (Task 8) and tested directly (Task 10).

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

1. Read the exact YAML attribute names produced by Task 4 (`platform`, `device_class`, `unit_of_measurement`, `state_class`, `name`, `value_transform`, `payload`, `object_suffix`). Conform to them precisely.
2. `FieldDescriptor` as a frozen dataclass. Registry: `dict[str, FieldDescriptor]`.
3. `load_library()`:
   - Glob `Path(__file__).parent / "device_library" / "*.yaml"`, skip `_skip_keys.yaml` for descriptor parsing (load it separately).
   - For each file, `yaml.safe_load`; each top-level key is a field name → attributes. Wrap per-file parsing in try/except logging the filename on error.
   - Build and return `(descriptors, skip_keys)`.
4. `apply_transform`:
   - For sensors: if `value_transform` has `round`, `round(float(value), n)`; `scale`, multiply. Be defensive about non-numeric values (return raw on failure).
   - For binary: use `payload` mapping (`{on: X, off: Y}`) to map the raw value to `True`/`False`. Handle the `battery_ok` inversion encoded in the YAML.
5. `load_user_overrides(config_path)`:
   - Path = `config_path / "rtl_433_mappings.yaml"` (caller passes `hass.config.path("rtl_433_mappings.yaml")`).
   - If present, parse and merge into the registry (override existing keys, add new). Catch and log exceptions; never raise to the caller.
6. Keep it dependency-free beyond stdlib + `yaml`. Add module-level `LOGGER`.
7. Only create/modify `custom_components/rtl_433/mapping.py`. Do not touch `normalizer.py`/`coordinator/` (Task 6) to preserve file-disjoint parallelism within Phase 2.
8. `ruff check custom_components/rtl_433/mapping.py`; commit `feat:`.
</details>
