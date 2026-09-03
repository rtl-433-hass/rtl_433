"""Guards for *this repo's* mutation configuration, not for the tooling itself.

The mutation gate (``.github/workflows/mutation.yml``) is driven by
``mutmut-ratchet`` (https://github.com/rtl-433-hass/mutmut-ratchet), which is
tested in its own repository. What that tooling cannot check is the per-repo
data it is pointed at: the ``[tool.mutmut_ratchet]`` table in ``pyproject.toml``
and the committed ``scripts/mutation_baseline.json``.

Two properties matter here, and both fail silently if they rot:

* A test whose filename maps to no source module escalates *every* PR that
  touches it to a full ~50-min run — or, worse, an explicit mapping that points
  at a renamed module under-scopes the run and misses a floor regression.
* A baseline file that lands in zero shards escapes the per-file floor entirely;
  one in two shards is mutated twice.

These replace the old ``tests/test_mutation_{targets,shards}.py``, which tested
the copy-pasted ``scripts/mutation_*.py`` helpers alongside this same data.
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path

from mutmut_ratchet import Config, load_config
from mutmut_ratchet.shards import mutable_modules, shard_for
from mutmut_ratchet.targets import source_for_test
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE = _REPO_ROOT / "scripts" / "mutation_baseline.json"

# mutmut copies only the package, tests/, and pyproject.toml into its
# ``mutants/`` sandbox — not scripts/ — so this meta-test cannot see the
# baseline there. It adds no mutation coverage anyway (it exercises no package
# source), so skip the module in that environment; the normal pytest job (where
# scripts/ exists) runs it in full.
if not _BASELINE.is_file():
    pytest.skip(
        "scripts/mutation_baseline.json absent (mutmut sandbox); this meta-test "
        "runs in the normal pytest job only",
        allow_module_level=True,
    )

# Tests with no 1:1 package module by design, so escalating to a full run when
# they change is correct. Kept here (not in pyproject.toml) so declaring one is a
# deliberate, reviewed edit rather than a config tweak.
_NO_SINGLE_MODULE = {
    # Broad: drives the whole config-entry lifecycle across __init__, entity and
    # every platform.
    "tests/test_lifecycle.py",
    # Broad: timeout resolution spans const.py + __init__.py + coordinator + entity.
    "tests/test_availability_class_defaults.py",
    # Meta: this file, which tests the mutation configuration itself.
    "tests/test_mutation_config.py",
    # Meta: tests scripts/release_credits.py, which is not package source.
    "tests/test_release_credits.py",
}

# The number of shards the mutation workflow's matrix uses. Checked explicitly
# so a change to the matrix width without a re-check here is visible.
_WORKFLOW_SHARDS = 6

# Modules mutmut walks but generates no mutants for, so they legitimately appear
# in neither the timings profile nor the baseline. A pure re-export shim has
# nothing to mutate. Listed explicitly rather than detected, because generating
# mutants to find out would cost a full mutmut run; the two coverage tests below
# close the loop from the other side -- if one of these ever *gains* mutants, the
# next profile refresh adds it and their "stale entry" half fails until it is
# removed from here.
_NO_MUTANTS = {
    "custom_components/rtl_433/coordinator/__init__.py",
    # Every function in the WebSocket API is decorated -- `@callback` on the
    # helpers, `@websocket_api.websocket_command` on the handlers -- and mutmut
    # cannot rewrite a decorated function into its `x_*`/`xǁ*` trampoline form,
    # so it generates nothing for this module: the meta it writes has an empty
    # `hash_by_function_name`. Checked, not assumed.
    #
    # That means these commands are not mutation-gated yet, which is a real gap
    # rather than a preference. It closes on its own as the module grows
    # undecorated helpers -- and when it does, this entry has to come out or the
    # stale half of the coverage check fails.
    "custom_components/rtl_433/websocket_api.py",
}


@pytest.fixture(autouse=True)
def _at_repo_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Both helpers probe repo-relative paths, so run from the repo root.

    mutmut caches its parsed configuration in a process-global, so drop it
    afterwards rather than leaving a cwd-dependent singleton behind.
    """
    from mutmut.configuration import Config as MutmutConfig

    monkeypatch.chdir(_REPO_ROOT)
    MutmutConfig.reset()
    yield
    MutmutConfig.reset()


