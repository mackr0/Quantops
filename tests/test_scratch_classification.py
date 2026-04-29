"""Fix 3: scratch-trade classification.

Trades with |pnl_pct| < 0.5% are 'scratch' — effectively break-even
after slippage and commission. Counting them as 'wins' inflated the
win rate to 70%+ on profiles that were closing trades at break-even
all day. This test pins the corrected classification.

Real prod data that motivated this:
  profile_8 winning_trades = 30, median win = $43, median trade
  notional ~$50K → median win is ~0.09% — well below the threshold.
  Many of those came from trail-stop firings after intraday reversals
  (the IBM $2.70 case from INTRADAY_STOPS_PLAN.md).
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _seed_trades(db_path, rows):
    """rows = list of (qty, price, pnl). Each row becomes a closed trade."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT,
            reason TEXT, ai_reasoning TEXT, ai_confidence INTEGER,
            stop_loss REAL, take_profit REAL, status TEXT, pnl REAL,
            decision_price REAL, fill_price REAL, slippage_pct REAL,
            max_favorable_excursion REAL,
            protective_stop_order_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_snapshots (
            date TEXT PRIMARY KEY, equity REAL, cash REAL, total_pnl REAL
        )
    """)
    for i, (qty, price, pnl) in enumerate(rows):
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "status, pnl) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"2026-04-{(i % 28)+1:02d}", f"S{i}", "sell", qty, price, "closed", pnl),
        )
    conn.commit()
    conn.close()


def test_scratch_excluded_from_win_rate(tmp_path):
    """30 'wins' all sub-0.5% = 0 real wins. 5 real losses at -3% =
    5 real losses. Win rate must be 0%, not 86% (the old calc would
    say 30 / (30+5) = 86%)."""
    from metrics import calculate_all_metrics
    db = str(tmp_path / "trades.db")
    rows = (
        # 30 sub-threshold "wins": $50 pnl on $50K notional = 0.1%
        [(100, 500.0, 50.0)] * 30 +
        # 5 real losses: pnl -$1500 on $50K notional = -3%
        [(100, 500.0, -1500.0)] * 5
    )
    _seed_trades(db, rows)

    m = calculate_all_metrics([db])
    assert m["winning_trades"] == 0, (
        f"Sub-0.5% pnl trades must NOT count as wins. Got "
        f"{m['winning_trades']} reported as winners."
    )
    assert m["losing_trades"] == 5
    assert m["scratch_trades"] == 30
    # Win rate denominator excludes scratches: 0 / (0 + 5) = 0%
    assert m["win_rate"] == 0.0


def test_scratch_rate_surfaces_separately(tmp_path):
    """When most trades are scratches, the dashboard needs to show
    scratch_rate so the user knows what's going on."""
    from metrics import calculate_all_metrics
    db = str(tmp_path / "trades.db")
    rows = (
        [(100, 500.0, 50.0)] * 18 +    # scratch
        [(100, 500.0, 1500.0)] * 1 +    # real win (+3%)
        [(100, 500.0, -1500.0)] * 1     # real loss (-3%)
    )
    _seed_trades(db, rows)

    m = calculate_all_metrics([db])
    assert m["scratch_trades"] == 18
    assert m["winning_trades"] == 1
    assert m["losing_trades"] == 1
    assert m["scratch_rate"] == pytest.approx(90.0, abs=0.1)
    # Decisive: 1 win, 1 loss → 50%
    assert m["win_rate"] == 50.0


def test_real_win_at_threshold_counts_as_win(tmp_path):
    """At exactly 0.5%, trade is a win (the threshold is inclusive on
    the win side)."""
    from metrics import calculate_all_metrics
    db = str(tmp_path / "trades.db")
    # 100 shares at $100 = $10K notional. $50 pnl = exactly 0.5%.
    rows = [(100, 100.0, 50.0)]
    _seed_trades(db, rows)
    m = calculate_all_metrics([db])
    assert m["winning_trades"] == 1
    assert m["scratch_trades"] == 0


def test_below_threshold_is_scratch(tmp_path):
    """At 0.49%, just below threshold → scratch."""
    from metrics import calculate_all_metrics
    db = str(tmp_path / "trades.db")
    rows = [(100, 100.0, 49.0)]
    _seed_trades(db, rows)
    m = calculate_all_metrics([db])
    assert m["winning_trades"] == 0
    assert m["scratch_trades"] == 1


def test_scratch_pnl_excluded_from_total_gains(tmp_path):
    """Slippage_vs_gross divides slippage by total_gains. If scratch
    pnls inflate total_gains, the ratio looks better than reality."""
    from metrics import calculate_all_metrics
    db = str(tmp_path / "trades.db")
    rows = (
        [(100, 100.0, 30.0)] * 10 +   # 10 scratches at $30 each
        [(100, 100.0, 500.0)] * 2     # 2 real wins at $500 each
    )
    _seed_trades(db, rows)
    m = calculate_all_metrics([db])
    # winning_trades is now derived from real wins only
    win_pnl_sum = m.get("winning_trades", 0) and 1000.0  # 2 × $500
    # The real-wins basis: 2 trades worth $1000 total. Scratches' $300
    # is no longer in total_gains.
    assert m["winning_trades"] == 2
    assert m["scratch_trades"] == 10


def test_template_shows_scratch_rate():
    """Static check: performance template surfaces scratch_rate so
    the user can see the breakdown."""
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "performance.html"
    )
    with open(template_path) as f:
        html = f.read()
    assert "scratch_rate" in html, (
        "Performance template doesn't surface scratch_rate — users "
        "can't see the win-rate denominator excluded scratches."
    )
    assert "scratch_trades" in html
