"""Guardrail: when a SELL/COVER fill confirms via _task_update_fill_prices,
only entry lots whose qty has been fully consumed by exits should flip
to status='closed'. Pre-2026-05-18 the code unconditionally flipped
EVERY open BUY (or SHORT) for the symbol — a partial sell wiped
multiple lots' worth of legitimately-open entries.

This test exercises the FIFO logic directly against a stub SQLite DB
that mirrors the trades schema, so we don't have to spin up the full
multi_scheduler. The structural AST guardrail at the bottom prevents
the buggy `WHERE symbol=? AND side=? AND status='open'` (no qty/FIFO)
pattern from being re-introduced.
"""
from __future__ import annotations

import sqlite3
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
                status TEXT
            )
        """)
        conn.commit()


def _insert(path: str, ts: str, symbol: str, side: str, qty: float,
            status: str = "open") -> int:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            "INSERT INTO trades(timestamp, symbol, side, qty, price, "
            "order_id, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, symbol, side, qty, 100.0, f"oid-{ts}", status),
        )
        conn.commit()
        return cur.lastrowid


def _status(path: str, tid: int) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT status FROM trades WHERE id=?", (tid,),
        ).fetchone()[0]


def _apply_fifo_close(path: str, trade_id: int, symbol: str,
                       exit_side: str, opp_side: str) -> None:
    """Mirror of the new logic in multi_scheduler._task_update_fill_prices
    (the FIFO close path). Run as a standalone function so the test
    doesn't have to import the whole scheduler module."""
    with closing(sqlite3.connect(path)) as conn:
        # Close the exit itself
        conn.execute(
            "UPDATE trades SET status = 'closed' WHERE id = ?",
            (trade_id,),
        )
        rows = conn.execute(
            "SELECT id, side, qty FROM trades "
            "WHERE symbol = ? AND side IN (?, ?) "
            "  AND COALESCE(status, 'open') != 'canceled' "
            "ORDER BY timestamp ASC, id ASC",
            (symbol, opp_side, exit_side),
        ).fetchall()
        lots = []
        for r in rows:
            side_i = r[1]
            qty_i = float(r[2] or 0)
            if side_i == opp_side:
                lots.append([r[0], qty_i])
            else:
                remaining = qty_i
                for lot in lots:
                    if remaining <= 0:
                        break
                    if lot[1] <= 0:
                        continue
                    consumed = min(lot[1], remaining)
                    lot[1] -= consumed
                    remaining -= consumed
        for lot_id, lot_remaining in lots:
            if lot_remaining <= 1e-6:
                conn.execute(
                    "UPDATE trades SET status = 'closed' "
                    "WHERE id = ? AND COALESCE(status, 'open') = 'open'",
                    (lot_id,),
                )
        conn.commit()


