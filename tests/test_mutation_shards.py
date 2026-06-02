"""Tests for ``scripts/mutation_shards.py`` — the nightly mutation sharder.

The nightly matrix splits a full mutmut run across N parallel jobs, each
mutating a disjoint subset of modules. The coverage-critical property is that
the shards form a *complete, non-overlapping* partition of every mutable
module: a module dropped from the union (or duplicated across shards) would
silently escape the per-file mutation floor, so these tests guard that
invariant — plus determinism, the path<->pattern derivation, and that
``--restrict`` (used to fan a scoped PR run across the same shards) still
covers exactly the in-scope set — without re-testing mutmut's walk or argparse.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "mutation_shards.py"

# mutmut copies only the package, tests/, and pyproject into its ``mutants/``
# sandbox — not scripts/ — so this meta-test cannot load the script there. It
# adds no mutation coverage anyway, so skip the module in that environment; the
# normal pytest job (where scripts/ exists) runs it in full.
if not _SCRIPT.is_file():
    pytest.skip(
        "scripts/mutation_shards.py absent (mutmut sandbox); this meta-test "
        "runs in the normal pytest job only",
        allow_module_level=True,
    )


def _load_shards_module():
    """Load the standalone script (it lives in ``scripts/``, not a package)."""
    spec = importlib.util.spec_from_file_location("mutation_shards", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ms = _load_shards_module()


def test_partition_is_complete_and_disjoint():
    shards = ms.partition(8)
    all_paths = [p for shard in shards for p in shard]
    assert len(all_paths) == len(set(all_paths)), "a path appears in >1 shard"
    assert set(all_paths) == set(ms.mutable_modules())


def test_every_baseline_file_lands_in_exactly_one_shard():
    data = json.loads((_REPO_ROOT / "scripts" / "mutation_baseline.json").read_text())
    shards = ms.partition(8)
    for path in data["files"]:
        hits = sum(path in shard for shard in shards)
        assert hits == 1, f"{path} is in {hits} shards, expected 1"


def test_partition_is_deterministic():
    assert ms.partition(8) == ms.partition(8)


def test_pattern_round_trip():
    path = "custom_components/rtl_433/coordinator/base.py"
    assert ms.patterns_for([path]) == ["custom_components.rtl_433.coordinator.base.*"]
    assert ms.patterns_for(["custom_components/rtl_433/sensor.py"]) == [
        "custom_components.rtl_433.sensor.*"
    ]


def test_package_init_patterns_target_root_mutants():
    """``__init__.py`` mutants live under the package name with no module segment.

    mutmut strips the ``__init__`` segment from mutant names, so the naive
    ``<pkg>.__init__.*`` filter matches *nothing* (every package-root mutant goes
    unrun and scores 0). They must be matched via the ``x_``/``xǁ`` trampoline
    prefixes instead, which no submodule name shares.
    """
    assert ms.patterns_for(["custom_components/rtl_433/__init__.py"]) == [
        "custom_components.rtl_433.x_*",
        "custom_components.rtl_433.xǁ*",
    ]
    # A nested package __init__ keeps its package path but still drops __init__.
    assert ms.patterns_for(["custom_components/rtl_433/coordinator/__init__.py"]) == [
        "custom_components.rtl_433.coordinator.x_*",
        "custom_components.rtl_433.coordinator.xǁ*",
    ]


def test_resolve_weights_prefers_timing_then_count_fallback():
    """A module's weight is its measured time; an untimed module is estimated as
    ``count * avg_seconds_per_mutant`` derived from the timed modules — so the
    bin-packer balances by *time*, not raw mutant count (the source of the slow
    pole when ``entity.py`` ~ 2.5x slower per mutant landed by count)."""
    modules = ["a.py", "b.py", "c.py"]
    # a: 100s/100 mutants and b: 300s/100 mutants -> avg 2 s/mutant. c has no
    # timing, 50 mutants -> estimated 100s. (Count alone would tie a and c.)
    timings = {"a.py": 100.0, "b.py": 300.0}
    counts = {"a.py": 100, "b.py": 100, "c.py": 50}
    weights = ms.resolve_weights(modules, timings=timings, counts=counts)
    assert weights["a.py"] == 100.0  # measured time used directly
    assert weights["b.py"] == 300.0
    assert weights["c.py"] == 100.0  # 50 mutants * (400s / 200 mutants) = 100s
    # b is the heaviest by time even though a and b tie on mutant count.
    assert max(weights, key=weights.get) == "b.py"


def _shard_paths(capsys, *argv):
    """Run main() for one shard and return its emitted source paths (line 2)."""
    assert ms.main(list(argv)) == 0
    lines = capsys.readouterr().out.splitlines()
    return lines[1].split() if len(lines) > 1 and lines[1] else []


def test_restrict_covers_exactly_the_in_scope_set(capsys):
    """Union of (shard ∩ restrict) over all shards == restrict, and is disjoint.

    This is what lets a scoped PR run fan its changed modules across the same
    4 shards without dropping or double-counting any in-scope module.
    """
    restrict = [
        "custom_components/rtl_433/coordinator/base.py",
        "custom_components/rtl_433/number.py",
        "custom_components/rtl_433/const.py",
    ]
    collected = []
    for shard in range(4):
        collected += _shard_paths(
            capsys, "--shard", str(shard), "--of", "4", "--restrict", *restrict
        )
    assert sorted(collected) == sorted(restrict)
    assert len(collected) == len(set(collected)), "an in-scope path appears in >1 shard"


def test_restrict_with_nothing_in_scope_is_empty(capsys):
    """An empty --restrict (docs-only PR) yields no work in any shard."""
    for shard in range(4):
        assert (
            _shard_paths(capsys, "--shard", str(shard), "--of", "4", "--restrict") == []
        )
