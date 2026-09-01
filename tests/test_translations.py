"""Guard that every string a flow can render exists in ``translations/en.json``.

The most common failure mode for a new flow step is invisible to the flow tests:
Home Assistant happily renders an unknown abort reason, error key, step id or
menu option as the raw key, so a form whose title is missing shows the literal
``add_devices`` and an abort shows ``no_pending_devices``. Every driving test
still passes, because the flow result carries the key and the key is what the
test asserts on — the gap only shows up in the UI.

So this module reads the two flow modules' source instead of driving them, walks
the AST for the keys they can emit, and checks each one against ``en.json``. It
is deliberately source-driven rather than a hand-maintained list: a step added
without its translations fails here without anyone remembering to extend a
fixture.

``config_flow.py`` resolves under the ``config`` section of the file and
``options_flow.py`` under ``options``, matching how Home Assistant looks them up.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "rtl_433"

# mutmut runs the suite against a rewritten copy of the package in its
# ``mutants/`` sandbox, where every mutable function is expanded into an
# ``__mutmut_orig`` plus one ``__mutmut_N`` variant per mutation. Those variants
# include ``step_id=None``, ``reason=None`` and ``errors["base"] = None``, so the
# harvest below reads the flow as emitting keys built from something other than a
# literal and the dynamic-key guard fails before any mutant is even evaluated.
# The name mutmut prefixes onto every variant is the marker for that copy.
#
# Skipping there loses no coverage and makes the score more honest: this module
# reads source text and ``en.json`` rather than driving the flow, so it can kill
# no mutant on behaviour -- it could only ever "kill" one on the shape of the
# rewritten source.
_MUTMUT_MARKER = "__mutmut_"


@dataclass
class _FlowKeys:
    """The translation keys one flow module can render, by category."""

    abort: set[str] = field(default_factory=set)
    error: set[str] = field(default_factory=set)
    step: set[str] = field(default_factory=set)
    # Menu step id -> the option ids it offers, which live under that step's
    # ``menu_options`` rather than beside the other step keys.
    menu: dict[str, set[str]] = field(default_factory=dict)
    # Any key built from something other than a string literal. Such a key cannot
    # be checked here, so it is surfaced as a failure rather than skipped.
    dynamic: set[str] = field(default_factory=set)


def _literal(node: ast.expr | None) -> str | None:
    """Return a node's string value, or ``None`` when it is not a literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _source(module: str) -> str:
    """The flow module's source, as it sits on disk under this test run."""
    return (COMPONENT / module).read_text(encoding="utf-8")


def _collect(module: str, source: str) -> _FlowKeys:
    """Walk a flow module's AST for every translation key it can render."""
    found = _FlowKeys()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        # ``errors["base"] = "cannot_connect"`` -> an ``error`` key.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "errors"
                ):
                    key = _literal(node.value)
                    if key is None:
                        found.dynamic.add(f"{module}: non-literal errors[...] value")
                    else:
                        found.error.add(key)
            continue

        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}

        if node.func.attr == "async_abort":
            key = _literal(kwargs.get("reason"))
            if key is None:
                found.dynamic.add(f"{module}: non-literal async_abort reason")
            else:
                found.abort.add(key)
        elif node.func.attr in ("async_show_form", "async_show_menu"):
            step = _literal(kwargs.get("step_id"))
            if step is None:
                found.dynamic.add(f"{module}: non-literal step_id")
                continue
            found.step.add(step)
            options = kwargs.get("menu_options")
            if isinstance(options, ast.List):
                found.menu.setdefault(step, set()).update(
                    _literal(element) or "" for element in options.elts
                )
            elif options is not None:
                found.dynamic.add(f"{module}: non-literal menu_options")

    return found


@pytest.fixture(scope="module")
def translations() -> dict:
    """The shipped English translations, as Home Assistant loads them."""
    return json.loads(
        (COMPONENT / "translations" / "en.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("module", "section"),
    [("config_flow.py", "config"), ("options_flow.py", "options")],
)
def test_every_flow_key_has_an_english_string(module, section, translations):
    """Every abort, error, step and menu option the flow emits is translated.

    Asserted as whole-set differences so a failure names precisely the keys that
    are missing rather than the first one encountered.
    """
    source = _source(module)
    if _MUTMUT_MARKER in source:
        pytest.skip(
            "flow source rewritten by mutmut; this source-driven check runs in "
            "the normal pytest job only"
        )
    keys = _collect(module, source)
    strings = translations[section]

    assert keys.dynamic == set(), (
        "a flow key is built dynamically and cannot be checked here"
    )
    # Sanity: an empty harvest would make every assertion below vacuous.
    assert keys.abort and keys.error and keys.step

    assert keys.abort - set(strings.get("abort", {})) == set()
    assert keys.error - set(strings.get("error", {})) == set()
    assert keys.step - set(strings.get("step", {})) == set()
    for step, options in keys.menu.items():
        assert options - set(strings["step"][step].get("menu_options", {})) == set()


def test_the_approval_steps_carry_their_form_field_labels(translations):
    """The add / ignore / un-ignore selects are labelled, not left bare.

    The step-level guard above cannot see form field keys (they come from a
    voluptuous schema, not a literal argument), and these three are the fields
    the whole approval flow is operated through: an unlabelled multi-select is a
    list of devices with no way to tell which action it performs.
    """
    steps = translations["options"]["step"]
    assert set(steps["add_devices"]["data"]) == {"add", "ignore"}
    assert set(steps["ignored_devices"]["data"]) == {"unignore"}


def test_the_ignore_vocabulary_never_says_reject(translations):
    """The verb is Ignore / Ignored throughout, never "reject".

    Home Assistant calls this ignoring a discovery, and the plan fixes that verb
    deliberately: "reject" reads as a judgement on the device and does not match
    anything else in the UI.
    """
    assert "reject" not in json.dumps(translations).lower()
