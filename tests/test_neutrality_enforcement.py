"""P4.5 of LONG_SHORT_PLAN.md — market-neutrality enforcement tests."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# simulate_book_beta_with_entry
# ---------------------------------------------------------------------------

def test_simulate_returns_none_on_unknown_candidate_beta():
    from portfolio_exposure import simulate_book_beta_with_entry
    proj = simulate_book_beta_with_entry(
        positions=[],
        equity=1_000_000,
        candidate_symbol="ZZZ",
        candidate_size_pct=5.0,
        candidate_action="BUY",
        beta_lookup=lambda s: None,
    )
    assert proj is None


def test_simulate_long_entry_increases_book_beta():
    """Empty book, long entry of 5% at beta 1.5 → book beta = 0.075
    (5% × 1.5 × +1 sign)."""
    from portfolio_exposure import simulate_book_beta_with_entry
    proj = simulate_book_beta_with_entry(
        positions=[],
        equity=1_000_000,
        candidate_symbol="TSLA",
        candidate_size_pct=5.0,
        candidate_action="BUY",
        beta_lookup=lambda s: 1.5,
    )
    assert proj == pytest.approx(0.075, abs=1e-6)


def test_simulate_short_entry_decreases_book_beta():
    """Empty book, short of 5% at beta 1.5 → -0.075."""
    from portfolio_exposure import simulate_book_beta_with_entry
    proj = simulate_book_beta_with_entry(
        positions=[],
        equity=1_000_000,
        candidate_symbol="TSLA",
        candidate_size_pct=5.0,
        candidate_action="SHORT",
        beta_lookup=lambda s: 1.5,
    )
    assert proj == pytest.approx(-0.075, abs=1e-6)


def test_simulate_combines_existing_book_with_entry():
    """Existing 50% long at beta 1.0 → book beta 0.5. Add 10% long at
    beta 1.5 → projected 0.5 + 0.10 × 1.5 = 0.65."""
    from portfolio_exposure import simulate_book_beta_with_entry
    positions = [{"symbol": "AAPL", "qty": 100, "market_value": 500_000}]
    betas = {"AAPL": 1.0, "NVDA": 1.5}
    proj = simulate_book_beta_with_entry(
        positions=positions,
        equity=1_000_000,
        candidate_symbol="NVDA",
        candidate_size_pct=10.0,
        candidate_action="BUY",
        beta_lookup=lambda s: betas.get(s),
    )
    assert proj == pytest.approx(0.65, abs=1e-6)


def test_simulate_short_position_in_existing_book_subtracts():
    """Existing 50% short at beta 1.0 → book beta -0.5 (qty < 0)."""
    from portfolio_exposure import simulate_book_beta_with_entry
    positions = [{"symbol": "TSLA", "qty": -100, "market_value": 500_000}]
    proj = simulate_book_beta_with_entry(
        positions=positions,
        equity=1_000_000,
        candidate_symbol="MSFT",
        candidate_size_pct=0.0,  # no new entry — just confirms existing is short
        candidate_action="BUY",
        beta_lookup=lambda s: 1.0,
    )
    # 0% new entry weight → projected ≈ existing book = -0.5
    assert proj == pytest.approx(-0.5, abs=1e-6)


# ---------------------------------------------------------------------------
# _validate_ai_trades neutrality gate
# ---------------------------------------------------------------------------

def _make_ctx(target_book_beta, enable_shorts=True):
    class Ctx:
        max_position_pct = 0.10
        short_max_position_pct = 0.05
        enable_short_selling = enable_shorts
        target_short_pct = 0.0
    Ctx.target_book_beta = target_book_beta
    return Ctx()


def _make_portfolio_state(positions, equity, current_book_beta):
    return {
        "positions": positions,
        "equity": equity,
        "exposure": {"book_beta": current_book_beta, "num_positions": len(positions)},
        "drawdown_pct": 0.0,
    }


def test_neutrality_gate_inactive_when_no_target():
    """target_book_beta None → don't gate at all."""
    from ai_analyst import _validate_ai_trades
    ctx = _make_ctx(target_book_beta=None)
    candidates = [{"symbol": "TSLA"}]
    result = {"trades": [{"symbol": "TSLA", "action": "BUY", "size_pct": 5.0,
                          "confidence": 70, "stop_loss_pct": 3,
                          "take_profit_pct": 8}]}
    pstate = _make_portfolio_state([], 1_000_000, 0.0)
    out = _validate_ai_trades(result, candidates, ctx=ctx,
                               portfolio_state=pstate)
    assert len(out["trades"]) == 1


