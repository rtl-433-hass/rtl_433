"""Mapping-coverage sweep over every JSON event fixture in ``tests/fixtures``.

Each event is run through the normalizer with the shipped skip-keys, and every
field key that survives must resolve to a library descriptor. A field that
resolves to nothing is precisely what the diagnostics export reports as
``unmatched_field_keys``: it builds no entity, so a user with that device sees a
sensor that never appears.

This exists as a regression net for one recurring class of bug. Library keys are
transcribed by hand from upstream rtl_433 ``data_make()`` calls in C, and field
matching is case-sensitive, so a transcription slip is silent -- keying SCMplus
consumption as ``consumption`` instead of ``Consumption`` produced no sensor at
all and no error anywhere. A sweep turns that into a failing test.

The fixtures under ``fixtures/generated/`` carry the most weight here: they are
decoded from real ``.cu8`` captures by ``scripts/regen_capture_fixtures.py``, so
the sweep runs against rtl_433's actual wire output rather than a hand-written
approximation of it. See ``tests/fixtures/generated/README.md``.

Discovery is recursive and unfiltered by design: adding a fixture anywhere under
``tests/fixtures`` opts it into the sweep with no wiring.
"""

from __future__ import annotations

from pathlib import Path

from pyrtl_433.normalizer import normalize
import pytest

from custom_components.rtl_433.mapping import load_library, lookup

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Relative names (e.g. ``generated/scmplus.json``) so failures name the file and
# test ids stay stable across runs. Resolved at import time for parametrization.
FIXTURE_NAMES = sorted(
    str(path.relative_to(FIXTURES_DIR)) for path in FIXTURES_DIR.rglob("*.json")
)


@pytest.fixture(scope="module")
def library():
    """Load the shipped device library once for the module."""
    return load_library()


def test_fixture_discovery_finds_the_committed_fixtures():
    """Guard the glob itself: an empty sweep would vacuously pass everything."""
    assert FIXTURE_NAMES, f"no JSON fixtures discovered under {FIXTURES_DIR}"
    # The generated captures are the point of the sweep; a bad path or a
    # half-finished regeneration must not quietly reduce coverage to the
    # hand-authored fixtures.
    assert any(name.startswith("generated/") for name in FIXTURE_NAMES)


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_fixture_fields_all_resolve_to_descriptors(fixture_name, library, events):
    """Every non-skipped field in every fixture event maps to a descriptor.

    Resolution is model-scoped, matching what ``entity.py`` does when it decides
    whether to build an entity for a field -- not the model-agnostic flat lookup
    the diagnostics export uses.
    """
    registry, skip_keys = library
    skip = set(skip_keys)

    unmatched: set[tuple[str, str]] = set()
    for event in events(fixture_name):
        normalized = normalize(event, skip)
        unmatched |= {
            (normalized.model, field_key)
            for field_key in normalized.fields
            if lookup(field_key, normalized.model, registry=registry) is None
        }

    assert not unmatched, (
        f"{fixture_name}: fields with no library descriptor "
        f"{sorted(unmatched)}. Field names are case-sensitive and must match the "
        "upstream rtl_433 decoder verbatim: add a descriptor under "
        "custom_components/rtl_433/device_library/, or add the key to "
        "_skip_keys.yaml if it is identity or transport data."
    )
