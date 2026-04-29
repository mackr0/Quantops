"""P2.2 of LONG_SHORT_PLAN.md — target_short_pct balance directive."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class StubCtx:
    """Minimal ctx for _build_batch_prompt tests."""
    enable_short_selling = True
    max_position_pct = 0.10
    max_total_positions = 10
    segment = "small"
    db_path = None
    signal_weights = "{}"
    prompt_layout = "{}"
    short_max_position_pct = 0.05
    target_short_pct = 0.0


def _empty_market_context():
    return {"vix": 18, "regime": "bullish",
            "sector_rotation": {}, "macro_data": {}}


def _portfolio_state_with_exposure(net_pct, gross_pct, num_positions,
                                    short_pct_by_sector=0.0):
    """Build a portfolio_state matching what trade_pipeline produces.
    short_pct_by_sector lets us simulate having shorts in the book.
    """
    by_sector = {}
    long_pct = gross_pct - short_pct_by_sector
    if long_pct > 0:
        by_sector["Technology"] = {
            "long_pct": long_pct, "short_pct": 0.0,
            "net_pct": long_pct, "gross_pct": long_pct,
            "n_long": 5, "n_short": 0,
        }
    if short_pct_by_sector > 0:
        by_sector["Energy"] = {
            "long_pct": 0.0, "short_pct": short_pct_by_sector,
            "net_pct": -short_pct_by_sector, "gross_pct": short_pct_by_sector,
            "n_long": 0, "n_short": 2,
        }
    return {
        "equity": 100_000, "cash": 50_000,
        "num_positions": num_positions,
        "positions": [],
        "drawdown_pct": 0.0, "drawdown_action": "normal",
        "exposure": {
            "net_pct": net_pct,
            "gross_pct": gross_pct,
            "num_positions": num_positions,
            "by_sector": by_sector,
            "concentration_flags": [],
        },
    }


def test_target_block_absent_when_target_is_zero():
    """Profiles with target_short_pct=0 (long-only) should not see
    a balance directive in the prompt."""
    from ai_analyst import _build_batch_prompt
    ctx = StubCtx()
    ctx.target_short_pct = 0.0  # long-only
    state = _portfolio_state_with_exposure(net_pct=20, gross_pct=20,
                                            num_positions=5)
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_empty_market_context(),
        ctx=ctx,
    )
    assert "LONG/SHORT BALANCE TARGET" not in prompt


def test_target_block_present_when_target_above_zero_and_book_has_exposure():
    from ai_analyst import _build_batch_prompt
    ctx = StubCtx()
    ctx.target_short_pct = 0.5  # balanced
    state = _portfolio_state_with_exposure(net_pct=20, gross_pct=20,
                                            num_positions=5)
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_empty_market_context(),
        ctx=ctx,
    )
    assert "LONG/SHORT BALANCE TARGET" in prompt
    # All-long book vs 50% target → undershorted
    assert "UNDERSHORTED" in prompt


def test_target_block_says_overshorted_when_short_share_above_target():
    from ai_analyst import _build_batch_prompt
    ctx = StubCtx()
    ctx.target_short_pct = 0.2  # mostly long
    # Book: 5% long + 15% short = gross 20%; short share 75% — way above target 20%
    state = _portfolio_state_with_exposure(net_pct=-10, gross_pct=20,
                                            num_positions=5,
                                            short_pct_by_sector=15)
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_empty_market_context(),
        ctx=ctx,
    )
    assert "OVERSHORTED" in prompt


def test_target_block_says_on_target_when_within_tolerance():
    from ai_analyst import _build_batch_prompt
    ctx = StubCtx()
    ctx.target_short_pct = 0.5
    # Book: 10% long + 10% short = gross 20%; short share = 50% = target
    state = _portfolio_state_with_exposure(net_pct=0, gross_pct=20,
                                            num_positions=4,
                                            short_pct_by_sector=10)
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_empty_market_context(),
        ctx=ctx,
    )
    assert "LONG/SHORT BALANCE TARGET" in prompt
    assert "Balance is on target" in prompt


def test_target_block_skipped_when_no_open_positions():
    from ai_analyst import _build_batch_prompt
    ctx = StubCtx()
    ctx.target_short_pct = 0.5
    # No exposure → don't render the directive (no baseline to compare)
    state = {
        "equity": 100_000, "cash": 100_000, "num_positions": 0,
        "positions": [], "drawdown_pct": 0.0, "drawdown_action": "normal",
    }
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_empty_market_context(),
        ctx=ctx,
    )
    assert "LONG/SHORT BALANCE TARGET" not in prompt


def test_target_block_skipped_when_shorts_disabled():
    from ai_analyst import _build_batch_prompt
    ctx = StubCtx()
    ctx.enable_short_selling = False
    ctx.target_short_pct = 0.5  # nonsense value; should be ignored
    state = _portfolio_state_with_exposure(net_pct=20, gross_pct=20,
                                            num_positions=5)
    prompt = _build_batch_prompt(
        candidates_data=[{"symbol": "AAPL", "price": 200, "signal": "BUY",
                          "score": 3, "rsi": 55, "volume_ratio": 1.0}],
        portfolio_state=state, market_context=_empty_market_context(),
        ctx=ctx,
    )
    assert "LONG/SHORT BALANCE TARGET" not in prompt


def test_balance_gate_pass_when_no_target():
    from portfolio_exposure import balance_gate
    assert balance_gate(target_short_pct=0.0,
                         current_exposure={"gross_pct": 50, "by_sector": {}}) == "pass"


def test_balance_gate_pass_when_no_exposure():
    from portfolio_exposure import balance_gate
    assert balance_gate(target_short_pct=0.5, current_exposure=None) == "pass"


def test_balance_gate_blocks_longs_when_undershorted():
    """target=50% short, currently 0% short → undershorted by 50% → block longs."""
    from portfolio_exposure import balance_gate
    exposure = {
        "gross_pct": 30,
        "by_sector": {"Tech": {"long_pct": 30, "short_pct": 0}},
    }
    assert balance_gate(target_short_pct=0.5, current_exposure=exposure) == "block_longs"


def test_balance_gate_blocks_shorts_when_overshorted():
    """target=20% short, currently ~67% short → overshorted by 47% → block shorts."""
    from portfolio_exposure import balance_gate
    exposure = {
        "gross_pct": 30,
        "by_sector": {"Tech": {"long_pct": 10, "short_pct": 20}},
    }
    assert balance_gate(target_short_pct=0.2, current_exposure=exposure) == "block_shorts"


def test_balance_gate_pass_within_tolerance():
    """target=50%, current=40% (10% delta < 25% threshold) → pass."""
    from portfolio_exposure import balance_gate
    exposure = {
        "gross_pct": 100,
        "by_sector": {
            "Tech": {"long_pct": 60, "short_pct": 0},
            "Energy": {"long_pct": 0, "short_pct": 40},
        },
    }
    assert balance_gate(target_short_pct=0.5, current_exposure=exposure) == "pass"


def test_validator_drops_short_trades_when_book_overshorted():
    """Integration: AI proposes a SHORT, but balance gate says block_shorts."""
    from ai_analyst import _validate_ai_trades

    class Ctx:
        max_position_pct = 0.10
        short_max_position_pct = 0.05
        enable_short_selling = True
        target_short_pct = 0.2  # mostly long

    portfolio_state = {
        "exposure": {
            "gross_pct": 30,
            "by_sector": {
                "Tech": {"long_pct": 5, "short_pct": 25},
            },
        },
    }
    candidates = [
        {"symbol": "AAPL", "signal": "BUY"},
        {"symbol": "TSLA", "signal": "SHORT"},
    ]
    ai_response = {"trades": [
        {"symbol": "TSLA", "action": "SHORT", "size_pct": 5, "confidence": 80},
        {"symbol": "AAPL", "action": "BUY", "size_pct": 5, "confidence": 80},
    ]}
    out = _validate_ai_trades(ai_response, candidates, ctx=Ctx(),
                                portfolio_state=portfolio_state)
    actions = [t["action"] for t in out["trades"]]
    assert "SHORT" not in actions  # blocked
    assert "BUY" in actions  # passes


def test_validator_drops_long_trades_when_book_undershorted():
    from ai_analyst import _validate_ai_trades

    class Ctx:
        max_position_pct = 0.10
        short_max_position_pct = 0.05
        enable_short_selling = True
        target_short_pct = 0.5

    portfolio_state = {
        "exposure": {
            "gross_pct": 30,
            "by_sector": {"Tech": {"long_pct": 30, "short_pct": 0}},
        },
    }
    candidates = [
        {"symbol": "AAPL", "signal": "BUY"},
        {"symbol": "TSLA", "signal": "SHORT"},
    ]
    ai_response = {"trades": [
        {"symbol": "AAPL", "action": "BUY", "size_pct": 5, "confidence": 80},
        {"symbol": "TSLA", "action": "SHORT", "size_pct": 5, "confidence": 80},
    ]}
    out = _validate_ai_trades(ai_response, candidates, ctx=Ctx(),
                                portfolio_state=portfolio_state)
    actions = [t["action"] for t in out["trades"]]
    assert "BUY" not in actions  # blocked
    assert "SHORT" in actions  # passes


def test_user_context_default_target_short_pct_is_zero():
    """Existing profiles without target_short_pct set should default
    to long-only (0.0) — no behavior change for long-only users."""
    from user_context import UserContext
    ctx = UserContext(user_id=1, segment="midcap")
    assert ctx.target_short_pct == 0.0
