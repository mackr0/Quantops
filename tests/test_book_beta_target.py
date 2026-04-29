"""P4.1 of LONG_SHORT_PLAN.md — beta-targeted portfolio construction."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# compute_book_beta
# ---------------------------------------------------------------------------

def test_book_beta_none_when_no_positions():
    from portfolio_exposure import compute_book_beta
    assert compute_book_beta([], equity=100_000) is None


def test_book_beta_none_when_zero_equity():
    from portfolio_exposure import compute_book_beta
    positions = [{"symbol": "AAPL", "qty": 100, "market_value": 20_000}]
    assert compute_book_beta(positions, equity=0,
                              beta_lookup=lambda s: 1.1) is None


def test_book_beta_long_only_book():
    """Long $30K AAPL (β=1.1) on $100K equity → book beta = +0.33."""
    from portfolio_exposure import compute_book_beta
    positions = [{"symbol": "AAPL", "qty": 100, "market_value": 30_000}]
    book_beta = compute_book_beta(positions, equity=100_000,
                                    beta_lookup=lambda s: 1.1)
    assert book_beta == pytest.approx(0.33, abs=0.001)


def test_book_beta_short_subtracts_from_book():
    """Short positions contribute NEGATIVELY to book beta because
    their P&L moves opposite to the underlying."""
    from portfolio_exposure import compute_book_beta
    positions = [
        {"symbol": "AAPL", "qty": 100, "market_value": 30_000},   # long, β=1.1
        {"symbol": "TSLA", "qty": -100, "market_value": -20_000},  # short, β=2.0
    ]
    betas = {"AAPL": 1.1, "TSLA": 2.0}
    book_beta = compute_book_beta(positions, equity=100_000,
                                    beta_lookup=lambda s: betas[s])
    # 0.30 * 1.1 - 0.20 * 2.0 = 0.33 - 0.40 = -0.07
    assert book_beta == pytest.approx(-0.07, abs=0.001)


def test_book_beta_market_neutral_book_lands_near_zero():
    """Long β=1.0 + short β=1.0 of the same notional should net to ~0."""
    from portfolio_exposure import compute_book_beta
    positions = [
        {"symbol": "L", "qty": 100, "market_value": 50_000},
        {"symbol": "S", "qty": -100, "market_value": -50_000},
    ]
    book_beta = compute_book_beta(positions, equity=100_000,
                                    beta_lookup=lambda s: 1.0)
    assert abs(book_beta) < 0.01


def test_book_beta_skips_positions_with_unknown_beta():
    """Positions with no beta data don't drop into the math (would
    distort), but the function still works on the rest."""
    from portfolio_exposure import compute_book_beta
    positions = [
        {"symbol": "GOOD", "qty": 100, "market_value": 30_000},
        {"symbol": "UNK",  "qty": 100, "market_value": 30_000},
    ]
    def lookup(s):
        return 1.5 if s == "GOOD" else None
    book_beta = compute_book_beta(positions, equity=100_000,
                                    beta_lookup=lookup)
    # Only GOOD contributes: 0.30 * 1.5 = 0.45
    assert book_beta == pytest.approx(0.45, abs=0.001)


def test_book_beta_none_when_all_unknown():
    from portfolio_exposure import compute_book_beta
    positions = [{"symbol": "UNK", "qty": 100, "market_value": 30_000}]
    book_beta = compute_book_beta(positions, equity=100_000,
                                    beta_lookup=lambda s: None)
    assert book_beta is None


def test_book_beta_in_compute_exposure_output():
    from portfolio_exposure import compute_exposure
    positions = [{"symbol": "AAPL", "qty": 100, "market_value": 30_000}]
    out = compute_exposure(
        positions, equity=100_000,
        sector_lookup=lambda s: "tech",
    )
    assert "book_beta" in out


# ---------------------------------------------------------------------------
# AI prompt directive
# ---------------------------------------------------------------------------

class _StubCtx:
    enable_short_selling = False
    max_position_pct = 0.10
    max_total_positions = 10
    segment = "midcap"
    db_path = None
    signal_weights = "{}"
    prompt_layout = "{}"
    short_max_position_pct = 0.05
    target_short_pct = 0.0
    target_book_beta = None


def _market_ctx():
    return {"vix": 18, "regime": "bullish",
            "sector_rotation": {}, "macro_data": {}}


def _state_with_book_beta(book_beta_value, num_positions=5):
    return {
        "equity": 100_000, "cash": 50_000,
        "num_positions": num_positions, "positions": [],
        "drawdown_pct": 0.0, "drawdown_action": "normal",
        "exposure": {
            "net_pct": 30, "gross_pct": 30, "num_positions": num_positions,
            "by_sector": {"tech": {"long_pct": 30, "short_pct": 0,
                                     "n_long": 5, "n_short": 0,
                                     "net_pct": 30, "gross_pct": 30}},
            "factors": {},
            "book_beta": book_beta_value,
        },
    }


def test_book_beta_directive_absent_when_no_target():
    """target_book_beta=None (default) → no directive in prompt."""
    from ai_analyst import _build_batch_prompt
    ctx = _StubCtx()
    state = _state_with_book_beta(book_beta_value=1.5)
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_market_ctx(), ctx=ctx,
    )
    assert "BOOK-BETA TARGET" not in prompt


def test_book_beta_directive_present_when_target_set():
    from ai_analyst import _build_batch_prompt
    ctx = _StubCtx()
    ctx.target_book_beta = 0.5
    state = _state_with_book_beta(book_beta_value=1.5)  # 1.0 above target
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_market_ctx(), ctx=ctx,
    )
    assert "BOOK-BETA TARGET" in prompt
    assert "BETA TOO HIGH" in prompt


def test_book_beta_directive_says_too_low_when_book_below_target():
    from ai_analyst import _build_batch_prompt
    ctx = _StubCtx()
    ctx.target_book_beta = 0.8
    state = _state_with_book_beta(book_beta_value=0.1)  # 0.7 below target
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_market_ctx(), ctx=ctx,
    )
    assert "BETA TOO LOW" in prompt


def test_book_beta_directive_on_target_within_tolerance():
    from ai_analyst import _build_batch_prompt
    ctx = _StubCtx()
    ctx.target_book_beta = 0.5
    state = _state_with_book_beta(book_beta_value=0.45)  # within 0.30 tolerance
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_market_ctx(), ctx=ctx,
    )
    assert "BOOK-BETA TARGET" in prompt
    assert "on target" in prompt.lower()


def test_book_beta_directive_skipped_when_book_empty():
    from ai_analyst import _build_batch_prompt
    ctx = _StubCtx()
    ctx.target_book_beta = 0.5
    state = {
        "equity": 100_000, "cash": 100_000, "num_positions": 0,
        "positions": [], "drawdown_pct": 0.0, "drawdown_action": "normal",
    }
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_market_ctx(), ctx=ctx,
    )
    assert "BOOK-BETA TARGET" not in prompt


def test_user_context_default_target_book_beta_is_none():
    from user_context import UserContext
    ctx = UserContext(user_id=1, segment="midcap")
    assert ctx.target_book_beta is None
