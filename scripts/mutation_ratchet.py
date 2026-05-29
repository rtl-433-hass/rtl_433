#!/usr/bin/env python3
"""Mutation-score ratchet for the rtl_433 integration.

Compares the current mutmut results (per-file stats from ``scripts/mutation_stats.py``)
against a committed per-file baseline and enforces that mutation coverage never
meaningfully regresses.

A per-file comparison needs a tolerance band for two reasons:

1. **Run-to-run variance.** mutmut's score is not perfectly reproducible — the
   async coordinator and time-sensitive paths flip a mutant or two between runs
   and between machines (locally vs CI).
2. **Scoped vs full divergence.** On pull requests CI mutates only the changed
   modules (``mutmut run "<module>.*"``), which is a slight *lower bound* on a
   file's full-suite score: a few mutants are killed only by tests in other files
   that a scoped run doesn't exercise. Observed example: ``number.py`` scores
   27/29 scoped but 29/29 in the full baseline — a 2-mutant (≈7%) gap on a small
   file.

Both effects are measured in **mutants**, not percentage points, so a flat
percentage tolerance is wrong: 2% is ~13 mutants on the 630-mutant coordinator
but 0 mutants on a 29-mutant file. The band is therefore
``max(fraction × total, absolute_mutants)`` converted back to score space — an
absolute-mutant cushion that protects small files, plus a fraction that scales for
large ones. A real regression (deleting a test typically kills many more mutants
than the band) still fails; a sub-band drop on a small file passes the PR gate and
is re-measured authoritatively by the nightly full run.

The bar only ratchets **upward** — genuine improvements are captured with
``--update``. Equivalent/unkillable mutants are recorded in the baseline rather
than suppressed: nothing is ignored, the score just cannot fall.

Two modes:

* ``floor`` (the CI gate): fail if any file's score is below its tolerance band.
  Improvements never fail.
* ``strict`` (local check that the committed baseline is still representative):
  fail if any file drifts beyond the band in either direction.

Usage:
    mutmut run
    python scripts/mutation_stats.py > stats.json
    python scripts/mutation_ratchet.py --mode floor  --stats stats.json
    python scripts/mutation_ratchet.py --mode strict --stats stats.json
    python scripts/mutation_ratchet.py --mode floor  --stats stats.json --update
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Per-file scores are rounded to this many decimals before comparison.
PRECISION = 6
# Tolerance band = max(TOLERANCE_FRACTION * total, TOLERANCE_MUTANTS) mutants.
# - TOLERANCE_MUTANTS (absolute) covers the scoped-vs-full lower-bound gap and
#   run-to-run noise on SMALL files (observed worst case: 2 mutants on number.py).
# - TOLERANCE_FRACTION scales the band for LARGE files (e.g. ~13 on the 630-mutant
#   coordinator), absorbing its proportionally larger run-to-run drift.
TOLERANCE_FRACTION = 0.02
TOLERANCE_MUTANTS = 3
DEFAULT_BASELINE = Path(__file__).with_name("mutation_baseline.json")


def score_for(file_stats: dict) -> tuple[int, int, float]:
    """Return (killed, scoreable_total, score) for one file's mutmut stats."""
    killed = file_stats.get("killed", 0) + file_stats.get("timeout", 0)
    total = file_stats.get("total", 0) - file_stats.get("skipped", 0)
    score = 1.0 if total <= 0 else killed / total
    return killed, total, round(score, PRECISION)


def scores_from_stats(stats: dict) -> dict[str, dict]:
    """Reduce a mutmut stats payload to per-file {killed,total,score}."""
    out: dict[str, dict] = {}
    for path, fstats in stats.get("files", {}).items():
        killed, total, score = score_for(fstats)
        out[path] = {"killed": killed, "total": total, "score": score}
    return dict(sorted(out.items()))


