"""Guardrail: reconcile_trade_statuses must NOT close BUYs based on
the broker's open_symbols set alone — that path has a race condition
with the submit→fill window that destroyed P12/P13/P14 equity twice
on 2026-05-18.

Timeline that caused the bug (15:38 ET):
  T+0: P13 submits 5 BUY orders (SJM, CDNS, BLK, EW, LMT)
  T+0: P14 submits 7 BUY orders (INTC, LLY, KHC, ISRG, GD, DPZ, T)
  T+0: P12 submits 1 BUY order (322 SPY)
  T+2: Alpaca takes a moment to register each fill in list_positions
  T+3: reconcile_trade_statuses fires for P13. It calls
       api.list_positions() which only returns the orders that
       filled fastest — say {SPY} (P12's that landed first).
  T+3: Old SQL path: `UPDATE trades SET status='closed' WHERE
       side='buy' AND status='open' AND symbol NOT IN ('SPY')` →
       flips every P13 BUY (SJM, CDNS, BLK, EW, LMT) to closed.
  T+3: get_virtual_positions excludes status='closed' BUYs → P13
       shows 0 positions → dashboard equity = cash only (~$11K
       instead of $250K).

The fix (deployed 2026-05-18 15:55 ET) removes step 2's blanket SQL
entirely. _classify_long_phantom in reconcile_journal_to_broker.py
does per-trade reasoning by checking each BUY's order_id status
directly at the broker — only takes action when the order is truly
terminal. FIFO matching in step 3 still closes BUYs that have a
matching SELL with realized pnl. Together they cover every legitimate
close-detection case without the race-condition false positives.
"""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing


def _make_db(path: str) -> None:
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
        for i, sym in enumerate(("SJM", "CDNS", "BLK", "EW", "LMT")):
            conn.execute(
                "INSERT INTO trades(timestamp, symbol, side, qty, price, "
                "order_id, status) VALUES (?, ?, 'buy', ?, ?, ?, 'open')",
                (f"2026-05-18T15:38:{26+i:02d}", sym, 100.0, 100.0,
                 f"alpaca-order-{i:04d}-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            )
        conn.commit()


def _open_buys(path: str) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {r[0] for r in conn.execute(
            "SELECT symbol FROM trades WHERE side='buy' AND status='open'"
        ).fetchall()}


class TestRaceWindow:
    def test_partial_broker_response_during_submit_fill_window(self, tmp_path):
        """The exact 2026-05-18 race. 5 fresh BUYs in journal; broker
        only shows {SPY} (another profile's faster fill). Pre-fix
        behavior closed all 5 P13 BUYs because they're NOT IN {SPY}.
        Post-fix: all 5 must remain open."""
        from journal import reconcile_trade_statuses
        db = str(tmp_path / "p13.db")
        _make_db(db)
        assert _open_buys(db) == {"SJM", "CDNS", "BLK", "EW", "LMT"}
        reconcile_trade_statuses(db_path=db, open_symbols={"SPY"})
        # All 5 must still be open. Pre-fix this was set().
        assert _open_buys(db) == {"SJM", "CDNS", "BLK", "EW", "LMT"}, (
            "Race-window broker response wrongly closed open BUYs — "
            "the exact bug from 2026-05-18 15:38 that collapsed P13 "
            "equity from $250K to $11K."
        )

    def test_completely_empty_broker_response(self, tmp_path):
        """Same race, but broker returned an empty set entirely
        (slow Alpaca, transient outage, auth flake). Same expectation
        — leave BUYs alone."""
        from journal import reconcile_trade_statuses
        db = str(tmp_path / "p13b.db")
        _make_db(db)
        reconcile_trade_statuses(db_path=db, open_symbols=set())
        assert _open_buys(db) == {"SJM", "CDNS", "BLK", "EW", "LMT"}

    def test_broker_shows_full_set_no_change(self, tmp_path):
        """Sanity: even when broker shows all our BUYs, the function
        must not close any of them (no false-positive 'all match'
        path either)."""
        from journal import reconcile_trade_statuses
        db = str(tmp_path / "p13c.db")
        _make_db(db)
        reconcile_trade_statuses(
            db_path=db,
            open_symbols={"SJM", "CDNS", "BLK", "EW", "LMT"},
        )
        assert _open_buys(db) == {"SJM", "CDNS", "BLK", "EW", "LMT"}


class TestStructuralInvariant:
    def test_blanket_symbol_not_in_sql_pattern_absent(self):
        """AST scan: the blanket `symbol NOT IN (...)` SQL pattern in
        a status='closed' update on side='buy' must not return to
        journal.py. If this test fails, someone has re-introduced
        the race-condition path that caused two consecutive outages
        on 2026-05-18."""
        import re
        with open("journal.py", encoding="utf-8") as f:
            src = f.read()
        # The dangerous pattern: SET status='closed' on side='buy'
        # combined with `symbol NOT IN (...)` placeholder, in an
        # executable SQL string. Match the SQL fragment as written.
        bad = re.search(
            r"UPDATE\s+trades\s+SET\s+status\s*=\s*['\"]closed['\"]\s+"
            r"WHERE\s+side\s*=\s*['\"]buy['\"]\s+AND\s+status\s*=\s*['\"]open['\"]\s+"
            r"AND\s+symbol\s+NOT\s+IN",
            src, re.IGNORECASE,
        )
        assert bad is None, (
            f"Race-condition SQL is back in journal.py: "
            f"{bad.group(0)[:140]!r}. This is the pattern that "
            f"flipped fresh open BUYs to closed when the broker "
            f"hadn't yet registered the fill."
        )
