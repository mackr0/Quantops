"""Tests for mfe_capture.py — realized P&L as a fraction of available
favorable excursion.

Fix 1 of the asymmetric-edge trio. The IBM tiny-win pattern (trade
runs +11% intraday, collapses to break-even by EOD) shows up here as
a near-zero or negative capture ratio. Surfaced to dashboard + AI
prompt so the asymmetry is no longer invisible.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _seed(db_path, rows):
    """Seed BUY + SELL pairs that mirror the real schema:
    - BUY row: side='buy', price=entry_price, max_favorable_excursion=mfe
    - SELL row: side='sell', price=exit_price, pnl=realized_pnl

    rows = list of (qty, entry_price, exit_price, pnl, mfe).
    The SELL's timestamp is later than the BUY's so the join finds the
    right entry row.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT,
            reason TEXT, ai_reasoning TEXT, ai_confidence INTEGER,
            stop_loss REAL, take_profit REAL, status TEXT, pnl REAL,
            decision_price REAL, fill_price REAL, slippage_pct REAL,
            max_favorable_excursion REAL,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    for i, (qty, entry_price, exit_price, pnl, mfe) in enumerate(rows):
        sym = f"S{i}"
        # BUY row (entry) — timestamp T0, holds the MFE
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "status, max_favorable_excursion) "
            "VALUES (?, ?, 'buy', ?, ?, 'closed', ?)",
            (f"2026-04-{(i % 28)+1:02d} 09:30:00", sym, qty, entry_price, mfe),
        )
        # SELL row (exit) — later timestamp, holds the pnl
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "status, pnl) "
            "VALUES (?, ?, 'sell', ?, ?, 'closed', ?)",
            (f"2026-04-{(i % 28)+1:02d} 16:00:00", sym, qty, exit_price, pnl),
        )
    conn.commit()
    conn.close()


def test_returns_none_when_insufficient_data(tmp_path):
    from mfe_capture import compute_capture_ratio
    db = str(tmp_path / "trades.db")
    # 5 trade pairs — below the 10-trade minimum
    # (qty, entry_price, exit_price, pnl, mfe)
    _seed(db, [(100, 100.0, 110.0, 1000.0, 110.0)] * 5)
    assert compute_capture_ratio(db) is None


def test_high_capture_when_exits_near_peak(tmp_path):
    """Entry $100, exit $110, MFE $110 — exited at peak. Capture ≈ 1.0."""
    from mfe_capture import compute_capture_ratio
    db = str(tmp_path / "trades.db")
    rows = [(100, 100.0, 110.0, 1000.0, 110.0) for _ in range(15)]
    _seed(db, rows)
    cap = compute_capture_ratio(db)
    assert cap is not None
    # realized_pct = 1000 / (100×100) = 10.0%
    # mfe_pct = (110 - 100) / 100 = 10.0%
    # capture = 10.0 / 10.0 = 1.0
    assert cap["avg_capture_ratio"] == pytest.approx(1.0, abs=0.05)


def test_low_capture_when_giving_back_gains(tmp_path):
    """The IBM pattern: entry $100, ran to $110 (mfe), exit $101.
    Realized 1%, MFE 10% → capture 0.1."""
    from mfe_capture import compute_capture_ratio
    db = str(tmp_path / "trades.db")
    rows = [(100, 100.0, 101.0, 100.0, 110.0) for _ in range(15)]
    _seed(db, rows)
    cap = compute_capture_ratio(db)
    assert cap is not None
    # realized_pct = 100 / (100×100) = 1.0%
    # mfe_pct = (110 - 100) / 100 = 10.0%
    # capture = 1.0 / 10.0 = 0.10
    assert cap["avg_capture_ratio"] < 0.30


def test_negative_capture_counted(tmp_path):
    """Trade lost despite favorable excursion — the worst pattern.
    Entry $100, mfe $110, exit $99 with $100 loss."""
    from mfe_capture import compute_capture_ratio
    db = str(tmp_path / "trades.db")
    rows = [(100, 100.0, 99.0, -100.0, 110.0) for _ in range(15)]
    _seed(db, rows)
    cap = compute_capture_ratio(db)
    assert cap is not None
    assert cap["avg_capture_ratio"] < 0
    assert cap["n_negative_capture"] == 15


def test_skips_trades_with_no_favorable_excursion(tmp_path):
    """When mfe <= entry_price, capture is undefined — skip those rows."""
    from mfe_capture import compute_capture_ratio
    db = str(tmp_path / "trades.db")
    # 12 valid trades (high capture) + 5 with mfe equal to entry (skipped)
    rows = (
        [(100, 100.0, 110.0, 1000.0, 110.0)] * 12 +
        [(100, 100.0, 90.0, -1000.0, 100.0)] * 5  # mfe == entry → skipped
    )
    _seed(db, rows)
    cap = compute_capture_ratio(db)
    assert cap is not None
    # Only the 12 valid trades counted
    assert cap["n_trades"] == 12


def test_render_suppressed_when_capture_is_high():
    from mfe_capture import render_for_prompt
    cap = {"avg_capture_ratio": 0.75, "n_trades": 50, "n_negative_capture": 0}
    assert render_for_prompt(cap) == ""


def test_render_warns_on_low_capture():
    from mfe_capture import render_for_prompt
    cap = {"avg_capture_ratio": 0.10, "n_trades": 50, "n_negative_capture": 0}
    block = render_for_prompt(cap)
    assert "MFE CAPTURE" in block
    assert "leaving substantial money" in block.lower()
    assert "10%" in block


def test_render_flags_negative_captures():
    from mfe_capture import render_for_prompt
    cap = {"avg_capture_ratio": -0.05, "n_trades": 30, "n_negative_capture": 12}
    block = render_for_prompt(cap)
    assert "12" in block
    assert "lost" in block.lower()


def test_render_handles_none_input():
    from mfe_capture import render_for_prompt
    assert render_for_prompt(None) == ""
    assert render_for_prompt({}) == ""
