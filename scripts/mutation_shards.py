#!/usr/bin/env python3
"""Deterministically partition the integration's modules into N balanced shards.

A full-package mutmut run is slow (~50 min). The matrix workflow splits that work
across N parallel jobs, each mutating a disjoint subset of *modules* (a module is
the smallest unit mutmut can filter to without losing fidelity to a full run).

For the split to be useful the shards must be *balanced* (so the slowest job,
which the gate waits on, is as short as possible) and *deterministic* (so every
matrix job computes the identical assignment from the same inputs, with no
coordination).

**Balance by time, not mutant count.** Per-mutant test time varies widely across
modules (``entity.py`` is ~2.5x slower per mutant than ``coordinator/base.py``),
so a count-balanced split still leaves a slow pole. Each module is therefore
weighted by its measured mutmut run time from ``scripts/mutation_timings.json``
(see scripts/mutation_timings.py). A module absent from that profile (a newly
added file, or a stale profile) falls back to ``mutant_count * avg_seconds_per_
mutant`` so it is still placed sensibly. Modules are sorted heaviest first (ties
by path), then each is placed into the currently-lightest bin (ties by lowest
index) — the classic LPT heuristic: within 4/3 of optimal makespan and, with
fixed sort/tie-break keys, fully reproducible (no randomness, no wall-clock).

**Output contract (two lines):**
    line 1: space-separated mutmut filter patterns for the requested shard
    line 2: space-separated source paths for the requested shard
Both lines are empty when the shard received no modules.

Run from the repository root, e.g. for an 8-way split, the first shard:
    python scripts/mutation_shards.py --shard 0 --of 8

``--restrict <path>...`` narrows the output to the intersection of the shard and
the given source paths, without changing the global assignment. This lets a
*scoped* run (a PR mutating only its changed modules) reuse the same shards as a
full run: the partition is still computed over every module (so each module keeps
its stable shard), but only the in-scope modules that fall in this shard are
emitted. The union across all shards of ``shard ∩ restrict`` equals ``restrict``,
so coverage of the scoped set stays complete and disjoint. A shard whose
intersection is empty emits two blank lines (and the caller skips it).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import mutmut
from mutmut.__main__ import ensure_config_loaded, walk_source_files

PKG = "custom_components/rtl_433"
PKG_DOTTED = "custom_components.rtl_433"

DEFAULT_BASELINE = Path(__file__).with_name("mutation_baseline.json")
DEFAULT_TIMINGS = Path(__file__).with_name("mutation_timings.json")

# Fallback seconds-per-mutant when neither a timing nor any profile exists at all
# (e.g. a fresh checkout with no committed timings). Only used to keep weights
# positive; the relative split is what matters.
FALLBACK_SECONDS_PER_MUTANT = 1.0


def mutable_modules() -> list[str]:
    """Enumerate mutable source modules exactly as mutmut would, as repo paths."""
    ensure_config_loaded()
    return [
        str(path)
        for path in walk_source_files()
        if not mutmut.config.should_ignore_for_mutation(path)
    ]


def load_counts(baseline: Path = DEFAULT_BASELINE) -> dict[str, int]:
    """Map module path -> baseline mutant ``total`` (for the count-based fallback)."""
    if not baseline.exists():
        return {}
    data = json.loads(baseline.read_text(encoding="utf-8"))
    return {
        path: int(stats.get("total", 0))
        for path, stats in data.get("files", {}).items()
    }


def load_timings(timings: Path = DEFAULT_TIMINGS) -> dict[str, float]:
    """Map module path -> measured mutmut seconds (the bin-pack weight)."""
    if not timings.exists():
        return {}
    data = json.loads(timings.read_text(encoding="utf-8"))
    return {path: float(secs) for path, secs in data.get("files", {}).items()}


def resolve_weights(
    modules: list[str] | None = None,
    timings: dict[str, float] | None = None,
    counts: dict[str, int] | None = None,
) -> dict[str, float]:
    """Per-module bin-pack weight in seconds: measured time, else a count estimate.

    A module with a measured timing uses it directly. A module without one (new
    file, or stale profile) is estimated as ``mutant_count * avg_seconds_per_
    mutant``, where the average is derived from the modules that *do* have both a
    timing and a count, so the estimate is in the same units as the real weights.
    """
    if modules is None:
        modules = mutable_modules()
    if timings is None:
        timings = load_timings()
    if counts is None:
        counts = load_counts()

    paired = [(timings[m], counts[m]) for m in timings if counts.get(m)]
    total_secs = sum(t for t, _ in paired)
    total_mutants = sum(c for _, c in paired)
    per_mutant = (
        total_secs / total_mutants if total_mutants else FALLBACK_SECONDS_PER_MUTANT
    )

    def weight(module: str) -> float:
        if module in timings:
            return timings[module]
        # Estimate from mutant count (>=1 so a module is never weightless).
        return max(counts.get(module, 1), 1) * per_mutant

    return {module: weight(module) for module in modules}


def partition(
    n: int,
    modules: list[str] | None = None,
    weights: dict[str, float] | None = None,
) -> list[list[str]]:
    """Partition modules into ``n`` balanced bins via deterministic LPT greedy.

    Returns a list of ``n`` lists of source paths (each inner list sorted). The
    partition is a pure function of (modules, weights, n): every module appears
    in exactly one bin and the union equals the full module set. Weights default
    to :func:`resolve_weights` (measured time with a count-based fallback).
    """
    if modules is None:
        modules = mutable_modules()
    if weights is None:
        weights = resolve_weights(modules)

    def weight(path: str) -> float:
        return weights.get(path, 0.0)

    # Heaviest first; stable tie-break by path so the order is reproducible.
    order = sorted(modules, key=lambda p: (-weight(p), p))
    bins: list[list[str]] = [[] for _ in range(n)]
    loads = [0.0] * n
    for path in order:
        # Place in the currently-lightest bin; lowest index wins on a tie.
        j = min(range(n), key=lambda k: (loads[k], k))
        bins[j].append(path)
        loads[j] += weight(path)
    return [sorted(b) for b in bins]


def patterns_for(paths: list[str]) -> list[str]:
    """Derive mutmut dotted filter patterns from source paths.

    A normal module ``a/b.py`` matches ``custom_components.rtl_433.a.b.*``.

    A package ``__init__.py`` is special: mutmut strips the ``__init__`` segment
    from its mutant names (``get_mutant_name`` does ``.replace('.__init__.', '.')``),
    so the package-root mutants live directly under the package's dotted name —
    e.g. ``custom_components.rtl_433.x_async_setup_entry__mutmut_1``. A bare
    ``custom_components.rtl_433.*`` would also match every *submodule*, so instead
    we match only the mutant trampolines, which mutmut names ``x_*`` (functions)
    and ``xǁ*`` (class methods); no module name starts with those, so this matches
    exactly the package-root mutants and nothing else.
    """
    patterns: list[str] = []
    for p in paths:
        dotted = f"{PKG_DOTTED}.{p[len(PKG) + 1 : -3].replace('/', '.')}"
        if dotted.endswith(".__init__"):
            base = dotted[: -len(".__init__")]
            patterns += [f"{base}.x_*", f"{base}.xǁ*"]
        else:
            patterns.append(f"{dotted}.*")
    return patterns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--shard", type=int, required=True, help="shard index (0-based)"
    )
    parser.add_argument("--of", type=int, required=True, help="total number of shards")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--timings", type=Path, default=DEFAULT_TIMINGS)
    parser.add_argument(
        "--restrict",
        nargs="*",
        default=None,
        metavar="PATH",
        help="emit only the intersection of this shard with these source paths "
        "(for a scoped run); the global assignment is unchanged",
    )
    args = parser.parse_args(argv)

    if args.of < 1:
        print(f"ERROR: --of must be >= 1, got {args.of}", file=sys.stderr)
        return 2
    if not (0 <= args.shard < args.of):
        print(
            f"ERROR: --shard must satisfy 0 <= shard < {args.of}, got {args.shard}",
            file=sys.stderr,
        )
        return 2

    modules = mutable_modules()
    weights = resolve_weights(
        modules,
        timings=load_timings(args.timings),
        counts=load_counts(args.baseline),
    )
    bins = partition(args.of, modules, weights)
    paths = bins[args.shard]
    if args.restrict is not None:
        restrict = {str(Path(p)) for p in args.restrict}
        paths = [p for p in paths if p in restrict]
    print(" ".join(patterns_for(paths)))
    print(" ".join(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
