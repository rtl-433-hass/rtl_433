"""Tests for ``scripts/release_credits.py`` — the release PR credits block.

``.github/workflows/release-credits.yml`` runs this script against the live
release PR with write access to its body, and there is no review step between
the script and the published release notes. The failure modes that matters are
therefore: crediting a maintainer (noise, and wrong), missing an outside
contributor (the thing the script exists to prevent), and stacking duplicate
blocks or rewriting an unchanged body (which re-notifies everyone mentioned on
every release push). Each has a test below, driven through a fake API so no
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
    # ``@dataclass`` resolves its annotations through ``sys.modules``, so the
    # module has to be registered before it executes, not after.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = _load_credits_module()


def _sha(seed: str) -> str:
    """A plausible 40-char commit SHA built from a short seed."""
    return (seed * 40)[:40]


def _body(*shas: str) -> str:
    """A release PR body shaped like release-please's, linking ``shas``."""
    entries = "\n".join(
        f"* some change ([{sha[:7]}](https://github.com/{_REPO}/commit/{sha}))"
        for sha in shas
    )
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
    body = _body(one, two, one)
    assert rc.extract_commit_shas(body, _REPO) == [one, two]


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


# --- collect_credits ---------------------------------------------------------


def test_credits_outside_contributors_and_skips_maintainers_and_bots():
    outside, mine, bot = _sha("a"), _sha("b"), _sha("c")
    api = FakeAPI(
        pulls_for_sha={
            outside: [_pull(208, "dimatx", "CONTRIBUTOR")],
            mine: [_pull(200, "deviantintegral", "MEMBER")],
            bot: [_pull(199, "renovate[bot]", "CONTRIBUTOR", user_type="Bot")],
        }
    )
    credits = rc.collect_credits(api, _body(outside, mine, bot), _REPO)
    assert [(credit.login, credit.pulls) for credit in credits] == [("dimatx", (208,))]


def test_groups_multiple_commits_and_pulls_per_contributor():
    first, second, third = _sha("a"), _sha("b"), _sha("d")
    api = FakeAPI(
        pulls_for_sha={
            first: [_pull(210, "dimatx")],
            second: [_pull(205, "dimatx")],
            third: [_pull(207, "someone-else")],
        }
    )
    credits = rc.collect_credits(api, _body(first, second, third), _REPO)
    # Ordered by each contributor's earliest PR, PRs ascending within a person.
    assert [(credit.login, credit.pulls) for credit in credits] == [
        ("dimatx", (205, 210)),
        ("someone-else", (207,)),
    ]


def test_commit_without_an_associated_pull_is_skipped():
    sha = _sha("a")
    api = FakeAPI(pulls_for_sha={sha: []})
    assert rc.collect_credits(api, _body(sha), _REPO) == []


def test_missing_association_is_re_read_rather_than_assumed():
    sha = _sha("a")
    inline = _pull(200, "deviantintegral")
    del inline["author_association"]
    api = FakeAPI(
        pulls_for_sha={sha: [inline]},
        pulls_by_number={200: _pull(200, "deviantintegral", "MEMBER")},
    )
    assert rc.collect_credits(api, _body(sha), _REPO) == []
    assert f"/repos/{_REPO}/pulls/200" in api.gets


def test_excluded_logins_are_treated_as_maintainers():
    sha = _sha("a")
    api = FakeAPI(pulls_for_sha={sha: [_pull(208, "DimaTX")]})
    credits = rc.collect_credits(api, _body(sha), _REPO, frozenset({"dimatx"}))
    assert credits == []


# --- rendering and body rewriting -------------------------------------------


def test_block_names_each_contributor_with_an_at_mention():
    block = rc.render_block(
        [rc.Credit("dimatx", (205, 210)), rc.Credit("someone-else", (207,))]
    )
    assert block.startswith(rc.BLOCK_START)
    assert block.endswith(rc.BLOCK_END)
    assert "* @dimatx — #205, #210" in block
    assert "* @someone-else — #207" in block


def test_no_contributors_renders_nothing():
    assert rc.render_block([]) == ""


def test_block_is_appended_after_the_generated_body():
    body = _body(_sha("a"))
    block = rc.render_block([rc.Credit("dimatx", (208,))])
    updated = rc.update_body(body, block)
    assert updated.startswith(":robot:")
    assert updated.endswith(block)


def test_rewriting_is_idempotent_rather_than_stacking_blocks():
    body = _body(_sha("a"))
    block = rc.render_block([rc.Credit("dimatx", (208,))])
    once = rc.update_body(body, block)
    twice = rc.update_body(once, block)
    assert twice == once
    assert twice.count(rc.BLOCK_START) == 1


def test_a_stale_block_is_replaced_not_duplicated():
    body = _body(_sha("a"))
    stale = rc.update_body(body, rc.render_block([rc.Credit("old-name", (1,))]))
    fresh = rc.update_body(stale, rc.render_block([rc.Credit("dimatx", (208,))]))
    assert "old-name" not in fresh
    assert "@dimatx" in fresh
    assert fresh.count(rc.BLOCK_START) == 1


def test_dropping_the_last_contributor_removes_the_block():
    body = _body(_sha("a"))
    with_block = rc.update_body(body, rc.render_block([rc.Credit("dimatx", (208,))]))
    assert rc.update_body(with_block, "") == body.rstrip()


def test_carriage_returns_are_normalized_so_reruns_compare_equal():
    body = _body(_sha("a"))
    block = rc.render_block([rc.Credit("dimatx", (208,))])
    once = rc.update_body(body, block)
    assert rc.update_body(once.replace("\n", "\r\n"), block) == once


# --- finding the release PR --------------------------------------------------


def test_release_pull_is_found_by_branch_prefix_or_label():
    by_branch = {"number": 210, "head": {"ref": "release-please--branches--main"}}
    by_label = {
        "number": 211,
        "head": {"ref": "something-else"},
        "labels": [{"name": "autorelease: pending"}],
    }
    other = {"number": 212, "head": {"ref": "feature"}, "labels": []}
    assert rc.find_release_pull(FakeAPI(open_pulls=[other, by_branch]), _REPO) == (
        by_branch
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


def test_main_writes_the_credits_block_to_the_release_pull():
    sha = _sha("a")
    api = FakeAPI(
        pulls_for_sha={sha: [_pull(208, "dimatx")]},
        open_pulls=[_release_pull(210, _body(sha))],
    )
    assert rc.main(["--repo", _REPO], api=api) == 0
    ((path, payload),) = api.patches
    assert path == f"/repos/{_REPO}/pulls/210"
    assert "@dimatx — #208" in payload["body"]


def test_main_does_not_rewrite_an_unchanged_body():
    sha = _sha("a")
    body = rc.update_body(_body(sha), rc.render_block([rc.Credit("dimatx", (208,))]))
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
    assert "@dimatx — #208" in capsys.readouterr().out


def test_main_requires_a_repository():
    assert rc.main(["--repo", ""], api=FakeAPI()) == 2
