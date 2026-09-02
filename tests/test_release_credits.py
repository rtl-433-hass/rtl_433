"""Tests for ``scripts/release_credits.py`` — inline release PR credits.

``.github/workflows/release-credits.yml`` runs this script against the live
release PR with write access to its body, and there is no review step between
the script and the published release notes. The failure modes that matter are
therefore: crediting a maintainer (noise, and wrong), missing an outside
contributor (the thing the script exists to prevent), duplicating a credit on
rerun, and — the explicit requirement — writing an ``@mention``, which is what
would notify people. Each has a test below, driven through a fake API so no
network is involved.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release_credits.py"
_REPO = "rtl-433-hass/rtl_433"

# mutmut copies only the package, tests/, and pyproject into its ``mutants/``
# sandbox — not scripts/ — so this meta-test cannot load the script there. It
# adds no mutation coverage anyway (it exercises no package source), so skip the
# module in that environment; the normal pytest job runs it in full.
if not _SCRIPT.is_file():
    pytest.skip(
        "scripts/release_credits.py absent (mutmut sandbox); this meta-test "
        "runs in the normal pytest job only",
        allow_module_level=True,
    )


def _load_credits_module():
    """Load the standalone script (it lives in ``scripts/``, not a package)."""
    spec = importlib.util.spec_from_file_location("release_credits", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Annotations are resolved through ``sys.modules``, so the module has to be
    # registered before it executes, not after.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = _load_credits_module()


def _sha(seed: str) -> str:
    """A plausible 40-char commit SHA built from a short seed."""
    return (seed * 40)[:40]


def _entry(sha: str, subject: str = "some change") -> str:
    """One changelog bullet shaped like release-please's."""
    return f"* {subject} ([{sha[:7]}](https://github.com/{_REPO}/commit/{sha}))"


def _body(*shas: str) -> str:
    """A release PR body shaped like release-please's, linking ``shas``."""
    entries = "\n".join(_entry(sha) for sha in shas)
    return (
        ":robot: I have created a release *beep* *boop*\n---\n\n\n"
        f"## [0.21.0](https://github.com/{_REPO}/compare/v0.20.1...v0.21.0)\n\n\n"
        f"### Features\n\n{entries}\n\n---\n"
        "This PR was generated with [Release Please]"
        "(https://github.com/googleapis/release-please)."
    )


class FakeAPI:
    """Stands in for the GitHub REST API, recording what would be written."""

    def __init__(
        self,
        pulls_for_sha: dict[str, list[dict[str, Any]]] | None = None,
        pulls_by_number: dict[int, dict[str, Any]] | None = None,
        open_pulls: list[dict[str, Any]] | None = None,
    ) -> None:
        self.pulls_for_sha = pulls_for_sha or {}
        self.pulls_by_number = pulls_by_number or {}
        self.open_pulls = open_pulls or []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[str] = []

    def get(self, path: str) -> Any:
        self.gets.append(path)
        if path.startswith(f"/repos/{_REPO}/commits/"):
            sha = path.split("/commits/")[1].split("/")[0]
            return self.pulls_for_sha.get(sha, [])
        if path.startswith(f"/repos/{_REPO}/pulls?"):
            return self.open_pulls
        if path.startswith(f"/repos/{_REPO}/pulls/"):
            return self.pulls_by_number[int(path.rsplit("/", 1)[1])]
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        self.patches.append((path, payload))
        return {}


def _pull(
    number: int,
    login: str,
    association: str = "CONTRIBUTOR",
    user_type: str = "User",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "number": number,
        "user": {"login": login, "type": user_type},
        "author_association": association,
        "merged_at": "2026-09-01T17:38:49Z",
        **extra,
    }


# --- extract_commit_shas -----------------------------------------------------


def test_extracts_linked_shas_in_order_without_duplicates():
    one, two = _sha("a"), _sha("b")
    assert rc.extract_commit_shas(_body(one, two, one), _REPO) == [one, two]


