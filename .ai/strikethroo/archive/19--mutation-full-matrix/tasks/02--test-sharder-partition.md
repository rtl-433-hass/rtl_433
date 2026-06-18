---
id: 2
group: "sharding-helper"
dependencies: [1]
status: "completed"
created: "2026-06-02"
skills:
  - python
---
# Test the sharder partition is complete, disjoint, and balanced

## Objective
Add `tests/test_mutation_shards.py` to guard the highest-risk property of the new sharder: that for `--of 4`, the four shards form a complete, non-overlapping partition of every mutable module (so no module silently escapes the per-file floor), and that the partition is deterministic and reasonably balanced.

## Skills Required
- `python` — pytest, mirroring `tests/test_mutation_targets.py`.

## Acceptance Criteria
- [ ] `tests/test_mutation_shards.py` exists, loading `scripts/mutation_shards.py` as a standalone module via `importlib.util` (copy the `_load_*_module` + sandbox-skip pattern from `tests/test_mutation_targets.py`).
- [ ] Test: union of paths from shards `0..3` (with `--of 4`) equals the full set of mutable modules, AND no path appears in more than one shard (disjoint).
- [ ] Test: every file key in `scripts/mutation_baseline.json` appears in exactly one shard.
- [ ] Test: the partition is deterministic — calling the partition twice yields identical assignments.
- [ ] Test: each shard's emitted patterns correspond 1:1 to its paths (pattern derivation round-trips: `custom_components/rtl_433/coordinator/base.py` ↔ `custom_components.rtl_433.coordinator.base.*`).
- [ ] Tests pass: `uv run pytest tests/test_mutation_shards.py` (using the Python 3.14 / uv test stack).

## Technical Requirements
- Reuse the repo's test conventions: `_REPO_ROOT = Path(__file__).resolve().parents[1]`, the `importlib.util.spec_from_file_location` loader, and the module-level skip when `scripts/mutation_shards.py` is absent (mutmut sandbox).
- Prefer exercising the sharder through its public surface (call its partition/main function or shell out to it for each shard) rather than asserting an exact hard-coded assignment, so the tests stay valid as the baseline evolves. It is fine to call an internal partition helper if Task 1 exposes one; otherwise invoke the CLI for each shard and parse the two output lines.

## Meaningful Test Strategy Guidelines
Your critical mantra for test generation is: "write a few tests, mostly integration".

**Definition of "Meaningful Tests":** Tests that verify custom business logic, critical paths, and edge cases specific to the application. Focus on testing YOUR code, not the framework or library functionality.

**When TO Write Tests:** custom business logic and algorithms; critical workflows and data transformations; edge cases and error conditions for core functionality; integration points between components; complex validation or calculations.

**When NOT to Write Tests:** third-party library functionality; framework features; simple CRUD without custom logic; getters/setters; config or static data; obvious functionality that would break immediately if incorrect.

Here, the *partition completeness/disjointness* is exactly the custom-logic, coverage-critical property worth testing. Do NOT test mutmut's `walk_source_files`, argparse behavior, or trivial string formatting beyond the one round-trip check. Keep it to the handful of assertions above.

## Input Dependencies
- Task 1: `scripts/mutation_shards.py` and whatever internal partition helper / CLI contract it exposes.

## Output Artifacts
- `tests/test_mutation_shards.py`.

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

Skeleton, following `tests/test_mutation_targets.py`:
```python
import importlib.util
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "mutation_shards.py"

if not _SCRIPT.is_file():
    pytest.skip("scripts/mutation_shards.py absent (mutmut sandbox)", allow_module_level=True)

def _load():
    spec = importlib.util.spec_from_file_location("mutation_shards", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

ms = _load()
```
Collect each shard's paths (via the internal partition function if exposed, else by running the CLI with `subprocess` for `--shard i --of 4` and splitting line 2). Then:
- `all_paths = [p for shard in shards for p in shard]` → assert `len(all_paths) == len(set(all_paths))` (disjoint) and `set(all_paths) == expected_mutable_modules`.
- Build `expected_mutable_modules` the same way the script does (mutmut walk) so the test tracks reality, or read it from a helper the script exposes.
- Load `scripts/mutation_baseline.json`; assert every `files` key is in exactly one shard.
- Determinism: run the partition twice; assert equal.

Run with the Python 3.14 uv stack per the repo's testing setup (system python is 3.13; the test stack needs 3.14 via uv). Keep the file under ~80 lines — a few focused tests, not exhaustive coverage.
</details>
