---
id: 1
group: "mapping-core"
dependencies: []
status: "completed"
created: 2026-05-28
skills:
  - python
---
# mapping.py: add validator + normalizer, remove load_user_overrides

## Objective
Extend `custom_components/rtl_433/mapping.py` with two new pure helpers — a strict `validate_user_mappings()` that returns per-entry problems for the UI, and a `normalize_overrides()` that produces a JSON-serialisable, payload-canonical object — and remove the now-unused file-reading `load_user_overrides()`.

## Skills Required
- `python`: pure-function dataclass/dict manipulation, YAML payload-key normalization.

## Acceptance Criteria
- [ ] `validate_user_mappings(data)` returns a list of human-readable per-entry problems (empty list == valid), without raising and without mutating input.
- [ ] `normalize_overrides(data)` returns a deep-copied, JSON-serialisable dict where binary `payload` keys are canonical string `"on"`/`"off"` (never Python `True`/`False`).
- [ ] `load_user_overrides()` is removed from `mapping.py`.
- [ ] `merge_overrides`, `_descriptor_from_entry`, `_normalize_payload`, `lookup`, `should_skip`, `load_library` keep their existing tolerant behaviour and signatures.
- [ ] `ruff check` and `ruff format --check` pass for `mapping.py`.

## Technical Requirements
- Reuse the existing descriptor model: required entry attrs are `platform`, `name`, `object_suffix`; supported platforms are those used by the shipped library (`sensor`, `binary_sensor`, `event`). Reserved top-level keys are `skip_keys` (a list) and `models` (a mapping of model -> {field_key -> entry}).
- The validator must mirror what `_descriptor_from_entry` would accept so the "validator accepts ⇒ merge keeps it" invariant holds. Do NOT make the runtime merge stricter.

## Input Dependencies
None.

## Output Artifacts
- `validate_user_mappings()` consumed by Task 4 (options flow).
- `normalize_overrides()` consumed by Task 3 (migration) and Task 4 (options flow, to canonicalise the editor object before storing).

## Implementation Notes
<details>
<summary>Detailed guidance</summary>

Read `custom_components/rtl_433/mapping.py` fully first. Relevant existing pieces: `FieldDescriptor`, `_DESCRIPTOR_ATTRS`, `_descriptor_from_entry`, `_normalize_payload`, `_parse_models_block`, `merge_overrides`, the constants `SKIP_KEYS_FIELD = "skip_keys"` and `MODELS_FIELD = "models"`.

**1. Supported platforms set.** Add a module constant, e.g.
`_SUPPORTED_PLATFORMS = frozenset({"sensor", "binary_sensor", "event"})`. (Confirm by grepping the `device_library/*.yaml` files for `platform:` values; include every value that actually appears.)

**2. `validate_user_mappings(data: Any) -> list[str]`.**
- If `data is None` → return `[]` (empty file/mapping is valid).
- If `data` is not a dict → return `["top-level mapping must be a YAML object"]`.
- Iterate items:
  - key == `SKIP_KEYS_FIELD`: value must be a `list`; else append `"skip_keys: must be a list"`.
  - key == `MODELS_FIELD`: value must be a dict; for each `model -> entries`, entries must be a dict; each entry validated with the same per-entry rule below, prefixing the problem with `models.<model>.<field_key>: ...`.
  - otherwise it's a flat field entry: validate with the per-entry rule, prefix `"<field_key>: ..."`.
- Per-entry rule (`_validate_entry(field_key, entry) -> list[str]`):
  - entry must be a dict, else `"must be a mapping"`.
  - for each required attr in (`platform`, `name`, `object_suffix`): missing/empty → `"missing required '<attr>'"`.
  - if `platform` present and not in `_SUPPORTED_PLATFORMS` → `"unknown platform '<value>' (expected one of sensor, binary_sensor, event)"`.
- Return the accumulated list. Each string should be self-contained and name the field (the options step joins them for the form error).

**3. `normalize_overrides(data: Any) -> dict[str, Any]`.**
- If not a dict → return `{}`.
- Build a new dict (deep copy of values via `copy.deepcopy`), then for any flat entry or `models.<model>.<field_key>` entry that has a `payload`, replace it with `_normalize_payload(payload)` so `True`/`False`/`"on"`/`"off"` keys become canonical string `"on"`/`"off"`. `_normalize_payload` already does exactly this — reuse it.
- Ensure the result is JSON-serialisable: after normalization there must be no non-string dict keys and no non-JSON scalar types. Because `_normalize_payload` stringifies keys, the main risk is boolean payload keys, which it handles. Add `import copy` at top if not present.
- Keep `skip_keys` (list) and `models` block structure intact.

**4. Remove `load_user_overrides()`** entirely (function + its references in the module docstring bullet list). Leave `merge_overrides`, `_copy_registry`, `_default_library` intact — Task 2 still uses `merge_overrides`; `_copy_registry` may become unused, in which case remove it too (and run ruff to confirm no F811/F401). NOTE: `__init__.py` currently imports `load_user_overrides`; that import is fixed in Task 2 (this task only owns mapping.py). Do not edit `__init__.py` here.

**5. Update the module docstring** bullet list to drop the `load_user_overrides` line and add lines for `validate_user_mappings` and `normalize_overrides`.

Run `ruff check custom_components/rtl_433/mapping.py` and `ruff format custom_components/rtl_433/mapping.py` before finishing.
</details>