def test_ignores_commit_links_to_other_repositories():
    mine, theirs = _sha("a"), _sha("c")
    body = (
        f"* mine (https://github.com/{_REPO}/commit/{mine})\n"
        f"* upstream (https://github.com/merbanan/rtl_433/commit/{theirs})\n"
    )
    assert rc.extract_commit_shas(body, _REPO) == [mine]


def test_ignores_compare_links_that_are_not_commits():
    body = f"## [0.21.0](https://github.com/{_REPO}/compare/v0.20.1...v0.21.0)"
    assert rc.extract_commit_shas(body, _REPO) == []


def test_empty_body_yields_no_shas():
    assert rc.extract_commit_shas("", _REPO) == []


# --- who counts as a maintainer ---------------------------------------------


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_push_capable_associations_are_maintainers(association):
    assert rc.is_maintainer(association) is True


@pytest.mark.parametrize(
    "association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", "", None]
)
def test_everyone_else_is_an_outside_contributor(association):
    assert rc.is_maintainer(association) is False


def test_bots_are_detected_by_type_and_by_login_suffix():
    assert rc.is_bot({"login": "renovate[bot]", "type": "Bot"}) is True
    assert rc.is_bot({"login": "some-app[bot]", "type": "User"}) is True
    assert rc.is_bot({"login": "dimatx", "type": "User"}) is False


# --- choose_pull -------------------------------------------------------------


def test_prefers_the_pull_whose_merge_commit_is_this_sha():
    sha = _sha("a")
    open_pr = _pull(9, "someone", merged_at=None)
    merged_pr = _pull(7, "dimatx", merge_commit_sha=sha)
    assert rc.choose_pull([open_pr, merged_pr], sha)["number"] == 7


def test_falls_back_to_the_merged_pull_then_to_the_lowest_number():
    sha = _sha("a")
    unmerged_low = _pull(3, "someone", merged_at=None)
    merged_high = _pull(8, "dimatx")
    assert rc.choose_pull([unmerged_low, merged_high], sha)["number"] == 8
    assert (
        rc.choose_pull([unmerged_low, _pull(9, "x", merged_at=None)], sha)["number"]
        == 3
    )
    assert rc.choose_pull([], sha) is None


# --- resolve_credits ---------------------------------------------------------


def test_credits_outside_contributors_and_skips_maintainers_and_bots():
    outside, mine, bot = _sha("a"), _sha("b"), _sha("c")
    api = FakeAPI(
        pulls_for_sha={
            outside: [_pull(208, "dimatx", "CONTRIBUTOR")],
            mine: [_pull(200, "deviantintegral", "MEMBER")],
            bot: [_pull(199, "renovate[bot]", "CONTRIBUTOR", user_type="Bot")],
        }
    )
    credits = rc.resolve_credits(api, _body(outside, mine, bot), _REPO)
    assert credits == {outside: "dimatx"}


def test_commit_without_an_associated_pull_is_skipped():
    sha = _sha("a")
    assert rc.resolve_credits(FakeAPI(pulls_for_sha={sha: []}), _body(sha), _REPO) == {}


def test_missing_association_is_re_read_rather_than_assumed():
    sha = _sha("a")
    inline = _pull(200, "deviantintegral")
    del inline["author_association"]
    api = FakeAPI(
        pulls_for_sha={sha: [inline]},
        pulls_by_number={200: _pull(200, "deviantintegral", "MEMBER")},
    )
    assert rc.resolve_credits(api, _body(sha), _REPO) == {}
    assert f"/repos/{_REPO}/pulls/200" in api.gets


def test_excluded_logins_are_treated_as_maintainers():
    sha = _sha("a")
    api = FakeAPI(pulls_for_sha={sha: [_pull(208, "DimaTX")]})
    assert rc.resolve_credits(api, _body(sha), _REPO, frozenset({"dimatx"})) == {}


