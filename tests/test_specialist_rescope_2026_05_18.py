"""Phase 3 re-scope (2026-05-18) — verify the deterministic rule
panel surfaces in the LLM specialists' prompts AND verify the
re-scoped specialists no longer ask the LLM to re-derive facts.

The re-scope shifts five specialists from "derive observations
from the candidate" to "synthesize from the deterministic panel
the rule layer already produced":
  - pattern_recognizer
  - risk_assessor
  - sentiment_narrative
  - earnings_analyst
  - adversarial_reviewer
  - iv_skew_specialist (tweaked, kept core role)

Two unique specialists are intentionally NOT re-scoped:
  - gamma_pin_specialist (deterministic can't model gamma surface)
  - option_spread_risk (deterministic doesn't compute multi-leg Greeks)
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Dict

import pytest


def _mock_ctx(**overrides) -> Any:
    """A minimal ctx with the fields specialists tend to read."""
    defaults = dict(
        db_path=":memory:",
        market_regime="chop",
        display_name="Test",
        segment="medium",
        max_position_pct=0.10,
        enable_short_selling=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _candidate(**overrides) -> Dict[str, Any]:
    """A candidate that triggers several deterministic rules so the
    RULES suffix isn't empty in the rendered prompt."""
    base = {
        "symbol": "AAPL", "signal": "BUY", "price": 150.0, "score": 3,
        "reason": "RSI bullish + volume surge",
        # Indicators that trigger several rules
        "rsi": 68, "stoch_rsi": 55, "adx": 30, "mfi": 60,
        "cmf": 0.20, "roc_10": 6, "pct_from_vwap": 1.2,
        "pct_from_52w_high": -3.0, "gap_pct": 0.5, "volume_ratio": 3.5,
        "squeeze": 0, "nearest_fib_dist": 5.0, "atr_pct": 1.8,
        "alt_data": {
            "insider_cluster": {
                "is_cluster": True, "cluster_direction": "buying",
                "insider_count": 4, "total_value": 1_500_000,
            },
        },
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────
# Plumbing — _get_or_compute_panel caches on the candidate dict
# ─────────────────────────────────────────────────────────────────────

class TestPanelCaching:
    def test_panel_computed_once_per_candidate(self):
        """Multiple specialists asking for the panel only run the
        deterministic library ONCE per candidate. The cache hangs off
        the candidate dict (`_panel_verdicts`)."""
        from specialists._common import _get_or_compute_panel
        c = _candidate()
        ctx = _mock_ctx()
        v1 = _get_or_compute_panel(c, ctx)
        assert isinstance(v1, list)
        assert "_panel_verdicts" in c, (
            "Panel must be cached on the candidate dict after first call"
        )
        # Mutating the cache to a known sentinel verifies the second
        # call DOES read from cache instead of recomputing.
        c["_panel_verdicts"] = [{"name": "sentinel", "severity": "VETO",
                                   "reasoning": "test"}]
        v2 = _get_or_compute_panel(c, ctx)
        assert len(v2) == 1 and v2[0]["name"] == "sentinel"

    def test_failure_returns_empty_list(self, monkeypatch):
        """When the deterministic library fails (import error, rule
        explosion, etc.), the panel is an empty list — never None,
        never an exception."""
        from specialists import _common

        def boom(*a, **kw):
            raise RuntimeError("simulated failure")
        monkeypatch.setattr("deterministic_specialists.run_panel", boom)
        c = {"symbol": "X", "signal": "BUY"}
        ctx = _mock_ctx()
        v = _common._get_or_compute_panel(c, ctx)
        assert v == []
        assert c["_panel_verdicts"] == []


# ─────────────────────────────────────────────────────────────────────
# Rendering — the candidate line now carries a RULES suffix
# ─────────────────────────────────────────────────────────────────────

class TestPanelInRenderedCandidate:
    def test_render_includes_rules_suffix(self):
        from specialists._common import format_candidate_for_specialist
        c = _candidate()
        ctx = _mock_ctx()
        out = format_candidate_for_specialist(c, "pattern_recognizer", ctx=ctx)
        assert "RULES:" in out, (
            f"Rendered candidate must carry a RULES suffix when ctx provided. "
            f"Got: {out!r}"
        )

    def test_render_without_ctx_has_no_rules(self):
        """Backwards-compatible: callers that don't pass ctx get the
        old format without the RULES suffix."""
        from specialists._common import format_candidate_for_specialist
        c = _candidate()
        out = format_candidate_for_specialist(c, "pattern_recognizer", ctx=None)
        assert "RULES:" not in out

    def test_render_uses_short_severity_tag(self):
        """The compact format uses [V]/[C]/[C] single-char tags to
        keep the line tight even when many rules fire."""
        from specialists._common import format_candidate_for_specialist
        c = _candidate()
        ctx = _mock_ctx()
        out = format_candidate_for_specialist(c, "risk_assessor", ctx=ctx)
        # At least one of [V], [C] should appear (we triggered ≥1 CONFIRM)
        assert any(tag in out for tag in ("[V]", "[C]"))


# ─────────────────────────────────────────────────────────────────────
# Specialist prompts — the re-scoped five reference SYNTHESIZE
# ─────────────────────────────────────────────────────────────────────

# Which specialists were re-scoped to consume the panel in 2026-05-18.
# Each must (1) call candidates_block with ctx, (2) reference the
# RULES suffix in the prompt instruction, (3) ask for synthesis not
# fact-derivation.
_RESCOPED_SPECIALISTS = [
    "pattern_recognizer",
    "risk_assessor",
    "sentiment_narrative",
    "earnings_analyst",
    "adversarial_reviewer",
    "iv_skew_specialist",
]


@pytest.mark.parametrize("name", _RESCOPED_SPECIALISTS)
def test_rescoped_specialist_passes_ctx_to_candidates_block(name):
    """The re-scoped specialists MUST pass ctx through to
    candidates_block — otherwise the RULES suffix doesn't appear in
    the rendered candidate and the re-scope is silently broken."""
    import inspect
    mod = importlib.import_module(f"specialists.{name}")
    src = inspect.getsource(mod.build_prompt)
    assert "candidates_block(" in src
    assert "ctx=ctx" in src, (
        f"{name}.build_prompt must pass ctx=ctx into candidates_block "
        "so the deterministic panel verdicts surface in the prompt."
    )


@pytest.mark.parametrize("name", _RESCOPED_SPECIALISTS)
def test_rescoped_specialist_prompt_references_rules(name):
    """The re-scoped prompts must reference the new RULES suffix so
    the LLM knows what the [V]/[C] tags mean."""
    mod = importlib.import_module(f"specialists.{name}")
    cands = [_candidate()]
    prompt = mod.build_prompt(cands, _mock_ctx())
    assert "RULES" in prompt, (
        f"{name} prompt must reference the RULES suffix conventions "
        "so the LLM can interpret the deterministic verdicts."
    )


@pytest.mark.parametrize("name", [
    "pattern_recognizer", "risk_assessor", "sentiment_narrative",
    "earnings_analyst",
])
def test_rescoped_specialist_asks_for_synthesis(name):
    """The non-VETO-authority re-scoped specialists must explicitly
    instruct the LLM to SYNTHESIZE rather than re-derive."""
    mod = importlib.import_module(f"specialists.{name}")
    prompt = mod.build_prompt([_candidate()], _mock_ctx())
    # Look for synthesis-vocabulary
    has_synthesis_keyword = any(kw in prompt.lower() for kw in (
        "synthesize", "synthesis", "weave", "weighted"
    ))
    assert has_synthesis_keyword, (
        f"{name} prompt must explicitly ask the LLM to synthesize from "
        "rule verdicts, not re-derive facts."
    )


# ─────────────────────────────────────────────────────────────────────
# Untouched specialists — confirm they're NOT re-scoped (sanity check)
# ─────────────────────────────────────────────────────────────────────

class TestUntouchedSpecialists:
    """gamma_pin_specialist and option_spread_risk cover unique
    territory the deterministic library doesn't subsume. They're
    intentionally NOT re-scoped — this test pins that decision."""

    def test_gamma_pin_specialist_still_uses_old_api(self):
        from specialists import gamma_pin_specialist as mod
        assert hasattr(mod, "build_prompt")
        # No assertion about ctx — gamma_pin can use or not use it,
        # the contract is "still functional, not blocked by re-scope"
        out = mod.build_prompt([_candidate()], _mock_ctx())
        assert isinstance(out, str) and len(out) > 100

    def test_option_spread_risk_still_uses_old_api(self):
        from specialists import option_spread_risk as mod
        assert hasattr(mod, "build_prompt")
        out = mod.build_prompt([_candidate()], _mock_ctx())
        assert isinstance(out, str) and len(out) > 100
