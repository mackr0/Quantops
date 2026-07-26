"""Realized-slippage learning labels (P2.1, 2026-07-26).

actual_return_pct_net is the label the tuner / meta-model / AI
track-record optimize against, so its cost term must be REALIZED,
not assumed. Three gaps closed:

- Exit legs: the round-trip cost assumed symmetric 2 × entry
  slippage even when the closing trade's realized slippage_pct was
  sitting in the journal. Now the exit leg's own number is used;
  the symmetric estimate remains only as the no-exit-row fallback.
- Shorts matched NOTHING: SHORT entries are journaled with
  side='short' (exits 'cover'), but the matcher mapped every
  non-BUY signal to side='sell' — every short prediction netted
  zero cost since #186 shipped.
- Borrow accrual: shorts now accrue BORROW_RATE_ANNUAL_PCT over
  days_held (general-collateral average; hard-to-borrow refines
  later).

(Historical note: the Fix-List item described the cost as a
"constant 2×12.5bps" — that was true before #186/c240b8c switched
to per-row entry slippage; the surviving gaps were the exit leg,
the short-side matching, and borrow.)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai_tracker import (  # noqa: E402
    BORROW_RATE_ANNUAL_PCT, _estimate_round_trip_cost_pct,
)
from journal import init_db  # noqa: E402

T0 = "2026-07-20 15:00:00"


def _db(rows):
    """Temp journal DB seeded with (symbol, side, timestamp,
    slippage_pct) trade rows."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    conn = sqlite3.connect(path)
    for sym, side, ts, slip in rows:
        conn.execute(
            "INSERT INTO trades (symbol, side, timestamp, status, "
            "slippage_pct, qty, price) VALUES (?, ?, ?, 'closed', ?, 1, 10)",
            (sym, side, ts, slip))
    conn.commit()
    conn.close()
    return path


def _pred(signal="BUY", ts=T0):
    return {"symbol": "XYZ", "predicted_signal": signal, "timestamp": ts}


class TestExitLegRealized:
    def test_uses_exit_legs_own_slippage(self):
        db = _db([("XYZ", "buy", T0, 0.10),
                  ("XYZ", "sell", "2026-07-22 16:00:00", 0.40)])
        try:
            cost = _estimate_round_trip_cost_pct(_pred(), db, days_held=3)
            assert abs(cost - 0.50) < 1e-9, (
                f"expected entry 0.10 + realized exit 0.40 = 0.50, "
                f"got {cost} — the symmetric 2x assumption is back."
            )
        finally:
            os.unlink(db)

    def test_no_exit_row_falls_back_to_symmetric(self):
        db = _db([("XYZ", "buy", T0, 0.10)])
        try:
            cost = _estimate_round_trip_cost_pct(_pred(), db, days_held=3)
            assert abs(cost - 0.20) < 1e-9
        finally:
            os.unlink(db)

    def test_exit_outside_holding_window_ignored(self):
        """A sell weeks later belongs to a DIFFERENT round trip."""
        db = _db([("XYZ", "buy", T0, 0.10),
                  ("XYZ", "sell", "2026-08-30 16:00:00", 0.90)])
        try:
            cost = _estimate_round_trip_cost_pct(_pred(), db, days_held=2)
            assert abs(cost - 0.20) < 1e-9
        finally:
            os.unlink(db)


class TestShortSideMatching:
    def test_short_prediction_matches_short_and_cover_rows(self):
        db = _db([("XYZ", "short", T0, 0.15),
                  ("XYZ", "cover", "2026-07-20 18:00:00", 0.25)])
        try:
            cost = _estimate_round_trip_cost_pct(
                _pred("SHORT"), db, days_held=0)
            assert abs(cost - 0.40) < 1e-9, (
                f"expected short 0.15 + cover 0.25 = 0.40, got {cost} "
                "— the everything-not-BUY-is-'sell' mapping is back "
                "(shorts netted zero cost for months)."
            )
        finally:
            os.unlink(db)

    def test_short_accrues_borrow_over_days_held(self):
        db = _db([("XYZ", "short", T0, 0.15),
                  ("XYZ", "cover", "2026-07-24 16:00:00", 0.25)])
        try:
            days = 10
            cost = _estimate_round_trip_cost_pct(
                _pred("SHORT"), db, days_held=days)
            expected = 0.40 + BORROW_RATE_ANNUAL_PCT / 365.0 * days
            assert abs(cost - expected) < 1e-9
        finally:
            os.unlink(db)

    def test_long_never_accrues_borrow(self):
        db = _db([("XYZ", "buy", T0, 0.10),
                  ("XYZ", "sell", "2026-07-22 16:00:00", 0.40)])
        try:
            c3 = _estimate_round_trip_cost_pct(_pred(), db, days_held=3)
            c30 = _estimate_round_trip_cost_pct(_pred(), db, days_held=30)
            assert c3 == c30
        finally:
            os.unlink(db)


class TestUntradedPredictions:
    def test_no_matching_trade_costs_nothing(self):
        db = _db([])
        try:
            assert _estimate_round_trip_cost_pct(
                _pred(), db, days_held=5) == 0.0
        finally:
            os.unlink(db)
