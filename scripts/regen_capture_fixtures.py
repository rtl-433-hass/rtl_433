#!/usr/bin/env python3
"""Regenerate the golden event fixtures under ``tests/fixtures/generated/``.

Decodes the pinned ``.cu8`` captures from the ``rtl_433_tests`` submodule with
the pinned ``rtl_433`` container image and writes the emitted JSON events into
``tests/fixtures/generated/``. Those files are committed, so the test suite reads
them offline and in milliseconds; this script is what refreshes them.

Why it exists
-------------
The device library's field keys are transcribed by hand from upstream rtl_433
``data_make()`` calls in C, and field matching is case-sensitive. A transcription
slip is completely silent -- keying SCMplus consumption as ``consumption``
instead of ``Consumption`` produced no sensor and no error. Capturing rtl_433's
real output as a fixture turns ``tests/test_fixture_coverage.py`` into a check
against the actual wire format instead of against a hand-written guess at it.

Running this in CI and failing on a diff (see ``.github/workflows/captures.yml``)
means an upstream field rename, or a bump of the pinned image, shows up as a red
build rather than as a device-library key that silently stops matching.

Do not hand-edit the generated files, and do not assert against the ``.json``
files vendored in the ``rtl_433_tests`` submodule: those are stale. The SCMplus
one still records ``"model": "SCM+"`` with no ``id`` field, while rtl_433 25.12
emits ``"model": "SCMplus"`` plus ``id``.

Usage
-----
    python3 scripts/regen_capture_fixtures.py            # rewrite the fixtures
    python3 scripts/regen_capture_fixtures.py --check     # fail on any drift

Requires Docker and the captures. Fetch them with the helper, not with
``git submodule update`` -- the ``sparse-checkout`` key in .gitmodules is not a
real git option and is ignored, so a plain init pulls all ~1.5 GB of upstream:

    ./scripts/fetch_captures.sh
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import json
from pathlib import Path
import re
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURES_ROOT = REPO_ROOT / "tests" / "integration" / "rtl_433_tests" / "tests"
COMPOSE_FILE = REPO_ROOT / "tests" / "integration" / "docker-compose.yml"
OUTPUT_DIR = REPO_ROOT / "tests" / "fixtures" / "generated"

# Metadata flags. `-M level` adds mod/freq/rssi/snr/noise, matching what the
# containerized harness runs and what a real hub with level reporting sends, so
# the fixtures exercise the signal-diagnostic descriptors too.
#
# Deliberately NOT `-M time:iso`: that stamps wall-clock time, which would make
# every regeneration a diff and the CI check permanently red. Replaying a file
# leaves rtl_433's default relative "@<offset>s" timestamps, which are a pure
# function of the capture and therefore stable.
METADATA_FLAGS = ("-M", "level")


@dataclass(frozen=True)
class CaptureGroup:
    """One output fixture, decoded from captures that share a sample rate."""

    fixture: str
    sample_rate: str
    captures: tuple[str, ...]


CAPTURE_GROUPS = (
    # The same Acurite-592TXR capture the screenshot harness replays. Included
    # so the golden-fixture mechanism is covered by a second, non-meter protocol
    # (temperature / humidity / battery keys) rather than only the SCM family.
    CaptureGroup(
        fixture="acurite_tower.json",
        sample_rate="250k",
        captures=("acurite/Acurite_592TXR/acurite-592txr-003.cu8",),
    ),
    # SCMplus: CamelCased fields (Consumption, MeterType, EndpointID, ...).
    # g005_912.6M_2359.3k.cu8 is deliberately absent -- it decodes to zero events
    # under rtl_433 25.12, which is what upstream's `ignore` marker in that
    # directory is about. Adding it back would only mask a real decode failure.
    CaptureGroup(
        fixture="scmplus.json",
        sample_rate="2359296",
        captures=(
            "scmplus/01/g002_912.6M_2359.3k.cu8",
            "scmplus/01/g003_912.6M_2359.3k.cu8",
        ),
    ),
    # ERT-SCM: lower_snake_case fields (consumption_data, ert_type, ...). Both
    # captures report ert_type 12, which the calibration flow reads as Gas.
    CaptureGroup(
        fixture="ert_scm.json",
        sample_rate="2400k",
        captures=(
            "ert/scm/01/g001_912.6M_2400k.cu8",
            "ert/scm/01/g002_912.6M_2400k.cu8",
        ),
    ),
)


def rtl433_image() -> str:
    """Return the digest-pinned rtl_433 image from the compose file.

    Read rather than duplicated so there is exactly one place the pin lives; a
    bump to ``docker-compose.yml`` regenerates against the new image with no
    matching edit here.
    """
    if not COMPOSE_FILE.is_file():
        raise SystemExit(f"compose file not found: {COMPOSE_FILE}")
    match = re.search(
        r"hertzg/rtl_433@sha256:[0-9a-f]{64}", COMPOSE_FILE.read_text(encoding="utf-8")
    )
    if match is None:
        raise SystemExit(
            f"no digest-pinned hertzg/rtl_433 image found in {COMPOSE_FILE}; "
            "the pin moved or changed shape -- update rtl433_image()."
        )
    return match.group(0)


def decode(image: str, capture: str, sample_rate: str) -> list[dict]:
    """Decode one capture and return its events, newest rtl_433 output order.

    One container per capture rather than one per group: rtl_433 accepts several
    ``-r`` inputs at once, but then a capture that decodes to nothing is
    indistinguishable from one that simply had fewer frames.
    """
    path = CAPTURES_ROOT / capture
    if not path.is_file():
        raise SystemExit(
            f"capture not found: {path}\nFetch the pinned captures first:\n"
            "  ./scripts/fetch_captures.sh"
        )
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{CAPTURES_ROOT}:/captures:ro",
            image,
            "-r",
            f"cu8:/captures/{capture}",
            "-s",
            sample_rate,
            *METADATA_FLAGS,
            "-F",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"rtl_433 failed on {capture} (exit {proc.returncode}):\n{proc.stderr}"
        )

    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    if not events:
        # A capture that stops decoding is the signal this whole check exists to
        # surface, so it is a hard error rather than an empty fixture.
        raise SystemExit(
            f"{capture} decoded to zero events under {image}.\n"
            "Either the capture no longer decodes with the pinned image, or the "
            "sample rate is wrong. Do not paper over it with an empty fixture."
        )
    return events


def render(events: list[dict]) -> str:
    """Serialize events as the committed fixture text.

    Events are kept verbatim and in emission order, including the repeats that
    come from a device transmitting the same frame several times in one capture.
    The file is meant to be exactly what rtl_433 produced; deduplicating or
    reordering here would make it a summary rather than a golden output.
    """
    return json.dumps(events, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if the committed fixtures are stale",
    )
    args = parser.parse_args()

    image = rtl433_image()
    print(f"rtl_433 image: {image}", file=sys.stderr)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []

    for group in CAPTURE_GROUPS:
        events: list[dict] = []
        for capture in group.captures:
            decoded = decode(image, capture, group.sample_rate)
            print(f"  {capture}: {len(decoded)} event(s)", file=sys.stderr)
            events.extend(decoded)

        target = OUTPUT_DIR / group.fixture
        rendered = render(events)
        current = target.read_text(encoding="utf-8") if target.is_file() else ""

        if args.check:
            if current != rendered:
                stale.append(group.fixture)
                sys.stderr.writelines(
                    difflib.unified_diff(
                        current.splitlines(keepends=True),
                        rendered.splitlines(keepends=True),
                        fromfile=f"committed/{group.fixture}",
                        tofile=f"decoded/{group.fixture}",
                    )
                )
            continue

        target.write_text(rendered, encoding="utf-8")
        print(f"wrote {target.relative_to(REPO_ROOT)}", file=sys.stderr)

    if stale:
        print(
            f"\nStale generated fixtures: {', '.join(stale)}\n"
            "rtl_433's output no longer matches the committed fixtures. If the "
            "field names changed, the device library under "
            "custom_components/rtl_433/device_library/ needs the same change. "
            "Refresh with: python3 scripts/regen_capture_fixtures.py",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
