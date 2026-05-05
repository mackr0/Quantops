"""Catastrophic single-trade gate tests."""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(path):
    from journal import init_db
    init_db(path)


def _add_trade(path, qty, price):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price, status) "
        "VALUES ('X', 'buy', ?, ?, 'open')",
        (qty, price),
    )
    conn.commit()
    conn.close()


def test_no_history_returns_no_baseline(tmp_path):
    from single_trade_gate import is_catastrophic
    p = str(tmp_path / "p.db")
    _setup(p)
    cat, reason, detail = is_catastrophic(10_000_000, p)
    assert cat is False
    # No history = no baseline; sample_size is 0 in detail
    assert detail["avg_recent_value"] is None


def test_within_cap(tmp_path):
    from single_trade_gate import is_catastrophic
    p = str(tmp_path / "p.db")
    _setup(p)
    # 6 trades averaging $1000 each
    for _ in range(6):
        _add_trade(p, 10, 100)
    cat, _, detail = is_catastrophic(2_000, p, mult=5.0)
    assert cat is False
    assert detail["multiple"] == 2.0


def test_exactly_at_cap_not_catastrophic(tmp_path):
    """5x exactly is NOT catastrophic; >5x is."""
    from single_trade_gate import is_catastrophic
    p = str(tmp_path / "p.db")
    _setup(p)
    for _ in range(6):
        _add_trade(p, 10, 100)  # avg $1000
    cat, _, _ = is_catastrophic(5_000, p, mult=5.0)
    assert cat is False


def test_above_cap_catastrophic(tmp_path):
    from single_trade_gate import is_catastrophic
    p = str(tmp_path / "p.db")
    _setup(p)
    for _ in range(6):
        _add_trade(p, 10, 100)  # avg $1000
    cat, reason, detail = is_catastrophic(10_000, p, mult=5.0)
    assert cat is True
    assert "10.0×" in reason or "10.0x" in reason or detail["multiple"] == 10.0


def test_detail_carries_sample_size(tmp_path):
    from single_trade_gate import is_catastrophic
    p = str(tmp_path / "p.db")
    _setup(p)
    for _ in range(8):
        _add_trade(p, 5, 200)  # avg $1000
    _, _, detail = is_catastrophic(20_000, p, mult=5.0)
    assert detail["avg_recent_value"] == 1000.0
    assert detail["threshold"] == 5000.0
    assert detail["multiple"] == 20.0