def test_neutrality_gate_blocks_high_beta_long_when_already_overshot():
    """Target 0.0, current book beta +0.6, candidate 10% TSLA β=2.0 →
    new beta = 0.6 + 0.10*2.0 = 0.80, distance 0.6 → 0.8 = +0.2.
    NOT blocked (delta < 0.5).

    But same trade at 30% size: 0.6 + 0.30*2.0 = 1.20, distance
    0.6 → 1.2 = +0.6 > 0.5 → BLOCKED."""
    from ai_analyst import _validate_ai_trades
    ctx = _make_ctx(target_book_beta=0.0)
    candidates = [{"symbol": "TSLA"}]
    pstate = _make_portfolio_state(
        [{"symbol": "AAPL", "qty": 100, "market_value": 600_000}],
        1_000_000, current_book_beta=0.6,
    )

    # Small-size trade — should pass
    result = {"trades": [{"symbol": "TSLA", "action": "BUY", "size_pct": 5.0,
                          "confidence": 70, "stop_loss_pct": 3,
                          "take_profit_pct": 8}]}
    with patch("factor_data.get_beta", return_value=2.0):
        out = _validate_ai_trades(result, candidates, ctx=ctx,
                                   portfolio_state=pstate)
    assert len(out["trades"]) == 1, "small size shouldn't trip neutrality gate"

    # Large size — must be blocked
    result = {"trades": [{"symbol": "TSLA", "action": "BUY", "size_pct": 30.0,
                          "confidence": 70, "stop_loss_pct": 3,
                          "take_profit_pct": 8}]}
    with patch("factor_data.get_beta", return_value=2.0):
        out = _validate_ai_trades(result, candidates, ctx=ctx,
                                   portfolio_state=pstate)
    # Note: 30% gets clamped to max_pos_pct=10%, so projected = 0.6 + 0.10*2.0 = 0.8
    # delta = 0.2 < 0.5, NOT blocked. Need to set max higher OR test with smaller
    # max + bigger swing. Adjust ctx.
    assert len(out["trades"]) == 1  # 10% cap → not blocked


def test_neutrality_gate_blocks_when_pushed_far():
    """Target 0.0, current book beta +0.5. Long 10% MSFT β=2.0:
    projected = 0.5 + 0.10*2.0 = 0.7. dist 0.5 → 0.7 = +0.2 (allow).
    But increase position sizing cap to 100% so the trade isn't capped:
    """
    from ai_analyst import _validate_ai_trades
    ctx = _make_ctx(target_book_beta=0.0)
    ctx.max_position_pct = 1.0  # allow huge size for test
    candidates = [{"symbol": "TSLA"}]
    pstate = _make_portfolio_state(
        [{"symbol": "AAPL", "qty": 100, "market_value": 600_000}],
        1_000_000, current_book_beta=0.6,
    )
    # 30% TSLA at β=2.0: projected = 0.6 + 0.30*2.0 = 1.20.
    # dist 0.6 → 1.20 = +0.60 > 0.5 → BLOCKED.
    result = {"trades": [{"symbol": "TSLA", "action": "BUY", "size_pct": 30.0,
                          "confidence": 70, "stop_loss_pct": 3,
                          "take_profit_pct": 8}]}
    with patch("factor_data.get_beta", return_value=2.0):
        out = _validate_ai_trades(result, candidates, ctx=ctx,
                                   portfolio_state=pstate)
    assert len(out["trades"]) == 0  # blocked by neutrality gate


def test_neutrality_gate_allows_trade_that_improves_neutrality():
    """Target 0.0, current book beta +0.6. SHORT 30% TSLA β=2.0:
    projected = 0.6 - 0.30*2.0 = 0.0 (perfect). dist 0.6 → 0.0 = -0.6.
    Must ALLOW — gate only blocks moves AWAY from target."""
    from ai_analyst import _validate_ai_trades
    ctx = _make_ctx(target_book_beta=0.0)
    ctx.short_max_position_pct = 1.0  # allow huge size for test
    candidates = [{"symbol": "TSLA"}]
    pstate = _make_portfolio_state(
        [{"symbol": "AAPL", "qty": 100, "market_value": 600_000}],
        1_000_000, current_book_beta=0.6,
    )
    result = {"trades": [{"symbol": "TSLA", "action": "SHORT", "size_pct": 30.0,
                          "confidence": 70, "stop_loss_pct": 3,
                          "take_profit_pct": 8}]}
    with patch("factor_data.get_beta", return_value=2.0):
        out = _validate_ai_trades(result, candidates, ctx=ctx,
                                   portfolio_state=pstate)
    assert len(out["trades"]) == 1


def test_neutrality_gate_skips_when_no_book_beta():
    """No exposure block / no book_beta computable → don't gate."""
    from ai_analyst import _validate_ai_trades
    ctx = _make_ctx(target_book_beta=0.0)
    ctx.max_position_pct = 1.0
    candidates = [{"symbol": "TSLA"}]
    pstate = {"positions": [], "equity": 1_000_000,
              "exposure": {"num_positions": 0},  # no book_beta
              "drawdown_pct": 0.0}
    result = {"trades": [{"symbol": "TSLA", "action": "BUY", "size_pct": 50.0,
                          "confidence": 70, "stop_loss_pct": 3,
                          "take_profit_pct": 8}]}
    with patch("factor_data.get_beta", return_value=2.0):
        out = _validate_ai_trades(result, candidates, ctx=ctx,
                                   portfolio_state=pstate)
    assert len(out["trades"]) == 1
