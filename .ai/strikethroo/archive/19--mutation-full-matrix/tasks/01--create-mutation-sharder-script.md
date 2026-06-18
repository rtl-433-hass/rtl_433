---
id: 1
group: "sharding-helper"
dependencies: []
status: "completed"
created: "2026-06-02"
skills:
  - python
---
# Create the deterministic mutation sharder script

## Objective
Add `scripts/mutation_shards.py`: a standalone helper that deterministically partitions all mutable source modules of the integration into N balanced shards (by mutant count), and for a given shard index prints the mutmut filter patterns and source paths that shard owns. This is the data source the matrix workflow (Task 3) consumes.

## Skills Required
- `python` — standalone CLI script using mutmut's API and the baseline JSON, mirroring the existing `scripts/mutation_*.py` style.

## Acceptance Criteria
- [ ] `scripts/mutation_shards.py` exists with a module docstring explaining the bin-packing strategy and the two-line output contract (matching the docstring style of `scripts/mutation_targets.py`).
- [ ] Accepts `--shard <i>` and `--of <N>` arguments (integers, `0 <= i < N`, `N >= 1`).
- [ ] Enumerates mutable modules exactly as mutmut would (via mutmut's `walk_source_files` + `should_ignore_for_mutation`, the same approach `scripts/mutation_stats.py` uses), so no static module list can drift.
- [ ] Weights each module by its `total` from `scripts/mutation_baseline.json`; a module absent from the baseline gets a default weight (so new files are still placed, never dropped).
- [ ] Partitions modules into N bins with a deterministic longest-processing-time-first greedy bin-pack (sort by descending weight then by path for stable ties; place each module in the currently-lightest bin, breaking ties by lowest bin index).
- [ ] For `--shard i --of N`, prints exactly two lines: line 1 = space-separated mutmut dotted filter patterns for that shard (e.g. `custom_components.rtl_433.coordinator.base.*`), line 2 = space-separated source paths for that shard (e.g. `custom_components/rtl_433/coordinator/base.py`). Both lines empty if the shard got no modules.
- [ ] The partition is a pure function of (baseline, on-disk modules, N): every module appears in exactly one shard across `i = 0..N-1`, and the union equals the full mutable module set.
- [ ] Running `python scripts/mutation_shards.py --shard 0 --of 4` from the repo root succeeds and emits a valid pattern/path pair.

## Technical Requirements
- Pattern derivation must match `scripts/mutation_targets.py`: a path `custom_components/rtl_433/<sub>/<mod>.py` becomes the dotted pattern `custom_components.rtl_433.<sub>.<mod>.*` (strip the `custom_components/rtl_433/` prefix and `.py` suffix, replace `/` with `.`, append `.*`). Use the constants `PKG = "custom_components/rtl_433"` and `PKG_DOTTED = "custom_components.rtl_433"` as that script does.
- Use `argparse`. Validate that `0 <= shard < of`; exit non-zero with a clear stderr message otherwise.
- The default baseline path is `scripts/mutation_baseline.json` (resolve relative to the script file, like `mutation_ratchet.py`'s `DEFAULT_BASELINE = Path(__file__).with_name("mutation_baseline.json")`).

## Input Dependencies
None. Reads existing committed files (`scripts/mutation_baseline.json`) and uses the already-installed `mutmut` package.

## Output Artifacts
- `scripts/mutation_shards.py` — consumed by Task 2 (tests) and Task 3 (workflow).

## Implementation Notes
<details>
<summary>Detailed implementation guidance</summary>

**Loading mutmut's module list (copy the pattern from `scripts/mutation_stats.py`):**
```python
import mutmut
from mutmut.__main__ import ensure_config_loaded, walk_source_files

ensure_config_loaded()
modules = [
    str(path)
    for path in walk_source_files()
    if not mutmut.config.should_ignore_for_mutation(path)
]
```
This yields paths like `custom_components/rtl_433/coordinator/base.py`. Filter to `.py` files under `PKG` if `walk_source_files` returns anything outside the package (it should not, given `paths_to_mutate`).

**Weights:** load `scripts/mutation_baseline.json`, read `files[path]["total"]`. For a module not present, use a default weight — use the median (or simply `1`) so it still gets placed; document the choice in a comment. New files are rare and the next nightly run after a baseline refresh rebalances anyway.

**Bin-pack (LPT greedy):**
```python
order = sorted(modules, key=lambda p: (-weight[p], p))   # heavy first, stable tie-break
bins = [[] for _ in range(n)]
loads = [0] * n
for path in order:
    j = min(range(n), key=lambda k: (loads[k], k))        # lightest bin, lowest index on tie
    bins[j].append(path)
    loads[j] += weight[path]
```
Then `paths = sorted(bins[shard])` and derive patterns from those paths.

**Output (two lines, matching `mutation_targets.py` lines 2–3 convention so the workflow parses identically):**
```python
print(" ".join(patterns))
print(" ".join(paths))
```

**Sandbox guard:** like `tests/test_mutation_targets.py` notes, mutmut copies only the package + tests/ + pyproject into `mutants/`, not `scripts/`. This script is only ever invoked from the repo root in CI before `mutmut run`, so no special handling is needed inside the script itself — just do not import it from package source.

**Determinism:** do NOT use any randomness or wall-clock. The sort keys above make the partition fully reproducible so every matrix shard computes the identical assignment.

Reference the existing scripts for tone and structure: `scripts/mutation_targets.py` (pattern derivation, two-line output), `scripts/mutation_stats.py` (mutmut walk + config), `scripts/mutation_ratchet.py` (baseline path, argparse).
</details>