@pytest.fixture
def config() -> Config:
    return load_config(_REPO_ROOT / "pyproject.toml")


def test_config_points_at_this_package(config: Config) -> None:
    """The table names the integration package and the committed baseline/timings."""
    assert config.package_path == "custom_components/rtl_433"
    assert config.package_dotted == "custom_components.rtl_433"
    assert config.baseline == _BASELINE
    assert config.timings == _REPO_ROOT / "scripts" / "mutation_timings.json"


def test_escalate_paths_all_exist(config: Config) -> None:
    """An escalation trigger naming a deleted file silently stops escalating."""
    assert config.escalate_paths
    for rel in sorted(config.escalate_paths):
        assert (_REPO_ROOT / rel).exists(), (
            f"escalate_paths names a missing file: {rel}"
        )


def test_no_test_file_silently_escalates(config: Config) -> None:
    """Every ``tests/test_*.py`` resolves, is explicitly mapped, or is declared broad.

    This is the guard for the original bug: a test whose name maps to a
    non-existent module (``test_coordinator`` -> ``coordinator.py``, which does
    not exist) silently escalates every touching PR to a full mutation run. A new
    such test fails here until it is added to the
    ``[tool.mutmut_ratchet.explicit_test_sources]`` table or to
    ``_NO_SINGLE_MODULE``.
    """
    offenders = []
    for path in sorted((_REPO_ROOT / "tests").glob("test_*.py")):
        rel = f"tests/{path.name}"
        if rel in config.explicit_test_sources or rel in _NO_SINGLE_MODULE:
            continue
        if source_for_test(path.stem, config) is None:
            offenders.append(rel)
    assert not offenders, (
        "these tests escalate to a full mutation run but are neither in "
        "[tool.mutmut_ratchet.explicit_test_sources] nor declared broad in "
        f"_NO_SINGLE_MODULE: {offenders}"
    )


def test_explicit_table_maps_real_tests_to_real_modules(config: Config) -> None:
    """Every override key is a real test file and every value a real module.

    Prevents the table from rotting into mappings that point at files which no
    longer exist — a renamed test or module would otherwise pass silently while
    under-scoping the modules its PR mutates.
    """
    assert config.explicit_test_sources
    for test_file, modules in config.explicit_test_sources.items():
        assert (_REPO_ROOT / test_file).is_file(), f"missing test file: {test_file}"
        for module in modules:
            target = _REPO_ROOT / config.source(module)
            assert target.is_file(), f"{test_file} maps to missing module: {module}"


def test_empty_mapping_scopes_to_nothing_instead_of_escalating(
    config: Config,
) -> None:
    """``test_fixture_coverage`` exercises no package source, and must not escalate.

    Everything it drives in code now lives in the ``pyrtl_433`` dependency, which
    mutmut does not mutate here. The empty list is load-bearing: dropping the
    entry entirely would send every PR that touches the fixture sweep into a full
    run.
    """
    from mutmut_ratchet.targets import resolve

    assert config.explicit_test_sources["tests/test_fixture_coverage.py"] == []
    full, sources = resolve(["tests/test_fixture_coverage.py"], config)
    assert full is False
    assert sources == set()


def test_declared_broad_tests_still_exist() -> None:
    """A stale ``_NO_SINGLE_MODULE`` entry would mask a genuinely broken mapping."""
    for rel in sorted(_NO_SINGLE_MODULE):
        assert (_REPO_ROOT / rel).is_file(), f"declared broad but missing: {rel}"


