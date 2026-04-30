"""Wash-trade cooldown — when Alpaca rejects a BUY with 'potential
wash trade detected', record a 30-day cooldown so we don't re-attempt
every cycle.

History: 2026-04-30 prod log showed 'Trade execution raised for BP
(BUY): potential wash trade detected. use complex orders'. The
exception didn't crash (already wrapped), but it was logged as ERROR
and the system would re-try BP every cycle until the wash-detection
window cleared. Now we record the cooldown and skip cleanly.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _init_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE recently_exited_symbols (
            symbol TEXT PRIMARY KEY,
            exited_at TEXT NOT NULL DEFAULT (datetime('now')),
            trigger TEXT,
            exit_price REAL
        )
    """)
    conn.commit()
    conn.close()


def test_record_wash_cooldown_writes_row(tmp_path):
    from journal import record_wash_cooldown
    db = str(tmp_path / "trades.db")
    _init_db(db)
    record_wash_cooldown(db, "BP")
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT symbol, trigger FROM recently_exited_symbols WHERE symbol = 'BP'"
    ).fetchone()
    conn.close()
    assert row[0] == "BP"
    assert row[1] == "wash_cooldown"


def test_get_wash_cooldown_returns_recent_only(tmp_path):
    from journal import get_wash_cooldown_symbols
    db = str(tmp_path / "trades.db")
    _init_db(db)
    conn = sqlite3.connect(db)
    # One recent (within 30 days) and one old (40 days ago)
    conn.execute(
        "INSERT INTO recently_exited_symbols (symbol, exited_at, trigger) "
        "VALUES ('BP', datetime('now'), 'wash_cooldown')"
    )
    conn.execute(
        "INSERT INTO recently_exited_symbols (symbol, exited_at, trigger) "
        "VALUES ('XOM', datetime('now', '-40 days'), 'wash_cooldown')"
    )
    conn.commit()
    conn.close()
    syms = get_wash_cooldown_symbols(db, days=30)
    assert "BP" in syms
    assert "XOM" not in syms


def test_get_wash_cooldown_filters_by_trigger(tmp_path):
    """Only trigger='wash_cooldown' rows count — normal recent-exit
    rows shouldn't appear here."""
    from journal import get_wash_cooldown_symbols
    db = str(tmp_path / "trades.db")
    _init_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO recently_exited_symbols (symbol, exited_at, trigger) "
        "VALUES ('AAPL', datetime('now'), 'stop_loss')"
    )
    conn.execute(
        "INSERT INTO recently_exited_symbols (symbol, exited_at, trigger) "
        "VALUES ('BP', datetime('now'), 'wash_cooldown')"
    )
    conn.commit()
    conn.close()
    syms = get_wash_cooldown_symbols(db, days=30)
    assert syms == {"BP"}


def test_trade_pipeline_classifies_wash_error_as_skip():
    """Source-level pin: the error handler in trade_pipeline must
    special-case the wash-trade error so it logs as WARNING + SKIP
    rather than ERROR with traceback."""
    import inspect
    import trade_pipeline
    src = inspect.getsource(trade_pipeline)
    assert "wash trade" in src.lower(), (
        "trade_pipeline doesn't classify the wash-trade error — every "
        "occurrence will spam ERROR logs with full traceback."
    )
    assert "record_wash_cooldown" in src, (
        "trade_pipeline doesn't record a cooldown when wash is "
        "detected — system will re-attempt every cycle."
    )


def test_pre_filter_unions_wash_cooldown_with_recent_exit():
    """Source pin: the pre-filter loop must union recently_exited with
    wash_cooldown_symbols so wash-flagged BUYs get skipped before
    submission."""
    import inspect
    import trade_pipeline
    src = inspect.getsource(trade_pipeline)
    assert "get_wash_cooldown_symbols" in src, (
        "trade_pipeline doesn't query the wash-cooldown set in the "
        "pre-filter — wash-flagged symbols will keep getting submitted."
    )
