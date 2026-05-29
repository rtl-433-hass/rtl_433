"""Tests for ``scripts/mutation_targets.py`` — the PR mutation-target resolver.

The CI mutation job (``.github/workflows/mutation.yml``) uses this script to
decide which source modules a PR should mutate. The mapping is name-based with
an explicit override table; a wrong entry silently escalates every touching PR
to a full ~50-min run (or, worse, under-scopes and misses a floor regression),
so these tests keep the table honest and guard against the mis-mapping class of
bug (e.g. ``test_coordinator`` -> ``coordinator.py``, which does not exist).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG = "custom_components/rtl_433"
_SCRIPT = _REPO_ROOT / "scripts" / "mutation_targets.py"

# mutmut copies only the package, tests/, and pyproject into its ``mutants/``
# sandbox — not scripts/ — so this meta-test cannot load the script there. It
# adds no mutation coverage anyway (it exercises no package source), so skip the
# module in that environment; the normal pytest job (where scripts/ exists) runs
# it in full.
if not _SCRIPT.is_file():
    pytest.skip(
        "scripts/mutation_targets.py absent (mutmut sandbox); this meta-test "
        "runs in the normal pytest job only",
        allow_module_level=True,
    )


def _load_targets_module():
    """Load the standalone script (it lives in ``scripts/``, not a package)."""
    spec = importlib.util.spec_from_file_location("mutation_targets", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mt = _load_targets_module()

# Tests with no 1:1 package module by design, so escalating to a full run when
# they change is correct: broad integration tests and tooling/meta tests. Kept
# here (not in the script) so adding one is a deliberate, reviewed edit.
_NO_SINGLE_MODULE = {
    "tests/test_lifecycle.py",  # broad: the whole config-entry lifecycle
    "tests/test_mutation_targets.py",  # meta: tests this very script
}


@pytest.fixture(autouse=True)
def _chdir_repo_root(monkeypatch):
    """``source_for_test`` probes relative paths; run from the repo root."""
    monkeypatch.chdir(_REPO_ROOT)


def test_source_module_change_scopes_to_itself():
    full, sources = mt.resolve([f"{_PKG}/sensor.py"])
    assert full is False
    assert sources == {f"{_PKG}/sensor.py"}


def test_conforming_test_maps_to_its_module():
    full, sources = mt.resolve(["tests/test_mut_coordinator_base.py"])
    assert full is False
    assert sources == {f"{_PKG}/coordinator/base.py"}


def test_full_run_trigger_escalates():
    full, sources = mt.resolve(["tests/conftest.py"])
    assert full is True
    assert sources == set()


def test_docs_only_change_scopes_with_no_sources():
    full, sources = mt.resolve(["README.md"])
    assert full is False
    assert sources == set()


def test_broad_integration_test_still_escalates():
    # test_lifecycle.py exercises the whole lifecycle across many modules; a
    # full run when it changes is intentional, not a regression to "fix".
    full, _ = mt.resolve(["tests/test_lifecycle.py"])
    assert full is True


@pytest.mark.parametrize("test_file, modules", sorted(mt.EXPLICIT_TEST_SOURCES.items()))
def test_explicit_map_entry_scopes_to_its_modules(test_file, modules):
    full, sources = mt.resolve([test_file])
    assert full is False, f"{test_file} should scope, not trigger a full run"
    assert sources == {f"{_PKG}/{module}" for module in modules}


def test_explicit_map_keys_and_targets_all_exist():
    """Every override key is a real test file and every value a real module.

    Prevents the table from rotting into mappings that point at files which no
    longer exist (a renamed test or module would otherwise pass silently).
    """
    for test_file, modules in mt.EXPLICIT_TEST_SOURCES.items():
        assert (_REPO_ROOT / test_file).is_file(), f"missing test file: {test_file}"
        for module in modules:
            target = _REPO_ROOT / _PKG / module
            assert target.is_file(), f"{test_file} maps to missing module: {module}"


def test_no_test_file_silently_escalates():
    """Every ``tests/test_*.py`` resolves, is explicitly mapped, or is declared broad.

    This is the guard for the original bug: a test whose name maps to a
    non-existent module (``test_coordinator`` -> ``coordinator.py``) silently
    escalates every touching PR to a full run. A new such test now fails here
    until it is added to ``EXPLICIT_TEST_SOURCES`` or ``_NO_SINGLE_MODULE``.
    """
    offenders = []
    for path in sorted((_REPO_ROOT / "tests").glob("test_*.py")):
        rel = f"tests/{path.name}"
        if rel in mt.EXPLICIT_TEST_SOURCES or rel in _NO_SINGLE_MODULE:
            continue
        if mt.source_for_test(path.stem) is None:
            offenders.append(rel)
    assert not offenders, (
        "these tests escalate to a full mutation run but are neither in "
        f"EXPLICIT_TEST_SOURCES nor declared broad in _NO_SINGLE_MODULE: {offenders}"
    )
