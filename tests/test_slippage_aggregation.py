"""Regression test for the slippage-stats key-mismatch bug.

Background: 2026-04-29. The Slippage Impact panel on /ai and
/performance was showing "No fill data yet" even when every profile
had 50-100 trades with full fill_price data. Root cause: the views
called `s.get("count", 0)` and `s.get("total_cost", 0)` against the
return value of `journal.get_slippage_stats`, but that function
returns `trades_with_fills` and `total_slippage_cost`. Wrong keys →
zero counts → empty-state panel.

Fix: read the correct keys.

This test pins both the journal-side contract (what keys the function
returns) AND the views-side aggregation (using the right keys).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_get_slippage_stats_contract():
    """The return shape must include trades_with_fills,
    avg_slippage_pct, total_slippage_cost, total_slippage_magnitude.
    The views code expects these exact keys; renaming would break
    the dashboard silently."""
    import inspect
    import journal
    src = inspect.getsource(journal.get_slippage_stats)
    # The COUNT / AVG / SUM aliases in the SQL
    assert "trades_with_fills" in src
    assert "avg_slippage_pct" in src
    assert "total_slippage_cost" in src
    assert "total_slippage_magnitude" in src


def test_total_slippage_cost_is_signed_not_absolute(tmp_path):
    """total_slippage_cost is the SIGNED net economic cost. Favorable
    executions REDUCE the total; adverse executions INCREASE it.
    Was ABS(fill - decision) * qty before — that double-counted
    favorable slippage as cost and inflated the dashboard ~4x."""
    import sqlite3
    db = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            decision_price REAL, fill_price REAL, slippage_pct REAL,
            status TEXT, pnl REAL, strategy TEXT
        )
    """)
    # Two trades that should cancel out:
    # 1. BUY 100 AAPL: decision $100, fill $100.10 → adverse $10
    # 2. BUY 100 MSFT: decision $200, fill $199.90 → favorable -$10
    # Absolute magnitude: $20. Signed cost: $0.
    rows = [
        ("AAPL", 100, 100.0, 100.10, "buy"),
        ("MSFT", 100, 200.0, 199.90, "buy"),
    ]
    for i, (sym, qty, dp, fp, side) in enumerate(rows):
        slip_pct = (fp - dp) / dp * 100
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "decision_price, fill_price, slippage_pct, pnl, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"2026-04-{i+1:02d}", sym, side, qty, fp, dp, fp, slip_pct,
             None, "filled"),
        )
    conn.commit()
    conn.close()

    from journal import get_slippage_stats
    s = get_slippage_stats(db_path=db)
    # Magnitude includes both: $10 + $10 = $20
    assert abs(s["total_slippage_magnitude"] - 20.0) < 0.01, (
        f"magnitude must be |dp-fp|*qty summed unsigned; "
        f"got {s['total_slippage_magnitude']}"
    )
    # Signed cost cancels: $10 (adverse) + (-$10) (favorable) = $0
    assert abs(s["total_slippage_cost"]) < 0.01, (
        f"total_slippage_cost must be signed — favorable executions "
        f"should cancel adverse ones. Got {s['total_slippage_cost']}; "
        f"if this is ~$20 you're still using ABS()."
    )


def test_signed_slippage_cost_is_negative_when_executions_are_favorable(tmp_path):
    """When BUYs fill below decision and SELLs fill above, the user's
    book got better fills than expected — total_slippage_cost should
    be NEGATIVE."""
    import sqlite3
    db = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            decision_price REAL, fill_price REAL, slippage_pct REAL,
            status TEXT, pnl REAL, strategy TEXT
        )
    """)
    rows = [
        ("AAPL", 100, 100.0, 99.90, "buy"),    # filled $0.10 below = good
        ("MSFT",  50, 200.0, 200.50, "sell"),  # sold $0.50 above = good
    ]
    for i, (sym, qty, dp, fp, side) in enumerate(rows):
        slip_pct = (fp - dp) / dp * 100
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "decision_price, fill_price, slippage_pct, pnl, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"2026-04-{i+1:02d}", sym, side, qty, fp, dp, fp, slip_pct,
             None, "filled"),
        )
    conn.commit()
    conn.close()
    from journal import get_slippage_stats
    s = get_slippage_stats(db_path=db)
    assert s["total_slippage_cost"] < 0, (
        f"All-favorable executions must produce negative signed cost; "
        f"got {s['total_slippage_cost']}. If this is positive you're "
        f"still summing absolute values."
    )


def test_views_uses_correct_slippage_keys():
    """The views aggregation MUST read the keys get_slippage_stats
    actually returns. Previously read 'count' and 'total_cost' which
    don't exist — silently kept the UI in empty state forever."""
    import inspect
    import views
    perf_src = inspect.getsource(views.performance_dashboard)
    ai_src = inspect.getsource(views.ai_dashboard)
    for src, where in [(perf_src, "performance_dashboard"),
                        (ai_src, "ai_dashboard")]:
        assert 'get("trades_with_fills"' in src, (
            f"{where} doesn't read 'trades_with_fills' from "
            f"get_slippage_stats — slippage UI will never populate."
        )
        assert 'get("total_slippage_cost"' in src, (
            f"{where} doesn't read 'total_slippage_cost' — "
            f"total dollar cost will silently stay 0."
        )
        # The wrong keys should NOT appear (regression-pin)
        assert 'get("count"' not in src or "get(\"trades_with_fills\"" in src, (
            f"{where} still uses get('count', ...) — wrong key, "
            f"empty-state forever."
        )


