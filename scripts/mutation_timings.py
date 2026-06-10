#!/usr/bin/env python3
"""Export per-file mutmut runtime so the sharder can balance by *time*, not count.

The mutation matrix shards the package across parallel jobs. Balancing by mutant
*count* is wrong because per-mutant test time varies widely (e.g. ``entity.py`` is
~2.5x slower per mutant than ``coordinator/base.py``), so a count-balanced shard
set still has a slow pole. This script records each file's measured mutmut run
time — the sum of its mutants' actual test durations (``durations_by_key`` in
mutmut's per-file meta) — which ``scripts/mutation_shards.py`` then uses as the
bin-pack weight.

Like the baseline, the resulting ``scripts/mutation_timings.json`` is a committed
profile refreshed periodically: run a FULL ``mutmut run`` (so every file's mutants
execute and get timed), then::

    mutmut run
    python scripts/mutation_timings.py            # writes scripts/mutation_timings.json

Timing drifts run-to-run and machine-to-machine, but the sharder only needs the
*relative* ordering to be roughly right, so a stale profile degrades gracefully to
a slightly suboptimal (never incorrect) split. A file absent from the profile
falls back to a count-based estimate in the sharder.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from mutmut.__main__ import SourceFileMutationData, walk_mutatable_files

DEFAULT_OUT = Path(__file__).with_name("mutation_timings.json")


def collect_timings() -> dict[str, float]:
    """Sum each mutable file's actual per-mutant test durations, in seconds."""
    timings: dict[str, float] = {}
    for path in walk_mutatable_files():
        meta = Path("mutants") / (str(path) + ".meta")
        if not meta.exists():
            continue
        data = SourceFileMutationData(path=path)
        data.load()
        durations = getattr(data, "durations_by_key", {}) or {}
        if not durations:
            continue
        timings[str(path)] = round(sum(durations.values()), 3)
    return dict(sorted(timings.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    timings = collect_timings()
    if not timings:
        print(
            "ERROR: no timing data found in mutants/*.meta — run a full `mutmut "
            "run` first so every file's mutants execute and get timed.",
            file=sys.stderr,
        )
        return 2
    args.out.write_text(
        json.dumps({"files": timings}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    total = sum(timings.values())
    print(f"Wrote {args.out} ({len(timings)} files, {total:.0f}s total mutmut time).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
