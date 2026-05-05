"""Stop-order coverage monitor tests."""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _seed_profile(path, longs):
    from journal import init_db
    init_db(path)
    conn = sqlite3.connect(path)
    for sym, has_stop in longs:
        sid = "stop-id-1234" if has_stop else None
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, "
            "protective_stop_order_id, status) "
            "VALUES (?, 'buy', 10, 100, ?, 'open')",
            (sym, sid),
        )
    conn.commit()
    conn.close()


def test_no_open_longs_returns_100_pct():
    from stop_coverage import coverage_snapshot
    snap = coverage_snapshot(db_paths=[])
    assert snap["total_longs"] == 0
    assert snap["coverage_pct"] == 100.0


def test_all_covered(tmp_path):
    from stop_coverage import coverage_snapshot
    p = str(tmp_path / "p1.db")
    _seed_profile(p, [("AAPL", True), ("MSFT", True), ("GOOGL", True)])
    snap = coverage_snapshot(db_paths=[p])
    assert snap["coverage_pct"] == 100.0


def test_partial(tmp_path):
    from stop_coverage import coverage_snapshot
    p = str(tmp_path / "p1.db")
    _seed_profile(p, [("AAPL", True), ("MSFT", True), ("GOOGL", False), ("META", False)])
    snap = coverage_snapshot(db_paths=[p])
    assert snap["covered"] == 2
    assert snap["coverage_pct"] == 50.0
    assert sorted(s for _, s in snap["naked_symbols"]) == ["GOOGL", "META"]


def test_floor_breach(tmp_path):
    from stop_coverage import check_coverage_floor
    p = str(tmp_path / "p1.db")
    _seed_profile(p, [("AAPL", True)] + [(f"BAD{i}", False) for i in range(4)])
    snap = check_coverage_floor(floor_pct=80.0, db_paths=[p])
    assert snap["coverage_pct"] == 20.0
    assert snap["breached"] is True


def test_floor_within(tmp_path):
    from stop_coverage import check_coverage_floor
    p = str(tmp_path / "p1.db")
    _seed_profile(p, [("AAPL", True), ("MSFT", True), ("GOOGL", True), ("META", True), ("AMZN", False)])
    snap = check_coverage_floor(floor_pct=80.0, db_paths=[p])
    assert snap["coverage_pct"] == 80.0
    assert snap["breached"] is False


def test_trailing_id_counts(tmp_path):
    from stop_coverage import coverage_snapshot
    p = str(tmp_path / "p1.db")
    from journal import init_db
    init_db(p)
    conn = sqlite3.connect(p)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price, "
        "protective_trailing_order_id, status) "
        "VALUES ('AAPL', 'buy', 10, 100, 'trailing-x', 'open')",
    )
    conn.commit()
    conn.close()
    snap = coverage_snapshot(db_paths=[p])
    assert snap["covered"] == 1


def test_shorts_excluded(tmp_path):
    from stop_coverage import coverage_snapshot
    p = str(tmp_path / "p1.db")
    from journal import init_db
    init_db(p)
    conn = sqlite3.connect(p)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price, status) "
        "VALUES ('TSLA', 'sell', 10, 100, 'open')",
    )
    conn.commit()
    conn.close()
    snap = coverage_snapshot(db_paths=[p])
    assert snap["total_longs"] == 0


def test_multi_profile(tmp_path):
    from stop_coverage import coverage_snapshot
    p1 = str(tmp_path / "p1.db")
    p2 = str(tmp_path / "p2.db")
    _seed_profile(p1, [("AAPL", True), ("MSFT", False)])
    _seed_profile(p2, [("GOOGL", True), ("META", True), ("AMZN", False)])
    snap = coverage_snapshot(db_paths=[p1, p2])
    assert snap["total_longs"] == 5
    assert snap["covered"] == 3
    assert snap["coverage_pct"] == 60.0
