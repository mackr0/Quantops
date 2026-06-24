"""Slice 6 — per-profile position-truth + realized-P&L hardening (2026-06-23).

Two verified-gap fixes, both per-profile (no cross-profile anything):
  1. get_virtual_positions must EXCLUDE rows tagged data_quality (a poisoned
     'open' phantom row otherwise corrupts the own_virtual_qty the oversell
     door trusts).
  2. recompute_realized_pnl must NOT FIFO-match a 'pending_fill' row that has
     no real broker fill price — doing so fabricates realized P&L off the
     decision price (the unfilled-buy decomposition gap).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _db(tmp_path):
    import journal
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    return db


def test_get_virtual_positions_excludes_data_quality_rows(tmp_path):
    import journal
    db = _db(tmp_path)
    with sqlite3.connect(db) as c:
        # a poisoned phantom row tagged data_quality, status 'open'
        c.execute("INSERT INTO trades (symbol,side,qty,price,fill_price,"
                  "status,data_quality) VALUES "
                  "('PHNTM','buy',1000,0.45,0.45,'open','phantom_stop_x')")
        # a clean held position
        c.execute("INSERT INTO trades (symbol,side,qty,price,fill_price,"
                  "status,data_quality) VALUES "
                  "('REAL','buy',50,10.0,10.0,'open',NULL)")
        c.commit()
    pos = {p["symbol"]: p["qty"] for p in journal.get_virtual_positions(db)}
    assert "PHNTM" not in pos, "data_quality-tagged phantom must not count as a position"
    assert pos.get("REAL") == 50


def test_recompute_mirrors_positions_on_pending_fill(tmp_path):
    """realized must consume the SAME row-set as get_virtual_positions: a
    pending_fill row is KEPT by both (an earlier guard that dropped it from
    realized only broke the positions/cash/realized agreement and was
    reverted). Here a pending_fill close that nets the position flat must also
    book its realized P&L, matching positions."""
    import journal
    db = _db(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO trades (timestamp,symbol,side,qty,price,"
                  "fill_price,status,order_id) VALUES "
                  "('2026-06-24T10:00:00','XYZ','buy',100,100.0,100.0,"
                  "'closed','o-buy')")
        c.execute("INSERT INTO trades (timestamp,symbol,side,qty,price,"
                  "fill_price,status,order_id) VALUES "
                  "('2026-06-24T10:05:00','XYZ','sell',100,105.0,105.0,"
                  "'pending_fill','o-sell')")
        c.commit()
    journal.recompute_realized_pnl(db)
    with sqlite3.connect(db) as c:
        sell_pnl = c.execute(
            "SELECT pnl FROM trades WHERE order_id='o-sell'").fetchone()[0]
    # the pending_fill close is honored (positions nets flat, so realized must
    # book the +500), keeping positions/cash/realized in agreement.
    assert sell_pnl is not None and abs(float(sell_pnl) - 500.0) < 1e-6, (
        f"pending_fill close not mirrored into realized: pnl={sell_pnl}")


def test_cash_excludes_data_quality_rows_like_positions(tmp_path):
    """Adversarial-review regression: a data_quality-tagged closed SELL must be
    excluded from the CASH math identically to positions, or equity is
    overstated (the sell's proceeds counted while its lot stays open)."""
    import journal
    db = _db(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO trades (symbol,side,qty,price,fill_price,status,"
                  "data_quality) VALUES ('XYZ','buy',100,10.0,10.0,'open',NULL)")
        # tagged, status 'closed' (passes the cash status filter) — must still
        # be excluded by the data_quality filter on both sides.
        c.execute("INSERT INTO trades (symbol,side,qty,price,fill_price,status,"
                  "data_quality) VALUES ('XYZ','sell',100,12.0,12.0,'closed',"
                  "'phantom_stop_2026_05_11')")
        c.commit()
    info = journal.get_virtual_account_info(db_path=db, initial_capital=100000.0)
    # cash = 100000 - 1000 (buy) + 0 (tagged sell EXCLUDED). Pre-fix the sell
    # added +1200 -> cash 100200 while the buy lot stayed open -> equity
    # overstated. equity is always cash+portfolio_value by construction.
    assert abs(info["cash"] - 99000.0) < 1e-6, (
        f"data_quality sell leaked into cash: cash={info['cash']}")
    assert abs(info["equity"] - (info["cash"] + info["portfolio_value"])) < 1e-6
