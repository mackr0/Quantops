"""Lock in the four-way split of best/worst predictions.

Mack flagged on 2026-05-04 that the dashboard was showing two HOLD
predictions (MXL +56%, CMPX -67%) under "Best Prediction" / "Worst
Prediction" with 0% confidence. The numbers were technically correct
(those underlyings DID move that much during the resolution window)
but conflated three different situations:

  - A directional trade (BUY / STRONG_SELL / SHORT) that won
  - A directional trade that lost
  - A HOLD where the AI passed and the stock then rose (missed gain)
  - A HOLD where the AI passed and the stock then fell (avoided loss)

The new contract:

  best_trade            — directional only, trade_pnl_pct sign-flipped
                          for SHORTs so wins are positive
  worst_trade           — directional only, lowest trade_pnl_pct
  biggest_missed_gain   — HOLD with the highest actual_return_pct
  biggest_avoided_loss  — HOLD with the lowest actual_return_pct

best_prediction/worst_prediction stay populated from best_trade/
worst_trade for backward compat.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def db_with_mixed_predictions():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from ai_tracker import init_tracker_db
    init_tracker_db(path)

    rows = [
        # Directional BUY winners (return_pct positive = good for BUY)
        ("AAPL", "BUY", 80, 100.0, "win",  +12.5, 5),
        ("MSFT", "BUY", 70, 200.0, "loss", -8.0, 5),
        # Directional SHORT — return_pct is the underlying's move,
        # so SHORT win = negative return_pct, SHORT loss = positive.
        ("TSLA", "STRONG_SELL", 75, 300.0, "win",  -15.0, 5),  # short win: best trade if highest abs
        ("NVDA", "SHORT", 65, 500.0, "loss", +20.0, 5),         # short loss: worst trade
        # HOLDs with extreme moves
        ("MXL",  "HOLD", 0, 50.0,  "loss", +56.5, 5),  # missed gain
        ("CMPX", "HOLD", 0, 100.0, "win",  -67.9, 5),  # avoided loss
        ("XOM",  "HOLD", 0, 80.0,  "win",  +1.2, 5),
    ]
    conn = sqlite3.connect(path)
    for sym, sig, conf, price, outcome, ret, days in rows:
        conn.execute(
            "INSERT INTO ai_predictions "
            "(timestamp, symbol, predicted_signal, confidence, "
            "price_at_prediction, status, actual_outcome, "
            "actual_return_pct, days_held) "
            "VALUES (datetime('now','-7 days'), ?, ?, ?, ?, "
            "'resolved', ?, ?, ?)",
            (sym, sig, conf, price, outcome, ret, days),
        )
    conn.commit()
    conn.close()
    yield path
    os.remove(path)


def test_best_trade_uses_trade_pnl_not_underlying(db_with_mixed_predictions):
    """For a SHORT the underlying went DOWN -15% — that's a +15%
    trade-PnL win, and it should beat AAPL's +12.5% BUY win."""
    from ai_tracker import get_ai_performance
    perf = get_ai_performance(db_path=db_with_mixed_predictions)
    assert perf["best_trade"] is not None
    assert perf["best_trade"]["symbol"] == "TSLA"
    assert perf["best_trade"]["signal"] == "STRONG_SELL"
    # trade_pnl_pct should be +15 (sign flipped from -15 underlying move)
    assert perf["best_trade"]["trade_pnl_pct"] == 15.0


def test_worst_trade_is_short_loss(db_with_mixed_predictions):
    """NVDA SHORT lost -20% (underlying ran +20%). That's worse than
    MSFT BUY's -8% loss in trade-PnL terms."""
    from ai_tracker import get_ai_performance
    perf = get_ai_performance(db_path=db_with_mixed_predictions)
    assert perf["worst_trade"] is not None
    assert perf["worst_trade"]["symbol"] == "NVDA"
    assert perf["worst_trade"]["trade_pnl_pct"] == -20.0


def test_holds_excluded_from_best_worst_trade(db_with_mixed_predictions):
    """MXL +56% HOLD must NOT appear in best_trade — it's a HOLD."""
    from ai_tracker import get_ai_performance
    perf = get_ai_performance(db_path=db_with_mixed_predictions)
    assert perf["best_trade"]["symbol"] != "MXL"
    assert perf["worst_trade"]["symbol"] != "CMPX"


def test_biggest_missed_gain_is_top_hold(db_with_mixed_predictions):
    from ai_tracker import get_ai_performance
    perf = get_ai_performance(db_path=db_with_mixed_predictions)
    assert perf["biggest_missed_gain"]["symbol"] == "MXL"
    assert perf["biggest_missed_gain"]["return_pct"] == 56.5


def test_biggest_avoided_loss_is_bottom_hold(db_with_mixed_predictions):
    from ai_tracker import get_ai_performance
    perf = get_ai_performance(db_path=db_with_mixed_predictions)
    assert perf["biggest_avoided_loss"]["symbol"] == "CMPX"
    assert perf["biggest_avoided_loss"]["return_pct"] == -67.9


def test_legacy_fields_track_new_trade_fields(db_with_mixed_predictions):
    """best_prediction / worst_prediction are kept for backwards
    compatibility — they should now mirror the directional best_trade
    / worst_trade (not include HOLDs)."""
    from ai_tracker import get_ai_performance
    perf = get_ai_performance(db_path=db_with_mixed_predictions)
    assert perf["best_prediction"]["symbol"] == perf["best_trade"]["symbol"]
    assert perf["worst_prediction"]["symbol"] == perf["worst_trade"]["symbol"]


def test_no_directional_no_trade_fields():
    """If only HOLDs exist, best_trade/worst_trade are None but the
    HOLD-only fields populate."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        from ai_tracker import init_tracker_db, get_ai_performance
        init_tracker_db(path)
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO ai_predictions "
            "(timestamp, symbol, predicted_signal, confidence, "
            "price_at_prediction, status, actual_outcome, "
            "actual_return_pct, days_held) "
            "VALUES (datetime('now','-5 days'), 'XYZ', 'HOLD', 0, "
            "10.0, 'resolved', 'win', -3.0, 5)",
        )
        conn.commit()
        conn.close()
        perf = get_ai_performance(db_path=path)
        assert perf["best_trade"] is None
        assert perf["worst_trade"] is None
        assert perf["biggest_avoided_loss"]["symbol"] == "XYZ"
    finally:
        os.remove(path)
