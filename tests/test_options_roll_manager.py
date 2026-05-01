"""Phase C1 of OPTIONS_PROGRAM_PLAN.md — roll mechanics.

Verifies:
  - Near-expiry detection respects window
  - Credit position at ≥80% max profit → AUTO_CLOSE
  - Credit position at 50-80% → ROLL_RECOMMEND
  - Credit position below 50% → HOLD
  - Long-premium positions → HOLD (no auto-close path)
  - Missing quote → HOLD
  - Render block surfaces actionable rows only
  - Auto-close: submits opposite-side order, updates journal
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from unittest.mock import MagicMock

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


def _seed_option(db_path, expiry, side="sell", strategy="covered_call",
                  occ="AAPL  990115C00150000", qty=1, premium=2.00,
                  strike=150.0):
    from journal import log_trade
    return log_trade(
        symbol="AAPL", side=side, qty=qty, price=premium,
        order_id="test", signal_type="OPTIONS", strategy=strategy,
        decision_price=premium, occ_symbol=occ,
        option_strategy=strategy, expiry=expiry, strike=strike,
        db_path=db_path,
    )


class TestFindNearExpiryOptions:
    def test_finds_options_in_window(self, tmp_db):
        from options_roll_manager import find_near_expiry_options
        today = date(2026, 5, 1)
        # Option expires 3 days out → in window
        _seed_option(tmp_db, expiry=(today + timedelta(days=3)).isoformat())
        rows = find_near_expiry_options(tmp_db, today=today, window_days=7)
        assert len(rows) == 1

    def test_skips_options_outside_window(self, tmp_db):
        from options_roll_manager import find_near_expiry_options
        today = date(2026, 5, 1)
        # Expires 30 days out → out of window
        _seed_option(tmp_db, expiry=(today + timedelta(days=30)).isoformat())
        rows = find_near_expiry_options(tmp_db, today=today, window_days=7)
        assert rows == []

    def test_skips_already_expired(self, tmp_db):
        """Already-expired options are the lifecycle sweep's domain."""
        from options_roll_manager import find_near_expiry_options
        today = date(2026, 5, 1)
        _seed_option(tmp_db, expiry=(today - timedelta(days=2)).isoformat())
        rows = find_near_expiry_options(tmp_db, today=today, window_days=7)
        assert rows == []


class TestEvaluateForRoll:
    def _row(self, **overrides):
        return {
            "side": "sell", "option_strategy": "covered_call",
            "decision_price": 2.00, "qty": 1, "occ_symbol": "X",
            **overrides,
        }

    def test_credit_at_80pct_returns_auto_close(self):
        from options_roll_manager import evaluate_for_roll
        # Premium $2 collected; current value $0.40 → captured 80%
        result = evaluate_for_roll(self._row(), current_market_value_per_contract=0.40)
        assert result["action"] == "AUTO_CLOSE"
        assert result["profit_pct"] == pytest.approx(0.80)

    def test_credit_at_50pct_returns_roll_recommend(self):
        from options_roll_manager import evaluate_for_roll
        # Premium $2; current $1 → 50% captured
        result = evaluate_for_roll(self._row(), current_market_value_per_contract=1.00)
        assert result["action"] == "ROLL_RECOMMEND"
        assert result["profit_pct"] == pytest.approx(0.50)

    def test_credit_at_30pct_returns_hold(self):
        from options_roll_manager import evaluate_for_roll
        # Premium $2; current $1.40 → 30% captured
        result = evaluate_for_roll(self._row(), current_market_value_per_contract=1.40)
        assert result["action"] == "HOLD"

    def test_long_premium_returns_hold(self):
        """Long position — no max-profit anchor; lifecycle handles it."""
        from options_roll_manager import evaluate_for_roll
        result = evaluate_for_roll(
            self._row(side="buy", option_strategy="long_call"),
            current_market_value_per_contract=0.10,
        )
        assert result["action"] == "HOLD"
        assert "Long-premium" in result["reason"]

    def test_no_quote_returns_hold(self):
        from options_roll_manager import evaluate_for_roll
        result = evaluate_for_roll(self._row(),
                                       current_market_value_per_contract=None)
        assert result["action"] == "HOLD"

    def test_credit_at_loss_returns_hold(self):
        """Credit position now worth more than premium collected →
        we're at a loss; not actionable for roll mechanics."""
        from options_roll_manager import evaluate_for_roll
        result = evaluate_for_roll(self._row(),
                                       current_market_value_per_contract=3.50)
        assert result["action"] == "HOLD"
        assert result["profit_pct"] < 0