def tolerance_score(total: int, fraction: float, mutants: int) -> float:
    """Tolerance band, in score space, for a file with ``total`` scoreable mutants."""
    if total <= 0:
        return 0.0
    band_mutants = max(fraction * total, mutants)
    return round(band_mutants / total, PRECISION)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_baseline(
    path: Path, scores: dict[str, dict], fraction: float, mutants: int
) -> None:
    payload = {
        "floor": 0.70,
        "tolerance_fraction": fraction,
        "tolerance_mutants": mutants,
        "files": scores,
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def check_floor(
    current: dict[str, dict], baseline: dict[str, dict], fraction: float, mutants: int
) -> list[str]:
    """Fail when a known file's score drops below baseline beyond its tolerance band."""
    failures: list[str] = []
    base_files = baseline.get("files", {})
    for path, cur in current.items():
        base = base_files.get(path)
        if base is None:
            print(
                f"  + new file (not yet in baseline): {path} score={cur['score']:.3f}"
            )
            continue
        band = tolerance_score(cur["total"], fraction, mutants)
        if cur["score"] < base["score"] - band:
            failures.append(
                f"  REGRESSION {path}: {cur['score']:.3f} < baseline {base['score']:.3f} "
                f"- band {band:.3f} (killed {cur['killed']}/{cur['total']})"
            )
    return failures


def check_strict(
    current: dict[str, dict], baseline: dict[str, dict], fraction: float, mutants: int
) -> list[str]:
    """Fail when current results drift from baseline beyond the tolerance band."""
    failures: list[str] = []
    base_files = baseline.get("files", {})
    cur_paths, base_paths = set(current), set(base_files)
    for path in sorted(base_paths - cur_paths):
        failures.append(f"  MISSING in current results (in baseline): {path}")
    for path in sorted(cur_paths - base_paths):
        failures.append(
            f"  UNRECORDED file (not in baseline): {path} score={current[path]['score']:.3f}"
        )
    for path in sorted(cur_paths & base_paths):
        cur, base = current[path], base_files[path]
        band = tolerance_score(cur["total"], fraction, mutants)
        if abs(cur["score"] - base["score"]) > band:
            direction = "improved" if cur["score"] > base["score"] else "regressed"
            failures.append(
                f"  DRIFT {path}: {direction} {base['score']:.3f} -> {cur['score']:.3f} "
                f"(> band {band:.3f}; refresh the committed baseline)"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=("floor", "strict"), required=True)
    parser.add_argument(
        "--stats",
        type=Path,
        required=True,
        help="per-file stats JSON from scripts/mutation_stats.py",
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--tolerance-fraction",
        type=float,
        default=None,
        help=f"fractional band (default: baseline's value or {TOLERANCE_FRACTION})",
    )
    parser.add_argument(
        "--tolerance-mutants",
        type=int,
        default=None,
        help=f"absolute-mutant band (default: baseline's value or {TOLERANCE_MUTANTS})",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="write current scores back to the baseline (ratchets upward)",
    )
    args = parser.parse_args(argv)

    if not args.stats.exists():
        print(f"ERROR: stats file not found: {args.stats}", file=sys.stderr)
        return 2

    current = scores_from_stats(load_json(args.stats))

    baseline = load_json(args.baseline) if args.baseline.exists() else None

    def setting(flag, key, default):
        if flag is not None:
            return flag
        return (baseline or {}).get(key, default)

    fraction = setting(
        args.tolerance_fraction, "tolerance_fraction", TOLERANCE_FRACTION
    )
    mutants = setting(args.tolerance_mutants, "tolerance_mutants", TOLERANCE_MUTANTS)

    if baseline is None:
        if args.update:
            write_baseline(args.baseline, current, fraction, mutants)
            print(f"Created baseline {args.baseline} with {len(current)} files.")
            return 0
        print(
            f"ERROR: baseline not found: {args.baseline} (run with --update to create it)",
            file=sys.stderr,
        )
        return 2

    failures = (
        check_floor(current, baseline, fraction, mutants)
        if args.mode == "floor"
        else check_strict(current, baseline, fraction, mutants)
    )

    overall = sum(c["killed"] for c in current.values())
    overall_total = sum(c["total"] for c in current.values())
    pct = (overall / overall_total * 100) if overall_total else 100.0
    print(
        f"Mutation score: {overall}/{overall_total} = {pct:.1f}% across "
        f"{len(current)} files (mode={args.mode}, band=max({fraction:.2f}xN, {mutants})."
    )

    if failures:
        print(f"\n{len(failures)} ratchet failure(s):")
        print("\n".join(failures))
        if args.update and args.mode == "floor":
            print("\nRefusing to update baseline while regressions exist.")
        return 1

    if args.update:
        # Ratchet upward: keep the higher of baseline/current per file.
        merged = {**baseline.get("files", {})}
        for path, cur in current.items():
            base = merged.get(path)
            merged[path] = (
                cur if base is None or cur["score"] >= base["score"] else base
            )
        write_baseline(args.baseline, dict(sorted(merged.items())), fraction, mutants)
        print(f"Baseline updated: {args.baseline}")

    print("OK: no mutation-score regression beyond tolerance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