# --- inline annotation -------------------------------------------------------


def test_credit_is_appended_to_the_line_of_its_own_commit():
    mine, theirs = _sha("a"), _sha("b")
    annotated = rc.annotate_body(_body(mine, theirs), _REPO, {theirs: "dimatx"})
    lines = annotated.split("\n")
    assert [line for line in lines if "thanks" in line] == [
        _entry(theirs) + " (thanks [dimatx](https://github.com/dimatx)!)"
    ]
    # The maintainer's own line is left exactly as release-please wrote it.
    assert _entry(mine) in lines


def test_default_credit_is_a_profile_link_and_never_an_at_mention():
    sha = _sha("a")
    annotated = rc.annotate_body(_body(sha), _REPO, {sha: "dimatx"})
    assert "https://github.com/dimatx" in annotated
    # An "@" anywhere in the body is the thing that would notify someone.
    assert "@" not in annotated


def test_mention_style_is_opt_in():
    sha = _sha("a")
    annotated = rc.annotate_body(_body(sha), _REPO, {sha: "dimatx"}, mention=True)
    assert "(thanks @dimatx!)" in annotated


def test_annotating_twice_does_not_repeat_the_credit():
    sha = _sha("a")
    credits = {sha: "dimatx"}
    once = rc.annotate_body(_body(sha), _REPO, credits)
    twice = rc.annotate_body(once, _REPO, credits)
    assert twice == once
    assert once.count("thanks") == 1


def test_switching_between_styles_replaces_rather_than_stacks():
    sha = _sha("a")
    credits = {sha: "dimatx"}
    linked = rc.annotate_body(_body(sha), _REPO, credits)
    mentioned = rc.annotate_body(linked, _REPO, credits, mention=True)
    assert mentioned.count("thanks") == 1
    assert "(thanks @dimatx!)" in mentioned
    assert rc.annotate_body(mentioned, _REPO, credits) == linked


def test_a_stale_credit_is_replaced_with_the_current_author():
    sha = _sha("a")
    stale = rc.annotate_body(_body(sha), _REPO, {sha: "old-name"})
    fresh = rc.annotate_body(stale, _REPO, {sha: "dimatx"})
    assert "old-name" not in fresh
    assert "dimatx" in fresh
    assert fresh.count("thanks") == 1


def test_dropping_a_contributor_clears_the_credit_from_the_line():
    sha = _sha("a")
    body = _body(sha)
    credited = rc.annotate_body(body, _REPO, {sha: "dimatx"})
    assert rc.annotate_body(credited, _REPO, {}) == body


def test_lines_without_a_commit_link_are_untouched():
    body = "### Features\n\nsome prose\n"
    assert rc.annotate_body(body, _REPO, {_sha("a"): "dimatx"}) == body


def test_carriage_returns_are_normalized_so_reruns_compare_equal():
    sha = _sha("a")
    credits = {sha: "dimatx"}
    once = rc.annotate_body(_body(sha), _REPO, credits)
    assert rc.annotate_body(once.replace("\n", "\r\n"), _REPO, credits) == once


# --- finding the release PR --------------------------------------------------


def test_release_pull_is_found_by_branch_prefix_or_label():
    by_branch = {"number": 210, "head": {"ref": "release-please--branches--main"}}
    by_label = {
        "number": 211,
        "head": {"ref": "something-else"},
        "labels": [{"name": "autorelease: pending"}],
    }
    other = {"number": 212, "head": {"ref": "feature"}, "labels": []}
    assert (
        rc.find_release_pull(FakeAPI(open_pulls=[other, by_branch]), _REPO) == by_branch
    )
    assert (
        rc.find_release_pull(FakeAPI(open_pulls=[other, by_label]), _REPO) == by_label
    )
    assert rc.find_release_pull(FakeAPI(open_pulls=[other]), _REPO) is None


# --- main --------------------------------------------------------------------


