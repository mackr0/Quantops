"""Position runaway sentinel tests."""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(path):
    from journal import init_db
    init_db(path)


def _add_trade(path, symbol, qty, price=100.0, status="open", side="buy"):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (symbol, side, qty, price, status),
    )
    conn.commit()
    conn.close()


def test_no_duplicates_returns_empty(tmp_path):
    from position_runaway import find_duplicate_open_buys
    p = str(tmp_path / "p.db")
    _setup(p)
    _add_trade(p, "AAPL", 10)
    _add_trade(p, "MSFT", 5)
    assert find_duplicate_open_buys(p) == []


def test_two_open_buys_same_symbol_flagged(tmp_path):
    from position_runaway import find_duplicate_open_buys
    p = str(tmp_path / "p.db")
    _setup(p)
    _add_trade(p, "AAPL", 10)
    _add_trade(p, "AAPL", 5)
    out = find_duplicate_open_buys(p)
    assert len(out) == 1
    assert out[0]["symbol"] == "AAPL"
    assert out[0]["count"] == 2
    assert out[0]["total_qty"] == 15.0


def test_closed_trades_dont_count(tmp_path):
    from position_runaway import find_duplicate_open_buys
    p = str(tmp_path / "p.db")
    _setup(p)
    _add_trade(p, "AAPL", 10, status="closed")
    _add_trade(p, "AAPL", 5)
    assert find_duplicate_open_buys(p) == []


def test_sells_dont_count(tmp_path):
    from position_runaway import find_duplicate_open_buys
    p = str(tmp_path / "p.db")
    _setup(p)
    _add_trade(p, "AAPL", 10, side="sell")
    _add_trade(p, "AAPL", 5)
    assert find_duplicate_open_buys(p) == []


def test_no_recent_history_returns_empty(tmp_path):
    """If <5 trades exist, can't compute median — skip."""
    from position_runaway import find_excessive_qty_trades
    p = str(tmp_path / "p.db")
    _setup(p)
    _add_trade(p, "AAPL", 10)
    _add_trade(p, "MSFT", 5)
    assert find_excessive_qty_trades(p) == []


def test_excessive_qty_flagged(tmp_path):
    from position_runaway import find_excessive_qty_trades
    p = str(tmp_path / "p.db")
    _setup(p)
    # 10 trades with median qty around 10
    for sym in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]:
        _add_trade(p, sym, 10)
    # 1 absurd trade — 100 shares
    _add_trade(p, "RUNAWAY", 100)
    out = find_excessive_qty_trades(p, mult=5.0)
    assert len(out) == 1
    assert out[0]["symbol"] == "RUNAWAY"
    assert out[0]["multiple"] == 10.0


def test_normal_qty_not_flagged(tmp_path):
    from position_runaway import find_excessive_qty_trades
    p = str(tmp_path / "p.db")
    _setup(p)
    for sym in ["A", "B", "C", "D", "E", "F", "G"]:
        _add_trade(p, sym, 10)
    _add_trade(p, "OK", 30)  # 3x median, under 5x cap
    out = find_excessive_qty_trades(p, mult=5.0)
    assert out == []


def test_runaway_snapshot_combines(tmp_path):
    from position_runaway import runaway_snapshot
    p = str(tmp_path / "p.db")
    _setup(p)
    for sym in ["A", "B", "C", "D", "E", "F"]:
        _add_trade(p, sym, 10)
    _add_trade(p, "DUP", 10)
    _add_trade(p, "DUP", 15)
    _add_trade(p, "BIG", 200)
    snap = runaway_snapshot(p)
    assert any(d["symbol"] == "DUP" for d in snap["duplicate_buys"])
    assert any(e["symbol"] == "BIG" for e in snap["excessive_qty"])