class TestPartialSellDoesNotOverclose:
    def test_partial_sell_leaves_buy_open(self, tmp_path):
        """The P12 (BuyHoldSPY) scenario from 2026-05-18: BUY 322 +
        BUY 16, then SELL 16. Pre-fix code closed BOTH BUYs; only
        16 of 338 shares were actually sold. Correct: BOTH BUYs
        stay open (FIFO at read time correctly shows 322 SPY held)."""
        db = str(tmp_path / "p12.db")
        _make_db(db)
        b1 = _insert(db, "2026-05-18T15:38:26", "SPY", "buy", 322.0)
        b2 = _insert(db, "2026-05-18T15:45:26", "SPY", "buy", 16.0)
        s1 = _insert(db, "2026-05-18T15:51:46", "SPY", "sell", 16.0,
                     status="pending_fill")
        _apply_fifo_close(db, s1, "SPY", exit_side="sell", opp_side="buy")
        # The SELL itself closed
        assert _status(db, s1) == "closed"
        # Both BUYs stay OPEN — only 16 of 322 from b1 was consumed
        assert _status(db, b1) == "open", (
            "Partial sell wrongly closed the 322-share BUY — this "
            "is the 2026-05-18 P12 bug returning."
        )
        assert _status(db, b2) == "open"

    def test_full_close_via_single_sell_closes_buy(self, tmp_path):
        """Sanity: a SELL that fully consumes a BUY lot does close
        that lot. BUY 100 + SELL 100 → both closed."""
        db = str(tmp_path / "p2.db")
        _make_db(db)
        b1 = _insert(db, "2026-05-18T10:00:00", "AAPL", "buy", 100.0)
        s1 = _insert(db, "2026-05-18T11:00:00", "AAPL", "sell", 100.0,
                     status="pending_fill")
        _apply_fifo_close(db, s1, "AAPL", exit_side="sell", opp_side="buy")
        assert _status(db, s1) == "closed"
        assert _status(db, b1) == "closed"

    def test_full_close_across_multiple_lots(self, tmp_path):
        """BUY 100 + BUY 50 + SELL 120 → b1 fully consumed, b2 partial.
        Only b1 should close; b2 stays open with 30 shares remaining."""
        db = str(tmp_path / "p3.db")
        _make_db(db)
        b1 = _insert(db, "2026-05-18T10:00:00", "AAPL", "buy", 100.0)
        b2 = _insert(db, "2026-05-18T10:05:00", "AAPL", "buy", 50.0)
        s1 = _insert(db, "2026-05-18T11:00:00", "AAPL", "sell", 120.0,
                     status="pending_fill")
        _apply_fifo_close(db, s1, "AAPL", exit_side="sell", opp_side="buy")
        assert _status(db, b1) == "closed"
        assert _status(db, b2) == "open"

    def test_short_cover_pair_same_pattern(self, tmp_path):
        """SHORT 100 + COVER 30 (partial) → SHORT stays open."""
        db = str(tmp_path / "p4.db")
        _make_db(db)
        sh = _insert(db, "2026-05-18T10:00:00", "TSLA", "short", 100.0)
        cv = _insert(db, "2026-05-18T11:00:00", "TSLA", "cover", 30.0,
                     status="pending_fill")
        _apply_fifo_close(db, cv, "TSLA", exit_side="cover",
                          opp_side="short")
        assert _status(db, cv) == "closed"
        assert _status(db, sh) == "open"

    def test_short_fully_covered_closes(self, tmp_path):
        """SHORT 100 + COVER 100 → both closed."""
        db = str(tmp_path / "p5.db")
        _make_db(db)
        sh = _insert(db, "2026-05-18T10:00:00", "TSLA", "short", 100.0)
        cv = _insert(db, "2026-05-18T11:00:00", "TSLA", "cover", 100.0,
                     status="pending_fill")
        _apply_fifo_close(db, cv, "TSLA", exit_side="cover",
                          opp_side="short")
        assert _status(db, sh) == "closed"
        assert _status(db, cv) == "closed"

    def test_two_sells_summing_to_full_close(self, tmp_path):
        """BUY 100 + SELL 60 (already closed) + SELL 40 (just confirming).
        The second sell finishes consuming the lot → BUY closes."""
        db = str(tmp_path / "p6.db")
        _make_db(db)
        b1 = _insert(db, "2026-05-18T10:00:00", "AAPL", "buy", 100.0)
        _insert(db, "2026-05-18T11:00:00", "AAPL", "sell", 60.0,
                status="closed")
        s2 = _insert(db, "2026-05-18T12:00:00", "AAPL", "sell", 40.0,
                     status="pending_fill")
        _apply_fifo_close(db, s2, "AAPL", exit_side="sell", opp_side="buy")
        assert _status(db, b1) == "closed"


class TestStructuralInvariant:
    def test_blanket_status_open_close_pattern_absent(self):
        """AST scan: the buggy `UPDATE trades SET status='closed'
        WHERE symbol=? AND side=? AND ... = 'open'` pattern (no qty
        filter, no FIFO) must not return to multi_scheduler.py.
        Match a status='closed' write that mentions both a symbol
        parameter and a side parameter but has no qty/FIFO marker."""
        import re
        with open("multi_scheduler.py", encoding="utf-8") as f:
            src = f.read()
        # Look for any UPDATE trades that closes by symbol+side+status='open'
        # without referencing 'qty' nearby — the smoking gun for the
        # blanket-close pattern.
        pat = re.compile(
            r"UPDATE\s+trades\s+SET\s+status\s*=\s*['\"]closed['\"]"
            r"[\s\S]{0,250}?"
            r"WHERE\s+symbol\s*=\s*\?\s+AND\s+side\s*=\s*\?"
            r"[\s\S]{0,150}?"
            r"['\"]open['\"]",
            re.IGNORECASE,
        )
        for m in pat.finditer(src):
            ctx = m.group(0)
            if "qty" in ctx.lower():
                continue  # FIFO-aware fix has qty in surrounding code
            raise AssertionError(
                f"Blanket symbol+side close pattern back in "
                f"multi_scheduler.py: {ctx[:200]!r}"
            )
