"""Guards for the screenshot harness's image pins, not for the harness itself.

``tests/integration/`` is a maintainer tool: nothing in CI brings those
containers up, so a green test suite says nothing about whether the harness
still runs. That is exactly why its pins need a guard here — the one thing CI
*can* check cheaply is that they still say what they are supposed to say.

The Home Assistant pin is the one that matters. It tracks the ``homeassistant``
key in ``hacs.json``: the oldest release the integration claims to support, and
therefore the one worth running against, because on a newer image the harness
would pass while the integration was quietly broken for everyone sitting on the
minimum. Nothing moves it but a hand, and a hand that bumps ``hacs.json`` and
forgets the compose file leaves the two disagreeing with no symptom until
someone regenerates screenshots months later.

That is not hypothetical. The pin sat on 2026.5.4 while ``hacs.json`` declared
2026.9.0, below a floor the integration genuinely needs
(``device_registry.async_get_device_by_identifier``, adopted in #241), and the
harness had stopped working entirely: the config entry raised ``AttributeError``
on setup, every panel capture hit its "no cards" guard and logged a skip, and
the run still exited 0.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HACS = _REPO_ROOT / "hacs.json"
_COMPOSE = _REPO_ROOT / "tests" / "integration" / "docker-compose.yml"

# mutmut copies only the package, tests/ and pyproject.toml into its ``mutants/``
# sandbox, so hacs.json is absent there. This meta-test adds no mutation coverage
# anyway (it exercises no package source), so skip the module in that
# environment; the normal pytest job runs it in full.
if not _HACS.is_file():
    pytest.skip(
        "hacs.json absent (mutmut sandbox); this meta-test runs in the normal "
        "pytest job only",
        allow_module_level=True,
    )

# repository[:tag]@sha256:<64 hex>, with the tag and digest captured separately.
#
# The path segments exclude "/" as well as the delimiters. That is what keeps the
# pattern linear: a segment class that could itself match "/" would overlap with
# the "(?:/...)*" that repeats it, leaving many ways to split the same string and
# so exponential backtracking on input that ultimately fails to match. Excluding
# it leaves exactly one possible split.
_IMAGE = re.compile(
    r"^\s*image:\s*(?P<repo>[^\s:@/]+(?::\d+)?(?:/[^\s:@/]+)*)"
    r"(?::(?P<tag>[\w][\w.-]*))?"
    r"(?:@sha256:(?P<digest>[0-9a-f]{64}))?\s*$",
    re.MULTILINE,
)


def _images() -> dict[str, re.Match[str]]:
    """Every ``image:`` line in the compose file, keyed by repository."""
    text = _COMPOSE.read_text(encoding="utf-8")
    found = {match.group("repo"): match for match in _IMAGE.finditer(text)}
    assert found, f"no image: lines found in {_COMPOSE}"
    return found


def test_home_assistant_pin_matches_the_hacs_floor() -> None:
    """The harness runs the oldest Home Assistant the integration supports."""
    floor = json.loads(_HACS.read_text(encoding="utf-8"))["homeassistant"]
    image = _images()["ghcr.io/home-assistant/home-assistant"]
    assert image.group("tag") == floor, (
        f"harness pins Home Assistant {image.group('tag')!r} but hacs.json "
        f"declares {floor!r}. The harness must run the minimum supported "
        "release, so bump tests/integration/docker-compose.yml (tag *and* "
        "digest) whenever the hacs.json floor moves."
    )
