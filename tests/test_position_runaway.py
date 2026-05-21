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


# ---------------------------------------------------------------------------
# 2026-05-21 — per-instrument-class median (stock vs option)
# ---------------------------------------------------------------------------

def _add_option_trade(path, occ_symbol, qty, status="open", side="buy"):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price, status, occ_symbol) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (occ_symbol[:4], side, qty, 5.0, status, occ_symbol),
    )
    conn.commit()
    conn.close()


def test_stock_positions_not_flagged_against_option_median(tmp_path):
    """The exact prod scenario: an options-heavy profile (option
    contract qtys 1-4) holding normal stock positions (~100 shares).
    Open stock positions must NOT be flagged as runaways just because
    the OPTION median is ~2. Each class is judged against its own
    median."""
    from position_runaway import find_excessive_qty_trades
    p = str(tmp_path / "p.db")
    _setup(p)
    # 40 option buys, median ~2
    for i in range(40):
        _add_option_trade(p, f"QCOM2606{i:02d}C00225000", (i % 4) + 1)
    # 10 stock buys, median ~100
    for sym in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]:
        _add_trade(p, sym, 100)
    # One open stock position at 150 shares (1.5× the stock median —
    # totally normal). Pre-fix this would flag as 75× the option-
    # polluted median (~2).
    _add_trade(p, "NORMAL_STOCK", 150)
    out = find_excessive_qty_trades(p, mult=5.0)
    flagged_syms = {e["symbol"] for e in out}
    assert "NORMAL_STOCK" not in flagged_syms, (
        f"Normal 150-share stock position flagged as runaway: {out}. "
        "The median must be per-instrument-class — 150 shares is 1.5× "
        "the stock median (~100), not 75× the option median (~2)."
    )


def test_excessive_stock_still_flagged_with_option_history(tmp_path):
    """A genuinely runaway stock position (10× the stock median) is
    still flagged even when option history is present."""
    from position_runaway import find_excessive_qty_trades
    p = str(tmp_path / "p.db")
    _setup(p)
    for i in range(40):
        _add_option_trade(p, f"QCOM2606{i:02d}C00225000", (i % 4) + 1)
    for sym in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]:
        _add_trade(p, sym, 100)
    _add_trade(p, "RUNAWAY_STOCK", 1000)  # 10× stock median
    out = find_excessive_qty_trades(p, mult=5.0)
    flagged = {e["symbol"]: e for e in out}
    assert "RUNAWAY_STOCK" in flagged
    assert flagged["RUNAWAY_STOCK"]["multiple"] == 10.0


def test_excessive_option_flagged_against_option_median(tmp_path):
    """An option position that's huge relative to the OPTION median
    (e.g., 50 contracts vs median 2) is still flagged — the per-class
    split protects option-runaway detection too."""
    from position_runaway import find_excessive_qty_trades
    p = str(tmp_path / "p.db")
    _setup(p)
    for i in range(40):
        _add_option_trade(p, f"QCOM2606{i:02d}C00225000", (i % 4) + 1)
    for sym in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]:
        _add_trade(p, sym, 100)
    _add_option_trade(p, "AAPL260101C00200000", 50)  # 25× option median
    out = find_excessive_qty_trades(p, mult=5.0)
    flagged_syms = {e["symbol"] for e in out}
    assert "AAPL" in flagged_syms, (
        "A 50-contract option position should flag against the option "
        "median (~2); per-class split must not blind option-runaway "
        "detection."
    )