class TestAutoCloseHighProfitCredits:
    def test_auto_closes_position_at_high_profit(self, tmp_db):
        from options_roll_manager import auto_close_high_profit_credits
        today = date(2026, 5, 1)
        _seed_option(tmp_db, expiry=(today + timedelta(days=3)).isoformat(),
                     side="sell", premium=2.00,
                     occ="AAPL  990501C00150000")
        api = MagicMock()
        api.submit_order.return_value = MagicMock(id="close-order-1")
        # Quote: $0.30 → 85% captured
        result = auto_close_high_profit_credits(
            api, tmp_db, quote_lookup=lambda s: 0.30, today=today,
        )
        assert result["evaluated"] == 1
        assert result["auto_closed"] == 1
        # Submit_order called with BUY (closing a SELL)
        kwargs = api.submit_order.call_args.kwargs
        assert kwargs["side"] == "buy"
        assert kwargs["qty"] == 1

        # Journal updated
        from journal import _get_conn
        conn = _get_conn(tmp_db)
        row = conn.execute(
            "SELECT status, pnl FROM trades WHERE id=1"
        ).fetchone()
        assert row[0] == "closed"
        # P&L = (2.00 - 0.30) * 100 = $170
        assert row[1] == pytest.approx(170.0)

    def test_skips_below_threshold(self, tmp_db):
        from options_roll_manager import auto_close_high_profit_credits
        today = date(2026, 5, 1)
        _seed_option(tmp_db, expiry=(today + timedelta(days=3)).isoformat(),
                     premium=2.00)
        api = MagicMock()
        # Quote $1.40 → 30% captured (below 80% threshold)
        result = auto_close_high_profit_credits(
            api, tmp_db, quote_lookup=lambda s: 1.40, today=today,
        )
        assert result["auto_closed"] == 0
        api.submit_order.assert_not_called()

    def test_skips_long_positions(self, tmp_db):
        from options_roll_manager import auto_close_high_profit_credits
        today = date(2026, 5, 1)
        _seed_option(tmp_db,
                     expiry=(today + timedelta(days=3)).isoformat(),
                     side="buy", strategy="long_call",
                     occ="AAPL  990501C00150000", premium=3.00)
        api = MagicMock()
        result = auto_close_high_profit_credits(
            api, tmp_db, quote_lookup=lambda s: 0.10, today=today,
        )
        assert result["auto_closed"] == 0


class TestRenderRollRecommendations:
    def test_empty_when_no_near_expiry(self, tmp_db):
        from options_roll_manager import render_roll_recommendations_for_prompt
        today = date(2026, 5, 1)
        out = render_roll_recommendations_for_prompt(
            tmp_db, today=today, quote_lookup=lambda s: 1.00,
        )
        assert out == ""

    def test_includes_roll_recommend_line(self, tmp_db):
        from options_roll_manager import render_roll_recommendations_for_prompt
        today = date(2026, 5, 1)
        _seed_option(tmp_db, expiry=(today + timedelta(days=3)).isoformat(),
                     premium=2.00,
                     occ="AAPL  990501C00150000")
        # 60% captured → ROLL_RECOMMEND
        out = render_roll_recommendations_for_prompt(
            tmp_db, today=today, quote_lookup=lambda s: 0.80,
        )
        assert "NEAR-EXPIRY OPTION POSITIONS" in out
        assert "consider rolling" in out.lower()

    def test_omits_held_below_threshold(self, tmp_db):
        from options_roll_manager import render_roll_recommendations_for_prompt
        today = date(2026, 5, 1)
        _seed_option(tmp_db, expiry=(today + timedelta(days=3)).isoformat(),
                     premium=2.00, occ="AAPL  990501C00150000")
        # 20% captured → HOLD, not in render
        out = render_roll_recommendations_for_prompt(
            tmp_db, today=today, quote_lookup=lambda s: 1.60,
        )
        assert out == ""
