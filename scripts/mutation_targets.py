#!/usr/bin/env python3
"""Map a PR's changed files to the mutmut targets the CI mutation job should run.

A full-package mutmut run is slow (~50 min). On pull requests we only need to
re-check the modules a PR could have affected, so this helper turns
``git diff --name-only`` output into:

* the source modules to mutate (so the per-file floor is enforced on touched code), and
* the matching ``mutmut run`` filter patterns.

Mapping rules (given changed paths on argv):

* A changed ``custom_components/rtl_433/<mod>.py`` maps to itself.
* A changed test file maps to the source module it exercises, when that can be
  resolved unambiguously: ``tests/test[_mut]_<name>.py`` →
  ``custom_components/rtl_433/<name>.py`` (also trying ``<a>/<b>.py`` for a
  ``<a>_<b>`` name, e.g. ``coordinator_base`` → ``coordinator/base.py``). This
  closes the "a test was weakened but its source is unchanged" blind spot for the
  common case. A test that can't be resolved to one module escalates to a full run.
* A few tests don't follow that 1:1 convention — compound tests that exercise
  several modules (``test_diagnostics_repairs``), the ``coordinator`` package
  root, and the ``__init__`` root (no ``init.py`` exists for the resolver to
  find). They are listed in ``EXPLICIT_TEST_SOURCES`` with the exact modules
  they cover, so touching them scopes rather than escalates. Genuinely broad
  integration tests (e.g. ``test_lifecycle.py``) are deliberately left out, so
  they still escalate — a full run is correct when they change.
* Any change to mutation infrastructure or shared test scaffolding
  (``pyproject.toml``, ``requirements_test.txt``, ``tests/conftest.py``,
  ``scripts/mutation_*.py``, the mutation workflow) escalates to a full run,
  because it can change results package-wide.

Output (stdout), three lines:
    line 1: ``all`` for a full run, or ``scoped``
    line 2: space-separated mutmut filter patterns (empty when nothing in scope)
    line 3: space-separated source paths (empty when nothing in scope)

A ``scoped`` mode with empty lines 2/3 means "no source in scope" — the caller
should pass (e.g. a docs-only PR).
"""

from __future__ import annotations

from pathlib import Path
import sys

PKG = "custom_components/rtl_433"
PKG_DOTTED = "custom_components.rtl_433"

# Changes to these escalate to a full run (they can move results package-wide).
FULL_RUN_TRIGGERS = {
    "pyproject.toml",
    "requirements_test.txt",
    "tests/conftest.py",
    "scripts/mutation_stats.py",
    "scripts/mutation_ratchet.py",
    "scripts/mutation_targets.py",
    ".github/workflows/mutation.yml",
}