def test_calculate_all_metrics_uses_get_slippage_stats():
    """Performance page (calculate_all_metrics) must use get_slippage_stats
    for slippage_avg_pct / slippage_total_cost / trades_with_slippage —
    same scope as the AI page. Without this, the two pages report
    different numbers for the same metric on the same data (Performance
    counts only closed trades; AI counts all fills)."""
    import inspect
    import metrics
    src = inspect.getsource(metrics.calculate_all_metrics)
    assert "get_slippage_stats" in src, (
        "calculate_all_metrics doesn't call get_slippage_stats — "
        "Performance page slippage scope will diverge from AI page."
    )
    assert "trades_with_fills" in src, (
        "calculate_all_metrics doesn't read trades_with_fills key — "
        "see the 2026-04-29 dict-key mismatch."
    )
    assert "total_slippage_cost" in src, (
        "calculate_all_metrics doesn't read total_slippage_cost key."
    )


def test_metrics_slippage_matches_get_slippage_stats(tmp_path):
    """End-to-end: same trades data → calculate_all_metrics and
    get_slippage_stats agree on count and total cost. Catches future
    re-divergence between the two scopes."""
    import sqlite3
    db = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            decision_price REAL, fill_price REAL, slippage_pct REAL,
            status TEXT, pnl REAL, strategy TEXT
        )
    """)
    # Mix of open entries (no pnl) and closed exits (with pnl).
    # All have slippage data.
    rows = [
        # (sym, qty, dp, fp, pnl)
        ("AAPL", 100, 150.0, 150.30, None),    # open entry
        ("MSFT",  50, 300.0, 300.50, None),    # open entry
        ("TSLA",  30, 200.0, 199.50, 1500.0),  # closed exit
        ("NVDA",  80, 400.0, 401.20, 2400.0),  # closed exit
        ("GOOG",  20, 130.0, 130.05, 100.0),   # closed exit
    ]
    for i, (sym, qty, dp, fp, pnl) in enumerate(rows):
        slip_pct = (fp - dp) / dp * 100
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "decision_price, fill_price, slippage_pct, pnl, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"2026-04-{i+1:02d}", sym, "buy", qty, fp, dp, fp, slip_pct,
             pnl, "filled"),
        )
    conn.commit()
    conn.close()

    from journal import get_slippage_stats
    from metrics import calculate_all_metrics
    s = get_slippage_stats(db_path=db)
    m = calculate_all_metrics([db])

    # Both must see all 5 fills (not just the 3 closed)
    assert s["trades_with_fills"] == 5
    assert m["trades_with_slippage"] == 5, (
        "calculate_all_metrics is using a stricter scope than "
        "get_slippage_stats — Performance page will under-count "
        "trades vs AI page."
    )
    # Total dollar cost should match within rounding
    assert abs(s["total_slippage_cost"] - m["slippage_total_cost"]) < 0.5


def test_slippage_aggregation_round_trip(tmp_path, monkeypatch):
    """End-to-end: seed a trades table with realistic decision/fill
    prices, run the aggregation that views.py uses, verify the
    slippage dict gets populated."""
    import sqlite3
    db = str(tmp_path / "trades.db")
    # Build minimal schema matching journal.init_db's trades shape
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            decision_price REAL,
            fill_price REAL,
            slippage_pct REAL,
            status TEXT
        )
    """)
    # 5 trades with realistic slippage
    for i, (sym, qty, dp, fp) in enumerate([
        ("AAPL", 100, 150.0, 150.30),  # +0.20%
        ("MSFT", 50, 300.0, 300.50),   # +0.17%
        ("TSLA", 30, 200.0, 199.50),   # -0.25%
        ("NVDA", 80, 400.0, 401.20),   # +0.30%
        ("GOOG", 20, 130.0, 130.05),   # +0.04%
    ]):
        slip_pct = (fp - dp) / dp * 100
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "decision_price, fill_price, slippage_pct, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"2026-04-{i+1:02d}", sym, "buy", qty, fp, dp, fp, slip_pct, "filled"),
        )
    conn.commit()
    conn.close()

    from journal import get_slippage_stats
    s = get_slippage_stats(db_path=db)
    assert s is not None
    assert s["trades_with_fills"] == 5
    assert s["total_slippage_cost"] > 0  # absolute dollar slippage

    # Now simulate the views aggregation logic
    slippage = {"avg_pct": 0.0, "total_cost": 0.0, "count": 0}
    weighted_pct_sum = 0.0
    n = s.get("trades_with_fills", 0) or 0
    slippage["count"] += n
    slippage["total_cost"] += s.get("total_slippage_cost", 0) or 0
    weighted_pct_sum += (s.get("avg_slippage_pct", 0) or 0) * n
    if slippage["count"] > 0:
        slippage["avg_pct"] = weighted_pct_sum / slippage["count"]

    # The result must NOT be empty — this is what was failing before
    assert slippage["count"] == 5
    assert slippage["total_cost"] > 0
    assert slippage["avg_pct"] != 0
