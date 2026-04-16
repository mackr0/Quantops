"""Tests for `virtual_audit.audit_virtual_profile` — data integrity checks."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def vdb(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    path = os.path.join(tmpdir, "audit.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            pnl REAL,
            status TEXT DEFAULT 'open'
        )
    """)
    conn.commit()
    conn.close()
    return path


def _trade(db, symbol, side, qty, price):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price) VALUES (?,?,?,?)",
        (symbol, side, qty, price),
    )
    conn.commit()
    conn.close()


class TestHealthyProfile:
    def test_no_problems_on_clean_profile_with_trades(self, vdb):
        from virtual_audit import audit_virtual_profile
        _trade(vdb, "AAPL", "buy", 10, 100)
        problems = audit_virtual_profile(vdb, initial_capital=10000)
        # Only issue should be the "no trades" check NOT firing
        assert not any("Accounting" in p for p in problems)
        assert not any("Negative" in p for p in problems)
        assert not any("Cash" in p for p in problems)

    def test_roundtrip_is_clean(self, vdb):
        from virtual_audit import audit_virtual_profile
        _trade(vdb, "AAPL", "buy", 10, 100)
        _trade(vdb, "AAPL", "sell", 10, 110)
        problems = audit_virtual_profile(vdb, initial_capital=10000)
        assert not any("Accounting" in p for p in problems)


class TestCatchesProblems:
    def test_flags_no_trades(self, vdb):
        from virtual_audit import audit_virtual_profile
        problems = audit_virtual_profile(vdb, initial_capital=10000)
        assert any("No trades" in p for p in problems)

    def test_flags_negative_cash(self, vdb):
        from virtual_audit import audit_virtual_profile
        # Buy way more than initial capital
        _trade(vdb, "AAPL", "buy", 1000, 100)  # $100K on $10K account
        problems = audit_virtual_profile(vdb, initial_capital=10000)
        assert any("negative" in p.lower() for p in problems)

    def test_deterministic_positions(self, vdb):
        from virtual_audit import audit_virtual_profile
        _trade(vdb, "AAPL", "buy", 10, 100)
        problems = audit_virtual_profile(vdb, initial_capital=10000)
        assert not any("deterministic" in p.lower() for p in problems)
