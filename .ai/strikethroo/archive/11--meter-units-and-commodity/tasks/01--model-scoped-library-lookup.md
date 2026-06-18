---
id: 1
group: "meter-units"
dependencies: []
status: "completed"
created: 2026-05-28
skills:
  - python
complexity_score: 5
complexity_notes: "Loader + merge + lookup signature change across mapping.py with one call-site update in entity.py; cohesive but touches several loader functions."
---
# Component A — model-scoped device-library lookup

## Objective
Let the device library (and the user-override file) carry an optional top-level `models:` table (`model -> {field_key -> descriptor}`) and make `lookup` model-aware, so meters whose unit/scale/commodity is known from the `model` get a correct descriptor with no per-device config. Additive and backwards-compatible.

## Skills Required
- `python` — `mapping.py` loader/merge internals, `entity.py` call site.

## Acceptance Criteria
- [ ] Library YAML supports an optional top-level `models:` mapping (`model -> {field_key -> descriptor}`), descriptors using the SAME attribute schema as flat entries. Flat top-level keys remain the global default, unchanged.
- [ ] The loaded registry carries a model-scoped table alongside the flat `{field_key: FieldDescriptor}`; `DATA_LIBRARY` holds the once-loaded result.
- [ ] `_load_descriptor_file` intercepts the reserved `models:` key (does NOT parse it as a field named `models`); `merge_overrides` merges a `models:` block from the override file (override model-scoped entry replaces shipped for same `model`+`field_key`); `load_user_overrides` supports `models:`. Malformed model entries are logged and skipped.
- [ ] `lookup` becomes `lookup(field_key, model, registry)`: returns the model-scoped entry for `(model, field_key)` if present, else the global flat entry, else `None`. The default-registry fallback behavior is preserved.
- [ ] The sole production call site `entity.py` `_descriptor_for` passes the device `model` (already in scope) into `lookup`.
- [ ] Existing themed files + flat lookup behavior are unchanged (no regression for non-meter fields). `ruff check`/`ruff format --check` clean; all existing tests pass.

## Technical Requirements
- Files: `custom_components/rtl_433/mapping.py`, `custom_components/rtl_433/entity.py`.
- Reuse the `FieldDescriptor` frozen dataclass + `_descriptor_from_entry`; mirror the `skip_keys` reserved-key interception precedent in `merge_overrides`.

## Input Dependencies
None.

## Output Artifacts
- Model-aware `lookup` + registry shape, consumed by Component B (task 2), tests (task 3), docs (task 4).

## Implementation Notes
<details>
<summary>Detailed guidance</summary>
- Read `mapping.py`: `FieldDescriptor` (~:48-76), `_descriptor_from_entry` (~:79-102), `_load_descriptor_file` (~:128-145), `load_library` (~:180-228), `merge_overrides` (~:251-284), `lookup` (~:346-358), `load_user_overrides`. Confirm current line numbers (may have drifted).
- **Registry shape**: simplest is a small container or a tuple `(flat: dict, models: dict[str, dict])`. But `DATA_LIBRARY` and existing callers expect the current shape — check what `load_library` returns (the plan notes it returns `(registry, skip_keys)`) and extend consistently (e.g. registry becomes an object/namedtuple with `.flat` and `.models`, OR add a third return). Update ALL callers (`__init__.py` `_async_load_library`, entity platforms) to the new shape. Keep it minimal and readable.
- **Parsing**: in `_load_descriptor_file`, pop/intercept `models:` before the per-field loop; parse each `model -> {field_key -> descriptor}` via the existing `_descriptor_from_entry`. In `merge_overrides`, merge the models table (override replaces per `(model, field_key)`), preserving flat + skip_keys merge semantics.
- **lookup**: `def lookup(field_key, model, registry): return registry.models.get(model, {}).get(field_key) or registry.flat.get(field_key)` (adapt to chosen shape; keep default-registry fallback if `registry` omitted as today).
- **entity.py**: `_descriptor_for` (~:425-430) already has `model`; pass it. Update the call.
- This is Component A only — do NOT add calibration/flow/sensor changes (that's task 2).
- Verify: `uvx ruff check .`; `uvx ruff format --check .`; `python -m pytest tests/test_mapping.py tests/test_lifecycle.py -q` (existing tests must still pass — update any direct `lookup(...)` callers in tests minimally if the signature change breaks them).
</details>
