#!/usr/bin/env python3
"""Credit outside contributors inline on the release PR's changelog lines.

release-please writes a changelog whose entries link to commits, never to the
people who wrote them, so a community contribution lands in the release notes
anonymously. This helper closes that gap: it reads the open release PR, resolves
every commit the changelog links to back to the pull request it merged from, and
appends the author's name to that entry's own line when they are not a
maintainer or a bot::

    * mark detect_wet as event_driven ([2eddd52](...)) (thanks [dimatx](...)!)

Run by ``.github/workflows/release-credits.yml`` after every Release run, so the
credits are refreshed whenever release-please rewrites the PR body.

**Credits are deliberately not ``@mentions``.** An ``@login`` in a PR body is a
GitHub mention, which is exactly the thing that notifies people, so the default
credit is a plain Markdown link to the contributor's profile: same visible
credit, clickable, but nothing GitHub's mention parser reacts to. ``--mention``
switches to a real ``@login`` for anyone who *wants* the notification. Nothing
here ever posts a comment, and editing a PR body does not notify its subscribers,
so the default run is silent.

Only the PR description is touched, so ``CHANGELOG.md`` keeps release-please's
own wording. Note that the *published GitHub Release* notes are built by
release-please from the merged release PR's body, so whatever credits are on it
at merge time do carry through to the release — which is why ``--mention`` is
not the default: an ``@login`` there would notify.

"Maintainer" is decided by the pull request's GitHub ``author_association``:
``OWNER``, ``MEMBER`` and ``COLLABORATOR`` are maintainers (people who can push
to the repo), everyone else is an outside contributor worth crediting. Bots are
never credited. Extra logins can be excluded with ``--exclude``.

A credit already on a line is replaced rather than repeated, so reruns are
idempotent, and a body that would not change is not written back at all.

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

# release-please names its branch ``release-please--branches--<base>`` and
# labels the PR ``autorelease: pending``. Either is enough to identify it; both
# are checked so a label rename upstream does not silently disable this.
RELEASE_BRANCH_PREFIX = "release-please--"
RELEASE_LABEL = "autorelease: pending"

PROFILE_URL = "https://github.com"

_SHA_RE = r"[0-9a-f]{7,40}"

# Matches a credit this script appended, in either style, at the end of a line.
# It is how a rerun finds and replaces its own previous output, so it has to keep
# matching what ``render_credit`` writes — including the styles it no longer
# writes by default, or switching --mention on and off would leave both behind.
CREDIT_SUFFIX_RE = re.compile(
    r"\s*\(thanks (?:@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?|\[[^\]]+\]\([^)]*\))!\)\s*$"
)


class GitHubAPI(Protocol):
    """The slice of the GitHub REST API this script needs."""

    def get(self, path: str) -> Any:
        """GET ``path`` (repo-relative) and return the decoded JSON body."""

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        """PATCH ``path`` (repo-relative) with ``payload`` as JSON."""


def _commit_link_re(repo: str) -> re.Pattern[str]:
    """Match this repo's commit links.

    Only links to ``repo`` count: a changelog can quote another project's commit
    (an upstream fix, a dependency bump), and resolving those against this repo
    would 404 or, worse, hit an unrelated commit that happens to share a prefix.
    """
    return re.compile(
        rf"https://github\.com/{re.escape(repo)}/commit/({_SHA_RE})\b",
        re.IGNORECASE,
    )


def extract_commit_shas(body: str, repo: str) -> list[str]:
    """Return the commit SHAs ``body`` links to, in first-seen order."""
    seen: dict[str, None] = {}
    for match in _commit_link_re(repo).finditer(body or ""):
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


def resolve_credits(
    api: GitHubAPI,
    body: str,
    repo: str,
    exclude: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Map each linked commit SHA to the outside contributor who wrote it.

    Commits with no associated pull request are left out: on a protected branch
    those are direct maintainer pushes, and there is no author association to
    judge an unknown one by.
    """
    excluded = {login.lower() for login in exclude}
    credits: dict[str, str] = {}

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

        credits[sha] = login

    return credits


def render_credit(login: str, mention: bool = False) -> str:
    """Render the trailing credit for one changelog line.

    The default links to the profile instead of writing ``@login``: a mention is
    what generates a notification, and this script is meant to be silent.
    """
    who = f"@{login}" if mention else f"[{login}]({PROFILE_URL}/{login})"
    return f" (thanks {who}!)"


def annotate_body(
    body: str,
    repo: str,
    credits: dict[str, str],
    mention: bool = False,
) -> str:
    """Return ``body`` with each changelog line credited to its contributor.

    A line is matched to a contributor through the commit it links to, so the
    credit lands on the entry it belongs to rather than in a summary at the end.
    Any credit already present is stripped first, which makes this idempotent and
    also clears a stale one when a line's authorship no longer qualifies.
    """
    link_re = _commit_link_re(repo)
    lines = []
    for raw_line in (body or "").replace("\r\n", "\n").split("\n"):
        line = CREDIT_SUFFIX_RE.sub("", raw_line)
        # Every link on the line is considered, not just the first: an entry that
        # cites a second commit (a revert, a follow-up) would otherwise lose its
        # credit to whichever link happened to come first.
        login = next(
            (
                credited
                for match in link_re.finditer(line)
                if (credited := credits.get(match.group(1).lower()))
            ),
            None,
        )
        lines.append(line + render_credit(login, mention) if login else line)
    return "\n".join(lines)


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
    """Return the open release-please PR, or None when no release is pending.

    Sorted by most-recently-updated because only the first page is read: the
    release PR is rewritten on every Release run, so it is always near the top,
    whereas the default (newest-created first) would push a long-lived release PR
    off the page on a repo carrying 100+ open dependency PRs.
    """
    pulls = (
        api.get(
            f"/repos/{repo}/pulls?state=open&per_page=100&sort=updated&direction=desc"
        )
        or []
    )
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
        "--mention",
        action="store_true",
        help="credit with @login instead of a profile link (this notifies them)",
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
        if not pull:
            print(f"::error::PR #{args.pr} not found in {args.repo}", file=sys.stderr)
            return 1

    number = int(pull["number"])
    # Normalized up front so the "did the body change?" check below compares like
    # with like: GitHub hands back CRLF for any body a human has touched in the
    # web UI, and ``annotate_body`` emits LF, which would otherwise look like a
    # change on every single run.
    body = (pull.get("body") or "").replace("\r\n", "\n")
    exclude = frozenset(
        part.strip() for part in str(args.exclude).split(",") if part.strip()
    )

    credits = resolve_credits(api, body, args.repo, exclude)
    new_body = annotate_body(body, args.repo, credits, args.mention)
    named = ", ".join(sorted(set(credits.values()), key=str.lower))

    if args.dry_run:
        print(new_body)
        return 0

    if new_body == body:
        print(f"PR #{number}: credits already up to date ({named or 'none'}).")
        return 0

    api.patch(f"/repos/{args.repo}/pulls/{number}", {"body": new_body})
    if credits:
        print(f"PR #{number}: credited {named}.")
    else:
        print(f"PR #{number}: no outside contributors; cleared stale credits.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as error:  # pragma: no cover - network failure path
        print(f"::error::GitHub API {error.code}: {error.reason}", file=sys.stderr)
        raise SystemExit(1) from error
