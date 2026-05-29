#!/usr/bin/env python3
"""Emit per-file mutmut statistics as JSON for the ratchet comparator.

mutmut 3.x's ``export-cicd-stats`` only writes a project-wide summary, so this
helper reads mutmut's per-file ``mutants/*.meta`` data directly (via mutmut's own
API) and emits a ``{"files": {path: {killed, survived, timeout, ...}}}`` payload
in the schema ``mutation_ratchet.py`` expects.

Run from the repository root after ``mutmut run``:
    python scripts/mutation_stats.py > stats.json

For a scoped (changed-files) run, pass ``--paths`` to restrict the output to the
mutated source files. This is required after a filtered ``mutmut run`` because
mutants outside the filter stay "not checked" (which would otherwise read as 0%):
    python scripts/mutation_stats.py --paths custom_components/rtl_433/switch.py > stats.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

import mutmut
from mutmut.__main__ import (
    SourceFileMutationData,
    ensure_config_loaded,
    status_by_exit_code,
    walk_source_files,
)

# Map mutmut status strings onto the buckets the ratchet understands. A mutation
# that crashes the interpreter (segfault) or trips an internal pytest error is a
# detection, so it counts as killed; "no tests"/"suspicious"/"not checked" all
# count against the score (they are recorded survivors, never suppressed).
_KILLED = {"killed", "timeout", "segfault"}
_BUCKET = {
    "killed": "killed",
    "timeout": "timeout",
    "segfault": "killed",
    "survived": "survived",
    "no tests": "no_tests",
    "suspicious": "suspicious",
    "skipped": "skipped",
    "caught by type check": "skipped",
    "not checked": "survived",
    "check was interrupted by user": "survived",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help="restrict output to these source paths (for scoped runs)",
    )
    args = parser.parse_args(argv)
    only = {str(Path(p)) for p in args.paths} if args.paths else None

    ensure_config_loaded()
    files: dict[str, dict] = {}
    for path in walk_source_files():
        if mutmut.config.should_ignore_for_mutation(path):
            continue
        if only is not None and str(path) not in only:
            continue
        meta = Path("mutants") / (str(path) + ".meta")
        if not meta.exists():
            continue
        data = SourceFileMutationData(path=path)
        data.load()
        if not data.exit_code_by_key:
            continue
        counts: dict[str, int] = defaultdict(int)
        for exit_code in data.exit_code_by_key.values():
            status = status_by_exit_code[exit_code]
            counts[_BUCKET.get(status, "suspicious")] += 1
        counts["total"] = sum(v for k, v in counts.items() if k != "total")
        files[str(path)] = {
            "killed": counts.get("killed", 0),
            "survived": counts.get("survived", 0),
            "timeout": counts.get("timeout", 0),
            "suspicious": counts.get("suspicious", 0),
            "skipped": counts.get("skipped", 0),
            "no_tests": counts.get("no_tests", 0),
            "total": counts["total"],
        }
    json.dump({"files": dict(sorted(files.items()))}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
