"""Phase C3 of OPTIONS_PROGRAM_PLAN.md — wheel state machine.

Verifies state derivation across the cycle:
  cash → csp_open → (assigned) shares_held → cc_open → (called away) cash
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_db():
    from journal import init_db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _stock_pos(sym, qty):
    return {"symbol": sym, "qty": qty}


def _seed_open_csp(db_path, sym, strike=145, expiry="2099-01-15"):
    from journal import log_trade
    log_trade(
        symbol=sym, side="sell", qty=1, price=2.00,
        signal_type="OPTIONS", strategy="cash_secured_put",
        decision_price=2.00,
        occ_symbol=f"{sym:<6}990115P00{int(strike*1000):08d}"[:21],
        option_strategy="cash_secured_put",
        expiry=expiry, strike=strike, db_path=db_path,
    )


def _seed_open_cc(db_path, sym, strike=160, expiry="2099-01-15"):
    from journal import log_trade
    log_trade(
        symbol=sym, side="sell", qty=1, price=1.50,
        signal_type="OPTIONS", strategy="covered_call",
        decision_price=1.50,
        occ_symbol=f"{sym:<6}990115C00{int(strike*1000):08d}"[:21],
        option_strategy="covered_call",
        expiry=expiry, strike=strike, db_path=db_path,
    )


class TestDetermineWheelState:
    def test_no_shares_no_options_is_cash(self, tmp_db):
        from options_wheel import determine_wheel_state, STATE_CASH
        result = determine_wheel_state(tmp_db, [], "AAPL")
        assert result["state"] == STATE_CASH
        assert result["shares_held"] == 0

    def test_open_csp_no_shares_is_csp_open(self, tmp_db):
        from options_wheel import determine_wheel_state, STATE_CSP_OPEN
        _seed_open_csp(tmp_db, "AAPL")
        result = determine_wheel_state(tmp_db, [], "AAPL")
        assert result["state"] == STATE_CSP_OPEN
        assert result["active_csp"] is not None

    def test_shares_held_no_cc_is_shares_held(self, tmp_db):
        from options_wheel import determine_wheel_state, STATE_SHARES_HELD
        result = determine_wheel_state(
            tmp_db, [_stock_pos("AAPL", 100)], "AAPL",
        )
        assert result["state"] == STATE_SHARES_HELD
        assert result["shares_held"] == 100

    def test_shares_held_with_cc_is_cc_open(self, tmp_db):
        from options_wheel import determine_wheel_state, STATE_CC_OPEN
        _seed_open_cc(tmp_db, "AAPL")
        result = determine_wheel_state(
            tmp_db, [_stock_pos("AAPL", 100)], "AAPL",
        )
        assert result["state"] == STATE_CC_OPEN
        assert result["active_cc"] is not None

    def test_state_per_symbol_independent(self, tmp_db):
        """States are independent across symbols."""
        from options_wheel import determine_wheel_state, STATE_CASH, STATE_SHARES_HELD
        positions = [_stock_pos("AAPL", 100)]
        # AAPL has shares
        a_state = determine_wheel_state(tmp_db, positions, "AAPL")
        assert a_state["state"] == STATE_SHARES_HELD
        # MSFT has none
        m_state = determine_wheel_state(tmp_db, positions, "MSFT")
        assert m_state["state"] == STATE_CASH


class TestRecommendNextAction:
    def test_cash_recommends_csp(self, tmp_db):
        from options_wheel import (
            determine_wheel_state, recommend_next_action,
        )
        state = determine_wheel_state(tmp_db, [], "AAPL")
        rec = recommend_next_action(state, "AAPL", current_price=150.0)
        assert rec is not None
        assert rec["step"] == "open_csp"
        assert rec["strategy"] == "cash_secured_put"
        # Strike ~5% below 150 → ~142 area, rounded
        assert 140 <= rec["strike"] <= 145

    def test_shares_held_recommends_cc(self, tmp_db):
        from options_wheel import (
            determine_wheel_state, recommend_next_action,
        )
        state = determine_wheel_state(
            tmp_db, [_stock_pos("AAPL", 200)], "AAPL",
        )
        rec = recommend_next_action(state, "AAPL", current_price=150.0)
        assert rec is not None
        assert rec["step"] == "open_cc"
        assert rec["strategy"] == "covered_call"
        # Strike ~5% above 150 → ~157.5 area
        assert 155 <= rec["strike"] <= 160
        # 200 shares → 2 contracts
        assert rec["contracts"] == 2

    def test_csp_open_no_recommendation(self, tmp_db):
        """Wait for the CSP to expire / be assigned — no new action."""
        from options_wheel import (
            determine_wheel_state, recommend_next_action,
        )
        _seed_open_csp(tmp_db, "AAPL")
        state = determine_wheel_state(tmp_db, [], "AAPL")
        rec = recommend_next_action(state, "AAPL", current_price=150.0)
        assert rec is None

    def test_cc_open_no_recommendation(self, tmp_db):
        from options_wheel import (
            determine_wheel_state, recommend_next_action,
        )
        _seed_open_cc(tmp_db, "AAPL")
        state = determine_wheel_state(
            tmp_db, [_stock_pos("AAPL", 100)], "AAPL",
        )
        rec = recommend_next_action(state, "AAPL", current_price=150.0)
        assert rec is None

    def test_under_100_shares_no_cc_recommendation(self, tmp_db):
        """Need ≥100 shares to write a CC — fewer means we can't act."""
        from options_wheel import (
            determine_wheel_state, recommend_next_action,
        )
        # 50 shares → state will still be cash (not enough for the
        # state machine's "shares_held" requirement of ≥100). Verify.
        state = determine_wheel_state(
            tmp_db, [_stock_pos("AAPL", 50)], "AAPL",
        )
        rec = recommend_next_action(state, "AAPL", current_price=150.0)
        # state=cash → recommends CSP, not CC
        assert rec["step"] == "open_csp"


class TestRenderWheelBlock:
    def test_empty_wheel_symbols_returns_empty(self, tmp_db):
        from options_wheel import render_wheel_block_for_prompt
        out = render_wheel_block_for_prompt(
            tmp_db, [], [], price_lookup=lambda s: 150.0,
        )
        assert out == ""

    def test_renders_state_per_symbol(self, tmp_db):
        from options_wheel import render_wheel_block_for_prompt
        out = render_wheel_block_for_prompt(
            tmp_db, [_stock_pos("AAPL", 100)],
            wheel_symbols=["AAPL"],
            price_lookup=lambda s: 150.0,
        )
        assert "WHEEL STRATEGY" in out
        assert "AAPL" in out
        assert "shares_held" in out

    def test_renders_recommendation_on_actionable_state(self, tmp_db):
        """Cash state on opted-in symbol should surface CSP recommendation."""
        from options_wheel import render_wheel_block_for_prompt
        out = render_wheel_block_for_prompt(
            tmp_db, [], wheel_symbols=["AAPL"],
            price_lookup=lambda s: 150.0,
        )
        assert "open_csp" in out or "Sell CSP" in out

    def test_skips_symbols_with_no_price(self, tmp_db):
        from options_wheel import render_wheel_block_for_prompt
        out = render_wheel_block_for_prompt(
            tmp_db, [], wheel_symbols=["AAPL"],
            price_lookup=lambda s: None,
        )
        # No price → no rendering for that symbol
        assert out == ""
