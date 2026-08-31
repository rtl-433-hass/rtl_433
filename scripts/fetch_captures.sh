#!/usr/bin/env bash
# Fetch the pinned rtl_433_tests captures used by the screenshot harness and by
# scripts/regen_capture_fixtures.py.
#
# Why not plain `git submodule update --init`: the upstream repository is ~1.5 GB
# and we need four directories from it. Git does NOT honour a `sparse-checkout`
# key in .gitmodules -- that key is not part of the .gitmodules schema and is
# silently ignored, so a plain submodule init downloads the whole thing. This
# script does the real equivalent: a blobless, sparse clone pinned to the same
# commit the superproject records, which fetches only the blobs it checks out.
#
# CAPTURE_PATHS below is the single source of truth for which directories we
# need; the matching line in .gitmodules is documentation only.
#
# Usage:
#   scripts/fetch_captures.sh          # fetch (no-op if already at the pin)
#   scripts/fetch_captures.sh --force  # discard and re-fetch

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUBMODULE_PATH="tests/integration/rtl_433_tests"
SUBMODULE_DIR="${REPO_ROOT}/${SUBMODULE_PATH}"
UPSTREAM_URL="https://github.com/merbanan/rtl_433_tests"

# Directories to check out. Acurite_592TXR and Acurite_606TX feed the
# containerized screenshot harness; scmplus/01 and ert/scm/01 feed the golden
# capture fixtures under tests/fixtures/generated/.
CAPTURE_PATHS=(
    tests/acurite/Acurite_592TXR
    tests/acurite/Acurite_606TX
    tests/scmplus/01
    tests/ert/scm/01
)

# The pin lives in the superproject's gitlink, so it moves with a normal
# `git submodule update --remote` + commit and never drifts from .gitmodules.
PINNED_SHA="$(git -C "$REPO_ROOT" rev-parse "HEAD:${SUBMODULE_PATH}")"

if [[ "${1:-}" == "--force" ]]; then
    rm -rf "$SUBMODULE_DIR"
fi

if [[ -d "${SUBMODULE_DIR}/.git" ]]; then
    current="$(git -C "$SUBMODULE_DIR" rev-parse HEAD)"
    if [[ "$current" == "$PINNED_SHA" ]]; then
        echo "captures already at pinned ${PINNED_SHA:0:12}"
        exit 0
    fi
    echo "captures at ${current:0:12}, want ${PINNED_SHA:0:12}; re-fetching" >&2
    rm -rf "$SUBMODULE_DIR"
fi

echo "cloning captures (blobless, sparse) at ${PINNED_SHA:0:12}" >&2
git clone --filter=blob:none --no-checkout --sparse "$UPSTREAM_URL" "$SUBMODULE_DIR"

# --no-cone: these are explicit directory paths, not a cone of top-level dirs.
git -C "$SUBMODULE_DIR" sparse-checkout set --no-cone "${CAPTURE_PATHS[@]}"
git -C "$SUBMODULE_DIR" checkout --quiet "$PINNED_SHA"

for path in "${CAPTURE_PATHS[@]}"; do
    if [[ ! -d "${SUBMODULE_DIR}/${path}" ]]; then
        echo "expected capture directory missing after checkout: ${path}" >&2
        exit 1
    fi
done

echo "captures ready: $(du -sh "$SUBMODULE_DIR" | cut -f1)" >&2
