"""Phase 3 of the instrument-class pipeline refactor (2026-05-11).

Phase 3 forks the AI prompt: stock candidates and option candidates
get DIFFERENT prompts. Stock prompt has only stock features; option
prompt has IV/Greeks/DTE/strike/spread economics alongside the
underlying's technicals. Closes audit finding #4 by construction.

Pins:
1. Stock prompt does NOT include option-specific feature keys
   (defense-in-depth: even if a stock candidate's extras leak an
   option key, the prompt builder strips it).
2. Option prompt DOES include IV rank, DTE, strike, Greeks when
   the candidate's extras carry them.
3. Option prompt orders option features FIRST so the AI sees them
   before the underlying's technicals.
4. Pipeline `build_prompt()` returns a non-empty string for
   non-empty candidates, and a clean empty-cycle message for
   empty candidate lists.
5. Class invariant: the stock prompt builder NEVER mentions any
   option-specific term (IV, Greeks, DTE, strike) regardless of
   what's in the candidate extras.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines import Candidate
from pipelines.stock import StockPipeline
from pipelines.option import OptionPipeline
from pipelines import stock_prompt, option_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stock_candidate(**extras):
    return Candidate(
        symbol="AAPL", score=0.85, signal="STRONG_BUY", price=150.0,
        extra={"rsi": 45, "sector_momentum": "rising", **extras},
    )


def _option_candidate(**extras):
    base_extras = {
        "iv_rank": 78, "dte": 32, "strike": 26.0,
        "spread_max_loss": 230, "spread_max_gain": 270,
        "delta": -0.35, "gamma": 0.08, "theta": -0.12,
        "rsi": 55,   # underlying technical, also surfaced
    }
    base_extras.update(extras)
    return Candidate(
        symbol="CWAN", score=0.82, signal="MULTILEG_OPEN",
        price=24.50, extra=base_extras,
    )


def _ctx():
    return SimpleNamespace(segment="Mid Cap")


# ---------------------------------------------------------------------------
# Stock prompt — option-key exclusion
# ---------------------------------------------------------------------------

class TestStockPromptExcludesOptionKeys:
    def test_stock_prompt_does_not_include_option_keys_when_clean(self):
        prompt = stock_prompt.build_prompt(_ctx(), [_stock_candidate()])
        # No option-specific terms appear
        for term in ("iv_rank", "delta", "gamma", "theta", "dte",
                     "strike", "spread_max_loss"):
            assert term not in prompt, (
                f"Stock prompt unexpectedly mentions {term!r}"
            )

    def test_stock_prompt_strips_leaked_option_keys(self):
        """Defense-in-depth: even if a buggy stock-candidate generator
        leaks IV rank into a stock candidate's extras, the prompt
        builder MUST strip it before the AI sees it."""
        leaky = _stock_candidate(iv_rank=99, delta=-0.42, dte=14)
        prompt = stock_prompt.build_prompt(_ctx(), [leaky])
        assert "iv_rank" not in prompt
        assert "delta" not in prompt
        assert "dte" not in prompt
        # But the legitimate stock features survive
        assert "rsi" in prompt
        assert "sector_momentum" in prompt

    def test_stock_prompt_lists_each_candidate(self):
        c1 = _stock_candidate()
        c2 = Candidate(symbol="MSFT", score=0.7, signal="BUY",
                       price=400.0, extra={"rsi": 60})
        prompt = stock_prompt.build_prompt(_ctx(), [c1, c2])
        assert "AAPL" in prompt
        assert "MSFT" in prompt

    def test_stock_prompt_empty_candidates(self):
        prompt = stock_prompt.build_prompt(_ctx(), [])
        assert prompt
        assert "no" in prompt.lower() or "empty" in prompt.lower()


# ---------------------------------------------------------------------------
# Option prompt — option-key inclusion + ordering
# ---------------------------------------------------------------------------

class TestOptionPromptIncludesOptionContext:
    def test_option_prompt_includes_iv_greeks_dte(self):
        prompt = option_prompt.build_prompt(_ctx(), [_option_candidate()])
        # Option-specific context surfaces
        for term in ("iv_rank", "dte", "strike", "spread_max_loss",
                     "spread_max_gain", "delta", "gamma", "theta"):
            assert term in prompt, (
                f"Option prompt missing required term {term!r}"
            )

    def test_option_prompt_orders_option_features_first(self):
        """In each candidate's rendered JSON, option-specific keys
        should appear BEFORE underlying technicals so the AI's
        attention is anchored on option economics."""
        prompt = option_prompt.build_prompt(_ctx(), [_option_candidate()])
        iv_pos = prompt.find("iv_rank")
        rsi_pos = prompt.find("rsi")
        assert iv_pos > 0
        assert rsi_pos > 0
        assert iv_pos < rsi_pos, (
            "Option features should be ordered before the "
            "underlying's technicals"
        )

    def test_option_prompt_includes_underlying_technicals_too(self):
        """Option AI still wants underlying context (where the stock
        is). Both option features and underlying technicals appear,
        in the right order."""
        prompt = option_prompt.build_prompt(_ctx(), [_option_candidate()])
        assert "rsi" in prompt   # underlying technical preserved

    def test_option_prompt_empty_candidates(self):
        prompt = option_prompt.build_prompt(_ctx(), [])
        assert prompt
        assert "no" in prompt.lower() or "empty" in prompt.lower()

    def test_option_prompt_handles_missing_option_keys_gracefully(self):
        """If the candidate's extras don't yet have IV/Greeks (the
        upstream feature pipeline isn't wired in this phase), the
        prompt must still render the underlying technicals without
        crashing."""
        partial = Candidate(
            symbol="CWAN", score=0.5, signal="MULTILEG_OPEN",
            price=24.50, extra={"rsi": 50},
        )
        prompt = option_prompt.build_prompt(_ctx(), [partial])
        assert "CWAN" in prompt
        assert "rsi" in prompt


# ---------------------------------------------------------------------------
# Class invariant — stock prompt builder NEVER outputs option terms
# regardless of what's in candidate extras
# ---------------------------------------------------------------------------

class TestStockPromptOptionKeyBlocklistInvariant:
    """Property-level invariant: for any candidate extras, the
    rendered stock prompt contains zero mentions of any option-
    specific feature key. Catches future regressions where a new
    option feature is added but the stock prompt's blocklist isn't
    updated."""

    @pytest.mark.parametrize("leak_key,leak_value", [
        ("iv_rank", 80),
        ("delta", -0.42),
        ("gamma", 0.08),
        ("theta", -0.05),
        ("vega", 0.12),
        ("dte", 14),
        ("strike", 200.0),
        ("spread_max_loss", 500),
        ("spread_max_gain", 250),
        ("option_strategy", "bull_put_spread"),
        ("occ_symbol", "AAPL260612P00200000"),
    ])
    def test_each_known_option_key_is_stripped(self, leak_key, leak_value):
        c = _stock_candidate(**{leak_key: leak_value})
        prompt = stock_prompt.build_prompt(_ctx(), [c])
        assert leak_key not in prompt, (
            f"Stock prompt leaked option key {leak_key!r} into "
            f"the rendered output"
        )


# ---------------------------------------------------------------------------
# Pipeline integration — build_prompt() wires through
# ---------------------------------------------------------------------------

class TestPipelineBuildPromptWiring:
    def test_stock_pipeline_build_prompt_uses_stock_module(self):
        prompt = StockPipeline().build_prompt(
            _ctx(), [_stock_candidate(iv_rank=80)],   # leaked iv
        )
        assert "iv_rank" not in prompt   # stripped at the boundary

    def test_option_pipeline_build_prompt_uses_option_module(self):
        prompt = OptionPipeline().build_prompt(
            _ctx(), [_option_candidate()],
        )
        assert "iv_rank" in prompt
        assert "spread_max_loss" in prompt