@pytest.mark.parametrize("of", [1, 2, 3, _WORKFLOW_SHARDS])
def test_every_baseline_file_lands_in_exactly_one_shard(
    config: Config, of: int
) -> None:
    """The shards partition the package: no baseline file is dropped or doubled.

    A module in zero shards escapes the per-file floor entirely; one in two is
    mutated twice. Both are silent, so the invariant is checked here against the
    real committed baseline for every split width CI might use — including the
    6-way split the mutation workflow's matrix actually runs.
    """
    shards = [shard_for(config, i, of) for i in range(of)]
    baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))
    for path in baseline["files"]:
        hits = sum(path in shard for shard in shards)
        assert hits == 1, f"{path} is in {hits} of {of} shards, expected 1"


def _expected_profiled_modules() -> set[str]:
    """Every module mutmut mutates, minus the ones that produce no mutants."""
    return set(mutable_modules()) - _NO_MUTANTS


def test_timings_profile_covers_every_mutable_module(config: Config) -> None:
    """A module missing from the timings profile silently unbalances the matrix.

    The sharder bin-packs by measured seconds. A module with no entry falls back
    to ``mutant_count * avg_seconds_per_mutant``, but that count comes from the
    *baseline* -- so a module missing from both (the normal case for anything
    added since the last profile refresh) is weighted as a single mutant and the
    packer treats it as very nearly free. It then lands wherever the bins happen
    to be lightest, and the shard that receives it runs far past the others while
    the gate waits on it.

    This is invisible without the check: the split is still correct, just badly
    balanced, so nothing fails -- CI simply gets slower. It is how five modules
    (repairs.py, device_replace.py and the three coordinator submodules, together
    ~15% of the package's mutants) came to carry no weight at all after the
    coordinator was split up.

    Refresh with a full ``mutmut run`` followed by ``mutmut-ratchet timings``.
    """
    profiled = set(json.loads(config.timings.read_text(encoding="utf-8"))["files"])
    expected = _expected_profiled_modules()

    missing = sorted(expected - profiled)
    assert not missing, (
        "these modules have no entry in scripts/mutation_timings.json, so the "
        "sharder weights them as ~free and the matrix is unbalanced; refresh "
        f"with `mutmut run && mutmut-ratchet timings`: {missing}"
    )
    stale = sorted(profiled - expected)
    assert not stale, (
        "scripts/mutation_timings.json profiles modules mutmut no longer mutates "
        f"(renamed, deleted, or now mutant-free): {stale}"
    )


def test_baseline_covers_every_mutable_module(config: Config) -> None:
    """A module missing from the baseline is not gated by the floor at all.

    ``ratchet --mode floor`` compares each file in the *current* results against
    its baseline entry; a file with no entry is reported as ``+ new file (not yet
    in baseline)`` and passes. That is the right behaviour for a genuinely new
    module on the PR that adds it, but it means a module which never makes it
    into the committed baseline is permanently exempt from the gate -- its score
    can fall to zero without failing anything.

    Kept as a separate assertion from the timings check because the two rot for
    the same reason but have different consequences: a missing timing costs CI
    minutes, a missing baseline entry costs coverage.
    """
    recorded = set(json.loads(_BASELINE.read_text(encoding="utf-8"))["files"])
    expected = _expected_profiled_modules()

    missing = sorted(expected - recorded)
    assert not missing, (
        "these modules are mutated but absent from scripts/mutation_baseline.json, "
        "so the per-file floor never gates them; add them with "
        f"`mutmut run && mutmut-ratchet stats > s.json && mutmut-ratchet ratchet "
        f"--mode floor --stats s.json --update`: {missing}"
    )
    stale = sorted(recorded - expected)
    assert not stale, (
        "scripts/mutation_baseline.json records modules mutmut no longer mutates; "
        f"a stale floor here can never be met: {stale}"
    )


def test_declared_mutant_free_modules_still_exist() -> None:
    """A stale ``_NO_MUTANTS`` entry would exempt a real module from both checks."""
    walked = set(mutable_modules())
    for rel in sorted(_NO_MUTANTS):
        assert (_REPO_ROOT / rel).is_file(), f"declared mutant-free but missing: {rel}"
        assert rel in walked, (
            f"{rel} is declared mutant-free but mutmut no longer walks it; drop it"
        )
