"""Guardrails for profile_classification — the single source of truth
that keeps experiment CONTROL profiles (buy_hold / random) out of the
system's aggregate metrics (dashboard overview, /performance "All System
Profiles", /ai-performance).

test-for-the-class, not the instance: the load-bearing property is that
ANY non-'ai' strategy_type classifies as a baseline, so a control type
added in the future is excluded from the system aggregates AUTOMATICALLY,
the day it's added — not when someone remembers to update an allowlist.
We assert that structural property, not just the three names that exist
today. (Matches the feedback rule: allowlists trap leaks from new data.)
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from profile_classification import (  # noqa: E402
    AI_STRATEGY_TYPE,
    is_baseline_profile,
    is_baseline_strategy,
)

_VIEWS = os.path.join(os.path.dirname(__file__), os.pardir, "views.py")


# ---------------------------------------------------------------------------
# Classifier behaviour
# ---------------------------------------------------------------------------

def test_known_baselines_are_baselines():
    for st in ("buy_hold", "random"):
        assert is_baseline_strategy(st) is True


def test_ai_is_not_a_baseline():
    assert is_baseline_strategy(AI_STRATEGY_TYPE) is False
    assert is_baseline_strategy("ai") is False


def test_missing_or_blank_defaults_to_ai_not_baseline():
    # The column default is 'ai'; an absent value must NOT be misread as a
    # control (that would silently drop a real AI profile from aggregates).
    for st in (None, "", "   "):
        assert is_baseline_strategy(st) is False


def test_case_and_whitespace_insensitive():
    assert is_baseline_strategy("AI") is False
    assert is_baseline_strategy(" ai ") is False
    assert is_baseline_strategy("Buy_Hold") is True
    assert is_baseline_strategy(" random ") is True


def test_unknown_future_control_is_treated_as_baseline():
    """THE class-level invariant: a NEW control type nobody enumerated
    must be excluded from system metrics automatically. If this ever
    fails because someone switched to an allowlist of known names, a
    future baseline would silently pollute the system aggregate."""
    for st in ("buy_hold_qqq", "momentum_baseline", "equal_weight",
               "spy_2x", "whatever_control_2027"):
        assert is_baseline_strategy(st) is True, (
            f"{st!r} is non-'ai' and must classify as a baseline so it "
            "cannot leak into the system aggregate. Classify by 'not ai', "
            "never by an allowlist of known names."
        )


def test_is_baseline_profile_reads_strategy_type():
    assert is_baseline_profile({"strategy_type": "random"}) is True
    assert is_baseline_profile({"strategy_type": "ai"}) is False
    assert is_baseline_profile({}) is False  # missing → default ai


# ---------------------------------------------------------------------------
# Structural guard: the aggregate routes must USE the classifier, so a
# future refactor can't silently drop the baseline exclusion.
# ---------------------------------------------------------------------------

def _function_source(tree, name):
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, f"{name} not found in views.py"
    return ast.get_source_segment(_views_src(), fn) or ""


_VIEWS_SRC_CACHE = {}


def _views_src():
    if "src" not in _VIEWS_SRC_CACHE:
        with open(_VIEWS) as fh:
            _VIEWS_SRC_CACHE["src"] = fh.read()
    return _VIEWS_SRC_CACHE["src"]


def test_aggregate_routes_filter_baselines():
    """`performance_dashboard` and `ai_performance_legacy` build a
    cross-profile aggregate when no single profile is selected. Each must
    reference the baseline classifier so controls are excluded — pinned so
    the exclusion can't be removed without this test failing."""
    tree = ast.parse(_views_src())
    for route_fn in ("performance_dashboard", "ai_performance_legacy"):
        src = _function_source(tree, route_fn)
        assert "is_baseline_profile" in src, (
            f"{route_fn} no longer references is_baseline_profile — the "
            "no-selection aggregate would include buy_hold/random control "
            "profiles, polluting the system metrics. Re-add the filter."
        )
