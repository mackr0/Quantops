"""Guardrail: reconcile_trade_statuses must NOT close every open BUY
when the broker returns an empty position list.

Pre-fix bug (caught 2026-05-18 13:30 ET): the function had two
branches gated on `if open_symbols:`. An empty set is falsy → fell
into the else branch → `UPDATE trades SET status='closed' WHERE
side='buy' AND status='open'` with no symbol filter → closed every
open BUY across the profile. Within minutes of A1's first reconcile
cycle this collapsed displayed equity from $3M → $2.27M by hiding
~$730K of real positions behind status='closed' (get_virtual_positions
excludes status='closed' BUYs).

This test pins the structural property — the close-everything branch
must never fire when the broker's response is empty — and would have
caught the pre-fix code.
"""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing

import pytest


def _make_minimal_db(path: str) -> None:
    """Create the minimum trades schema reconcile_trade_statuses needs."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                symbol TEXT,
                side TEXT,
                qty REAL,
                price REAL,
                order_id TEXT,
                signal_type TEXT,
                strategy TEXT,
                reason TEXT,
                ai_reasoning TEXT,
                ai_confidence REAL,
                stop_loss REAL,
                take_profit REAL,
                status TEXT,
                pnl REAL,
                decision_price REAL,
                fill_price REAL
            )
        """)
        # 3 fresh open BUYs — real broker order IDs, no matching SELLs
        for i, sym in enumerate(["AAPL", "MSFT", "NVDA"]):
            conn.execute(
                "INSERT INTO trades(timestamp, symbol, side, qty, price, "
                "order_id, status) VALUES (?, ?, 'buy', ?, ?, ?, 'open')",
                (f"2026-05-18T13:30:{50+i:02d}", sym, 10.0, 100.0,
                 f"alpaca-order-{i}"),
            )
        conn.commit()


def _open_buys(path: str) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM trades WHERE side='buy' AND status='open'"
        ).fetchone()[0]


class TestReconcileEmptyBrokerResponse:
    def test_empty_set_does_not_close_all_buys(self, tmp_path):
        """The exact regression: passing open_symbols=set() (broker
        returned empty) must NOT close every open BUY. Pre-fix code
        closed all 3 → fix preserves all 3."""
        from journal import reconcile_trade_statuses
        db = str(tmp_path / "journal.db")
        _make_minimal_db(db)
        assert _open_buys(db) == 3
        reconcile_trade_statuses(db_path=db, open_symbols=set())
        # All 3 BUYs must still be open. Pre-fix this was 0.
        assert _open_buys(db) == 3, (
            "Empty broker response wrongly closed open BUYs — the "
            "exact bug from 2026-05-18 that hid $730K of real positions "
            "from the dashboard."
        )

    def test_non_empty_set_no_longer_closes_unlisted(self, tmp_path):
        """The "broker says symbol not in positions → close BUY" path
        was REMOVED 2026-05-18 (the second-outage fix). Race window
        between submit and broker fill registration made it unsafe:
        a fresh BUY whose order is still mid-flight at the broker
        would get closed wrongly. Per-trade reasoning lives in
        reconcile_journal_to_broker._classify_long_phantom now."""
        from journal import reconcile_trade_statuses
        db = str(tmp_path / "journal.db")
        _make_minimal_db(db)
        reconcile_trade_statuses(db_path=db, open_symbols={"AAPL"})
        with closing(sqlite3.connect(db)) as conn:
            rows = dict(conn.execute(
                "SELECT symbol, status FROM trades ORDER BY symbol"
            ).fetchall())
        # All three stay open — the function no longer flips BUY
        # status based on broker's open_symbols set alone.
        assert rows == {"AAPL": "open", "MSFT": "open", "NVDA": "open"}

    def test_none_falls_back_to_fifo_path(self, tmp_path):
        """Sanity: open_symbols=None (caller didn't query broker)
        must use the FIFO-match path, not touch BUYs without a
        matching SELL."""
        from journal import reconcile_trade_statuses
        db = str(tmp_path / "journal.db")
        _make_minimal_db(db)
        reconcile_trade_statuses(db_path=db, open_symbols=None)
        # No matching SELLs exist, so all BUYs stay open.
        assert _open_buys(db) == 3


class TestStructuralInvariant:
    """AST scan: any future SQL UPDATE on trades.status='closed' that
    writes side='buy' rows MUST be either (a) gated on a non-empty
    open_symbols set, or (b) only fire on rows with a matching closed
    SELL. Catches the class of bug, not the specific instance."""

    def test_close_all_buys_pattern_is_absent_from_journal(self):
        """The unguarded UPDATE pattern must not return in journal.py.
        If this test fails the someone has re-added a 'close every
        open buy' branch — exactly what we just deleted."""
        import re
        with open("journal.py", encoding="utf-8") as f:
            src = f.read()
        # Pattern: UPDATE trades SET status='closed' WHERE side='buy'
        # AND status='open' (no symbol filter, no JOIN to sell, no
        # placeholder). Allow whitespace + quote variations.
        pat = re.compile(
            r"UPDATE\s+trades\s+SET\s+status\s*=\s*['\"]closed['\"]\s+"
            r"WHERE\s+side\s*=\s*['\"]buy['\"]\s+AND\s+status\s*=\s*['\"]open['\"]\s*['\"]?\s*[,)]",
            re.IGNORECASE,
        )
        m = pat.search(src)
        assert m is None, (
            f"Unguarded 'close every open buy' SQL re-appeared in "
            f"journal.py: {m.group(0)[:120]!r}. The 2026-05-18 bug "
            f"is back — this SQL closes real positions every time the "
            f"broker returns an empty list."
        )