def _release_pull(number: int, body: str) -> dict[str, Any]:
    return {
        "number": number,
        "body": body,
        "head": {"ref": "release-please--branches--main"},
        "labels": [{"name": "autorelease: pending"}],
    }


def test_main_writes_inline_credits_to_the_release_pull():
    sha = _sha("a")
    api = FakeAPI(
        pulls_for_sha={sha: [_pull(208, "dimatx")]},
        open_pulls=[_release_pull(210, _body(sha))],
    )
    assert rc.main(["--repo", _REPO], api=api) == 0
    ((path, payload),) = api.patches
    assert path == f"/repos/{_REPO}/pulls/210"
    assert "(thanks [dimatx](https://github.com/dimatx)!)" in payload["body"]
    assert "@" not in payload["body"]


def test_main_does_not_rewrite_an_unchanged_body():
    sha = _sha("a")
    body = rc.annotate_body(_body(sha), _REPO, {sha: "dimatx"})
    api = FakeAPI(
        pulls_for_sha={sha: [_pull(208, "dimatx")]},
        open_pulls=[_release_pull(210, body)],
    )
    assert rc.main(["--repo", _REPO], api=api) == 0
    assert api.patches == []


def test_main_is_a_no_op_when_no_release_pull_is_open():
    api = FakeAPI(open_pulls=[{"number": 1, "head": {"ref": "feature"}, "labels": []}])
    assert rc.main(["--repo", _REPO], api=api) == 0
    assert api.patches == []


def test_main_dry_run_prints_the_body_and_writes_nothing(capsys):
    sha = _sha("a")
    api = FakeAPI(
        pulls_for_sha={sha: [_pull(208, "dimatx")]},
        pulls_by_number={210: _release_pull(210, _body(sha))},
    )
    assert rc.main(["--repo", _REPO, "--pr", "210", "--dry-run"], api=api) == 0
    assert api.patches == []
    assert "(thanks [dimatx](https://github.com/dimatx)!)" in capsys.readouterr().out


def test_main_requires_a_repository():
    assert rc.main(["--repo", ""], api=FakeAPI()) == 2


def test_credit_lands_on_the_credited_commit_when_a_line_links_two():
    """A line citing a second commit (a revert, a follow-up) keeps its credit.

    Only the *first* link used to be consulted, so an entry whose credited commit
    was cited second silently lost its contributor.
    """
    first, second = _sha("b"), _sha("c")
    line = (
        f"* revert ([{first[:7]}](https://github.com/{_REPO}/commit/{first}))"
        f" reverts ([{second[:7]}](https://github.com/{_REPO}/commit/{second}))"
    )
    annotated = rc.annotate_body(line, _REPO, {second: "dimatx"})
    assert annotated == line + " (thanks [dimatx](https://github.com/dimatx)!)"


def test_main_does_not_rewrite_a_crlf_body_that_is_already_credited():
    """GitHub hands back CRLF for any body a human has touched in the web UI.

    ``annotate_body`` emits LF, so without normalizing the body first it looks
    changed on every run and the workflow PATCHes it — and logs a fresh credit —
    each time the Release workflow completes.
    """
    sha = _sha("a")
    body = rc.annotate_body(_body(sha), _REPO, {sha: "dimatx"}).replace("\n", "\r\n")
    api = FakeAPI(
        pulls_for_sha={sha: [_pull(208, "dimatx")]},
        open_pulls=[_release_pull(210, body)],
    )
    assert rc.main(["--repo", _REPO], api=api) == 0
    assert api.patches == []


def test_main_fails_loudly_when_the_requested_pull_does_not_exist():
    """``--pr`` on a number the API has nothing for must not crash on ``None``."""

    class MissingPullAPI(FakeAPI):
        def get(self, path: str) -> Any:
            return None

    api = MissingPullAPI()
    assert rc.main(["--repo", _REPO, "--pr", "999"], api=api) == 1
    assert api.patches == []
