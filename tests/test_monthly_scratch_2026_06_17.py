"""2026-06-17 — the monthly performance table must add up. A breakeven
(pnl == 0) close is a real closed trade but neither a win nor a loss; it
was counted in "Closed Trades" yet in NO win/loss bucket, so the row
read "12 trades, 6 wins, 0 losses" (the other 6 were OPEN shorts opened
and covered at the same price). Now pnl==0 is a separate `scratch`
bucket so wins + losses + scratch == trades; and `return_computable`
flags a month with no equity snapshot so the UI shows "—" rather than a
fake 0.0% next to a non-zero P&L.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def db(tmp_path):
    from journal import init_db
    p = str(tmp_path / "p.db")
    init_db(p)
    return p


def _seed(db, rows):
    c = sqlite3.connect(db)
    for sym, pnl, oid in rows:
        c.execute(
            "INSERT INTO trades (timestamp,symbol,side,qty,price,fill_price,"
            "pnl,status,order_id) VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-06-10T12:00:00", sym, "sell", 1.0, 10.0, 10.0, pnl,
             "closed", oid))
    c.commit()
    c.close()


def _jun(res):
    return next(m for m in res["monthly_returns"]
               if m.get("month_key") == "2026-06")


def test_breakeven_is_scratch_not_win_or_loss(db):
    from metrics.legacy import calculate_all_metrics
    # 1 win, 1 loss, 2 breakevens
    _seed(db, [("WIN", 100.0, "w"), ("LOSS", -50.0, "l"),
               ("SCR1", 0.0, "s1"), ("SCR2", 0.0, "s2")])
    jun = _jun(calculate_all_metrics([db]))
    assert jun["trades"] == 4
    assert jun["wins"] == 1
    assert jun["losses"] == 1
    assert jun["scratch"] == 2
    # the table now ADDS UP — this was the bug
    assert jun["wins"] + jun["losses"] + jun["scratch"] == jun["trades"]


def test_return_not_computable_without_equity_snapshot(db):
    from metrics.legacy import calculate_all_metrics
    _seed(db, [("WIN", 100.0, "w")])
    jun = _jun(calculate_all_metrics([db]))
    # no equity snapshot seeded -> return baseline unknown, not a real 0%
    assert jun["return_computable"] is False
    assert isinstance(jun["return_pct"], float)  # stays numeric for the SVG
