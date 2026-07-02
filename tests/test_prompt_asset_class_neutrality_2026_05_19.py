"""AI prompt asset-class neutrality contract.

2026-05-19. Operator principle: *"the system is not supposed to be
biased one way or another towards any market — stocks, options, later
on crypto, fx etc. may be added, but one should not 'win' over the
other."*

Pre-this-commit the prompt had asymmetric framing:
- Stocks: "take as-is / adjust / or propose your own" (inviting)
- Options: "propose for any candidate whose…" (conditional)
- Multileg: "only propose when…" (restrictive)
PLUS an explicit "Do NOT default to options" line that tilted toward
stocks. The AI silently preferred stocks even when option spreads
offered better defined-risk economics on the same candidates.

Tests pin the new contract:
  - Multileg note does NOT use restrictive framing
  - Options note uses parallel "take / adjust / propose" language
  - The "Do NOT default to options" anti-bias line is gone
  - All three action types are described as "first-class"
  - No action type is described as "default" or "preferred"
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _make_ctx():
    return SimpleNamespace(
        ai_provider="google",
        segment="stocks",
        max_position_pct=0.05,
        max_total_positions=10,
        enable_short_selling=True,
        enable_options=True,
    )


def _make_candidate_with_options(symbol="AAPL"):
    """A candidate that triggers options_action_enabled AND
    multileg_action_enabled in the prompt builder."""
    return {
        "symbol": symbol,
        "price": 150.0,
        "signal": "STRONG_BUY",
        "score": 0.8,
        "rsi": 60, "volume_ratio": 1.2, "atr": 1.0, "adx": 25,
        "stoch_rsi": 50, "roc_10": 1.0, "pct_from_52w_high": 0.05,
        "mfi": 50, "cmf": 0, "squeeze": 0, "pct_from_vwap": 0,
        "nearest_fib_dist": 99, "gap_pct": 0,
        "options_oracle_summary": (
            f"{symbol} IV rank 72 (rich); skew 1.05 (neutral); "
            "DTE 30; GEX positive"
        ),
    }


class TestNoRestrictiveMultilegFraming:
    """Catches the regression where MULTILEG_OPEN was framed as an
    exception ('only propose when…'). New framing must use the same
    inviting verbs as the stock action."""

    def test_multileg_note_does_not_use_only_propose_when(self, monkeypatch):
        # Stub the strategy-advisor calls to force multileg block render
        from unittest.mock import patch
        from ai_analyst import _build_batch_prompt

        # Force the ledger to surface an option row so the MULTILEG_OPEN
        # note renders (P2b: the note is gated on ledger_has_options).
        cands = [_make_candidate_with_options()]
        with patch("opportunity_ledger.build_opportunities", return_value=[]), \
             patch("opportunity_ledger.render_ledger_block",
                   return_value=(
                       "RISK-ADJUSTED OPPORTUNITY LEDGER\n"
                       "   1  +0.30   62%  $8,000  $2,400   AAPL bull_put_spread",
                       True)):
            prompt = _build_batch_prompt(
                cands,
                portfolio_state={"positions": [], "drawdown_pct": 0.0,
                                  "account": {"equity": 100000}},
                market_context={"regime": "neutral"},
                ctx=_make_ctx(),
            )
        # The restrictive phrase must not appear
        assert "only propose when" not in prompt.lower(), (
            "MULTILEG_OPEN note re-introduced restrictive 'only propose "
            "when' framing. Must use 'take as-is / adjust / propose' "
            "language matching other action types — see operator's "
            "asset-class-neutrality principle (memory entry "
            "feedback_trade_and_make_money_not_hoard)."
        )


class TestNoStockBiasInHeader:
    """The header used to contain 'Do NOT default to options' which
    explicitly tilted toward stocks. That phrase must not return."""

    def test_header_does_not_contain_stock_biased_phrasing(self):
        from ai_analyst import _build_batch_prompt
        prompt = _build_batch_prompt(
            [_make_candidate_with_options()],
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=_make_ctx(),
        )
        # Old anti-bias phrasing was actively biasing against options.
        # If any of these strings come back, it's a regression.
        forbidden = [
            "do not default to options",
            "do not default to multileg",
            "stock buy/short setups are first-class trades",
            "simple stock action may have better risk/reward",
        ]
        prompt_lower = prompt.lower()
        for phrase in forbidden:
            assert phrase not in prompt_lower, (
                f"Stock-biased phrase {phrase!r} re-appeared in prompt. "
                f"Operator's principle: no asset class wins over another. "
                f"See feedback_trade_and_make_money_not_hoard memory."
            )


class TestEqualFirstClassFraming:
    """The prompt must describe every action type as first-class.
    'First-class' is the load-bearing word — if it appears only for
    stocks or only for options, the bias is back."""

    def test_all_three_actions_described_as_first_class(self):
        from unittest.mock import patch
        from ai_analyst import _build_batch_prompt
        cands = [_make_candidate_with_options()]
        with patch("opportunity_ledger.build_opportunities", return_value=[]), \
             patch("opportunity_ledger.render_ledger_block",
                   return_value=(
                       "RISK-ADJUSTED OPPORTUNITY LEDGER\n"
                       "   1  +0.30   62%  $8,000  $2,400   AAPL bull_put_spread",
                       True)):
            prompt = _build_batch_prompt(
                cands,
                portfolio_state={"positions": [], "drawdown_pct": 0.0,
                                  "account": {"equity": 100000}},
                market_context={"regime": "neutral"},
                ctx=_make_ctx(),
            )
        # The phrase "first-class" should refer to ALL action types
        # collectively or appear neutrally — never only for stocks.
        # Count: if "first-class" appears multiple times, it should
        # not be only-for-stocks.
        # Stricter check: the explicit "all action types are equal
        # first-class trades" header must be present.
        assert "all action types are equal first-class" in prompt.lower(), (
            "The 'all action types equal first-class trades' header "
            "is the load-bearing assertion of asset-class neutrality. "
            "If it's missing, the prompt may have drifted back to "
            "asymmetric framing."
        )


class TestParallelStructureAcrossActions:
    """The 'take as-is / adjust / propose your own' invitation must
    appear for every action type with that language available, not
    just for stocks. Catches re-introduction of asymmetric verbs."""

    def test_options_note_uses_inviting_verbs(self):
        from unittest.mock import patch
        from ai_analyst import _build_batch_prompt
        cands = [_make_candidate_with_options()]
        with patch("opportunity_ledger.build_opportunities", return_value=[]), \
             patch("opportunity_ledger.render_ledger_block",
                   return_value=("", False)):  # no ledger option rows this test
            prompt = _build_batch_prompt(
                cands,
                portfolio_state={"positions": [], "drawdown_pct": 0.0,
                                  "account": {"equity": 100000}},
                market_context={"regime": "neutral"},
                ctx=_make_ctx(),
            )
        # The single-leg OPTIONS section should use the same inviting
        # verbs as stocks (take/adjust/propose)
        # Look at the OPTIONS-specific section
        # 2026-06-10 — header gained "ONLY" when multileg-under-
        # OPTIONS proposals were rejected at the parse layer; match
        # the prefix so the inviting-verbs pin survives wording
        # hardening around the single-leg constraint.
        options_section_start = prompt.find("OPTIONS (single-leg")
        assert options_section_start >= 0, (
            "Could not find 'OPTIONS (single-leg' section header in "
            "prompt — the equalised options_note language is missing."
        )
        # Check the OPTIONS section paragraph (~next 500 chars)
        options_section = prompt[options_section_start:
                                  options_section_start + 800].lower()
        assert "take them as-is" in options_section, (
            "OPTIONS section must invite 'take as-is' (parallel to stocks)"
        )
        assert "adjust" in options_section
        assert "propose" in options_section