# Tests whose filename does not map 1:1 to a source module via the naming
# convention below, declared with the modules they actually exercise so a PR
# touching them scopes instead of escalating to a full run. Covers compound
# tests (``test_diagnostics_repairs`` -> diagnostics + repairs), the
# ``coordinator`` package root, and the ``__init__`` root (``source_for_test``
# cannot reach ``__init__.py`` — there is no ``init.py``).
#
# Genuinely broad integration tests (``test_lifecycle.py`` drives the whole
# config-entry setup across __init__, entity, and every platform) are
# intentionally absent so they still escalate. Values are module paths under
# ``custom_components/rtl_433/``. An entry that under-specifies a test's modules
# is caught by the push-to-main + nightly FULL runs (which re-verify the entire
# baseline), so it is a delayed catch, never a silent floor blind spot.
EXPLICIT_TEST_SOURCES: dict[str, list[str]] = {
    "tests/test_coordinator.py": ["coordinator/base.py"],
    "tests/test_mut_init.py": ["__init__.py", "migration.py", "hub_settings.py"],
    "tests/test_config_flow.py": ["config_flow.py", "options_flow.py"],
    "tests/test_mut_config_flow.py": ["config_flow.py", "options_flow.py"],
    "tests/test_binary_sensor_motion.py": ["binary_sensor.py", "event.py"],
    "tests/test_event_trace.py": ["event.py"],
    "tests/test_diagnostics_repairs.py": ["diagnostics.py", "repairs.py"],
    "tests/test_sdr_controls.py": [
        "number.py",
        "select.py",
        "switch.py",
        "sdr_settings.py",
    ],
    # The sdr_settings HA adapter mapping (``test_sdr_settings_adapter`` does not
    # auto-resolve to a source module via the naming convention).
    "tests/test_sdr_settings_adapter.py": ["sdr_settings.py"],
    # The ``mapping`` package: its test files exercise the whole package, and the
    # package root (``mapping/__init__.py``) is unreachable via the naming
    # convention (there is no ``mapping.py``), so map all three explicitly.
    "tests/test_mapping.py": [
        "mapping/__init__.py",
        "mapping/_loader.py",
        "mapping/_model.py",
        "mapping/_overrides.py",
        "mapping/_transform.py",
    ],
    "tests/test_mut_mapping.py": [
        "mapping/__init__.py",
        "mapping/_loader.py",
        "mapping/_model.py",
        "mapping/_overrides.py",
        "mapping/_transform.py",
    ],
    "tests/test_mut_mapping_floor.py": [
        "mapping/__init__.py",
        "mapping/_loader.py",
        "mapping/_model.py",
        "mapping/_overrides.py",
        "mapping/_transform.py",
    ],
    # Mutation-floor test files: their ``_floor`` suffix does not auto-resolve to
    # a source module via the naming convention, so each is mapped explicitly.
    "tests/test_mut_calibration_floor.py": ["calibration.py"],
    "tests/test_mut_library_floor.py": ["library.py"],
    "tests/test_mut_migration_floor.py": ["migration.py"],
    # End-to-end migration round-trip identity tests: exercise the migration
    # ladder (``async_migrate_entry``) against seeded registries; the
    # ``_roundtrip`` suffix does not auto-resolve to a module.
    "tests/test_migration_roundtrip.py": ["migration.py"],
    "tests/test_mut_repairs_floor.py": ["repairs.py"],
}


def source_for_test(stem: str) -> str | None:
    """Resolve a test-file stem to its source module path, or None if ambiguous."""
    for prefix in ("test_mut_", "test_"):
        if stem.startswith(prefix):
            name = stem[len(prefix) :]
            break
    else:
        return None
    # Try a flat module, then progressively turn underscores into a sub-path
    # (coordinator_base -> coordinator/base) so package submodules resolve.
    candidates = [name.replace("_", "/")]
    parts = name.split("_")
    for i in range(len(parts) - 1, 0, -1):
        candidates.append("/".join(["_".join(parts[:i]), *parts[i:]]))
    candidates.append(name)
    for cand in dict.fromkeys(candidates):
        path = f"{PKG}/{cand}.py"
        if Path(path).is_file():
            return path
    return None


def resolve(changed: list[str]) -> tuple[bool, set[str]]:
    """Return (full_run, source_paths) for the changed files."""
    sources: set[str] = set()
    for raw in changed:
        f = raw.strip()
        if not f:
            continue
        if f in FULL_RUN_TRIGGERS:
            return True, set()
        if f.startswith(f"{PKG}/") and f.endswith(".py"):
            sources.add(f)
        elif f.startswith("tests/") and f.endswith(".py"):
            explicit = EXPLICIT_TEST_SOURCES.get(f)
            if explicit is not None:
                sources.update(f"{PKG}/{module}" for module in explicit)
                continue
            src = source_for_test(Path(f).stem)
            if src is None:
                # A broad/unmappable test changed — be safe and run everything.
                return True, set()
            sources.add(src)
        # Any other path (docs, brands, etc.) is irrelevant to mutation.
    return False, sources


def main(argv: list[str]) -> int:
    changed = argv or sys.stdin.read().split()
    full, sources = resolve(changed)
    if full:
        print("all")
        print("")
        print("")
        return 0
    paths = sorted(sources)
    patterns = [
        f"{PKG_DOTTED}.{p[len(PKG) + 1 : -3].replace('/', '.')}.*" for p in paths
    ]
    print("scoped")
    print(" ".join(patterns))
    print(" ".join(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
