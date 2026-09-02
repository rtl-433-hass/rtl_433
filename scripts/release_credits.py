#!/usr/bin/env python3
"""Append ``@username`` credits for outside contributors to the release PR body.

release-please writes a changelog whose entries link to commits, never to the
people who wrote them, so a community contribution lands in the release notes
anonymously and the contributor is never notified that their work shipped. This
helper closes that gap: it reads the open release PR, resolves every commit the
changelog links to back to the pull request it merged from, drops the ones
authored by maintainers and bots, and appends a marker-delimited credits block
naming the rest with an ``@mention``.

Run by ``.github/workflows/release-credits.yml`` after every Release run, so the
block is refreshed whenever release-please rewrites the PR.

"Maintainer" is decided by the pull request's GitHub ``author_association``:
``OWNER``, ``MEMBER`` and ``COLLABORATOR`` are maintainers (people who can push
to the repo), everyone else is an outside contributor worth crediting. Bots are
never credited. Extra logins can be excluded with ``--exclude``.

The block is delimited by HTML comment markers, so a rerun replaces its own
previous output instead of stacking duplicates, and the body is left untouched
when nothing changed (an unchanged body is not PATCHed at all, which keeps
GitHub from re-notifying the mentioned contributors on every release push).

Usage::

    # Update the open release PR (the workflow's invocation).
    GITHUB_TOKEN=... python3 scripts/release_credits.py --repo owner/name

    # A specific PR, printing the new body instead of writing it.
    python3 scripts/release_credits.py --repo owner/name --pr 210 --dry-run

Exit status is 0 when there is no open release PR: that is the normal state
right after a release PR merges, not a failure.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import sys
from typing import Any, Protocol
import urllib.error
import urllib.request

# Author associations that mean "can push to this repo", i.e. not someone whose
# contribution needs calling out in the release notes. Everything else
# (CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, FIRST_TIMER, NONE, MANNEQUIN) is an
# outside contributor.
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# Markers around the generated block. They must stay stable across releases:
# they are how a rerun finds and replaces its own previous output.
BLOCK_START = "<!-- release-credits:start -->"
BLOCK_END = "<!-- release-credits:end -->"

BLOCK_HEADING = "### Thanks to our contributors"

# release-please names its branch ``release-please--branches--<base>`` and
# labels the PR ``autorelease: pending``. Either is enough to identify it; both
# are checked so a label rename upstream does not silently disable this.
RELEASE_BRANCH_PREFIX = "release-please--"
RELEASE_LABEL = "autorelease: pending"

_SHA_RE = r"[0-9a-f]{7,40}"


class GitHubAPI(Protocol):
    """The slice of the GitHub REST API this script needs."""

    def get(self, path: str) -> Any:
        """GET ``path`` (repo-relative) and return the decoded JSON body."""

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        """PATCH ``path`` (repo-relative) with ``payload`` as JSON."""


@dataclass(frozen=True)
class Credit:
    """One outside contributor and the PRs of theirs in this release."""

    login: str
    pulls: tuple[int, ...]

    @property
    def first_pull(self) -> int:
        """Lowest PR number, used to order credits the way the changelog runs."""
        return self.pulls[0]


def extract_commit_shas(body: str, repo: str) -> list[str]:
    """Return the commit SHAs ``body`` links to, in first-seen order.

    Only links to ``repo`` count: a changelog can quote another project's commit
    (an upstream fix, a dependency bump), and resolving those against this repo
    would 404 or, worse, hit an unrelated commit that happens to share a prefix.
    """
    pattern = re.compile(
        rf"https://github\.com/{re.escape(repo)}/commit/({_SHA_RE})\b",
        re.IGNORECASE,
    )
    seen: dict[str, None] = {}
    for match in pattern.finditer(body or ""):
        seen.setdefault(match.group(1).lower(), None)
    return list(seen)


def is_bot(user: dict[str, Any]) -> bool:
    """True for GitHub Apps and bot accounts, which are never credited.

    ``type`` is authoritative, but the ``[bot]`` login suffix is checked too so a
    bot posting through a non-App account is still filtered.
    """
    if str(user.get("type", "")).lower() == "bot":
        return True
    return str(user.get("login", "")).lower().endswith("[bot]")


def is_maintainer(association: str | None) -> bool:
    """True when the PR author can push to the repo (so needs no credit)."""
    return str(association or "").upper() in MAINTAINER_ASSOCIATIONS


def choose_pull(pulls: list[dict[str, Any]], sha: str) -> dict[str, Any] | None:
    """Pick the pull request a commit actually merged from.

    ``/commits/{sha}/pulls`` can return several: the PR that merged the commit
    plus any still-open PR that also contains it (a stacked branch, a revert in
    progress). Prefer the one whose merge commit *is* this SHA, then any merged
    one, then the lowest number — the earliest PR is the one that introduced the
    commit.
    """
    if not pulls:
        return None
    ordered = sorted(pulls, key=lambda pull: int(pull.get("number", 0)))
    for pull in ordered:
        if str(pull.get("merge_commit_sha", "")).lower() == sha.lower():
            return pull
    for pull in ordered:
        if pull.get("merged_at"):
            return pull
    return ordered[0]


def collect_credits(
    api: GitHubAPI,
    body: str,
    repo: str,
    exclude: frozenset[str] = frozenset(),
) -> list[Credit]:
    """Resolve the changelog's commits to the outside contributors behind them.

    Commits with no associated pull request are skipped: on a protected branch
    those are direct maintainer pushes, and there is no author association to
    judge an unknown one by.
    """
    excluded = {login.lower() for login in exclude}
    by_login: dict[str, set[int]] = {}

    for sha in extract_commit_shas(body, repo):
        pulls = api.get(f"/repos/{repo}/commits/{sha}/pulls") or []
        pull = choose_pull(list(pulls), sha)
        if pull is None:
            continue

        user = pull.get("user") or {}
        login = str(user.get("login", ""))
        if not login or is_bot(user) or login.lower() in excluded:
            continue

        association = pull.get("author_association")
        if association is None:
            # Not every API shape carries the association inline; re-read the PR
            # rather than guess, so a maintainer is never credited by accident.
            full = api.get(f"/repos/{repo}/pulls/{int(pull['number'])}") or {}
            association = full.get("author_association")
        if is_maintainer(association):
            continue

        by_login.setdefault(login, set()).add(int(pull["number"]))

    credits = [
        Credit(login=login, pulls=tuple(sorted(pulls)))
        for login, pulls in by_login.items()
    ]
    credits.sort(key=lambda credit: (credit.first_pull, credit.login.lower()))
    return credits


def render_block(credits: list[Credit]) -> str:
    """Render the credits block, or an empty string when there is nothing to say."""
    if not credits:
        return ""
    lines = [BLOCK_START, "", BLOCK_HEADING, ""]
    for credit in credits:
        pulls = ", ".join(f"#{number}" for number in credit.pulls)
        lines.append(f"* @{credit.login} — {pulls}")
    lines += ["", BLOCK_END]
    return "\n".join(lines)


def update_body(body: str, block: str) -> str:
    """Return ``body`` with the credits block replaced by (or set to) ``block``.

    Any previously generated block is removed first, so this is idempotent:
    applying it to its own output changes nothing. An empty ``block`` just
    strips a stale block, which is what should happen when the last outside
    contribution drops out of a release.
    """
    normalized = (body or "").replace("\r\n", "\n")
    stripped = re.sub(
        rf"\n*{re.escape(BLOCK_START)}.*?{re.escape(BLOCK_END)}\n*",
        "\n",
        normalized,
        flags=re.DOTALL,
    )
    stripped = stripped.rstrip()
    if not block:
        return stripped
    if not stripped:
        return block
    return f"{stripped}\n\n{block}"


class _RestAPI:
    """Minimal GitHub REST client (stdlib only, no runner-side dependencies)."""

    def __init__(self, token: str, base_url: str = "https://api.github.com") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("PATCH", path, payload)

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "rtl-433-hass-release-credits")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request) as response:  # noqa: S310 - fixed https API
            raw = response.read().decode()
        return json.loads(raw) if raw else None


def find_release_pull(api: GitHubAPI, repo: str) -> dict[str, Any] | None:
    """Return the open release-please PR, or None when no release is pending."""
    pulls = api.get(f"/repos/{repo}/pulls?state=open&per_page=100") or []
    for pull in pulls:
        head_ref = str((pull.get("head") or {}).get("ref", ""))
        labels = {str(label.get("name", "")) for label in pull.get("labels") or []}
        if head_ref.startswith(RELEASE_BRANCH_PREFIX) or RELEASE_LABEL in labels:
            return pull
    return None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name (defaults to $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="PR number to update (defaults to the open release-please PR)",
    )
    parser.add_argument(
        "--exclude",
        default=os.environ.get("RELEASE_CREDITS_EXCLUDE", ""),
        help="comma-separated logins to treat as maintainers",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resulting body instead of updating the PR",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, api: GitHubAPI | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.repo:
        print("::error::--repo (or $GITHUB_REPOSITORY) is required", file=sys.stderr)
        return 2

    if api is None:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            print("::error::$GITHUB_TOKEN is required", file=sys.stderr)
            return 2
        api = _RestAPI(
            token, os.environ.get("GITHUB_API_URL", "https://api.github.com")
        )

    if args.pr is None:
        pull = find_release_pull(api, args.repo)
        if pull is None:
            print("No open release PR; nothing to credit.")
            return 0
    else:
        pull = api.get(f"/repos/{args.repo}/pulls/{args.pr}")

    number = int(pull["number"])
    body = pull.get("body") or ""
    exclude = frozenset(
        part.strip() for part in str(args.exclude).split(",") if part.strip()
    )

    credits = collect_credits(api, body, args.repo, exclude)
    new_body = update_body(body, render_block(credits))

    if args.dry_run:
        print(new_body)
        return 0

    if new_body == body:
        print(
            f"PR #{number}: credits already up to date ({len(credits)} contributor(s))."
        )
        return 0

    api.patch(f"/repos/{args.repo}/pulls/{number}", {"body": new_body})
    if credits:
        named = ", ".join(f"@{credit.login}" for credit in credits)
        print(f"PR #{number}: credited {named}.")
    else:
        print(f"PR #{number}: no outside contributors; removed stale credits block.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as error:  # pragma: no cover - network failure path
        print(f"::error::GitHub API {error.code}: {error.reason}", file=sys.stderr)
        raise SystemExit(1) from error
