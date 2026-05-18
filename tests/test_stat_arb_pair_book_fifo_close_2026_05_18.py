"""Guardrail: stat_arb_pair_book pair-exit close must use FIFO walk,
not the blanket `UPDATE symbol=? AND strategy='pair_trade' AND
status='open'` pattern.

Same partial-close-overcloses bug class as multi_scheduler.py:1320
(fixed earlier 2026-05-18). A partial pair exit (close 30 of 100
shares of one leg) used to flip the original 100-share entry row to
closed, leaving the virtual book showing 0 shares when 70 were still
held.

Fixed in the same commit as the multi_scheduler.py:1374 audit pass.
"""
from __future__ import annotations

import re
import sqlite3
import tempfile
from contextlib import closing


def test_blanket_pair_close_pattern_absent():
    """AST scan: `UPDATE trades SET status='closed' WHERE symbol=? AND
    strategy='pair_trade' AND ... 'open'` without a FIFO marker
    (qty in nearby context) must not return to stat_arb_pair_book.py."""
    with open("stat_arb_pair_book.py", encoding="utf-8") as f:
        src = f.read()
    pat = re.compile(
        r"UPDATE\s+trades\s+SET\s+status\s*=\s*['\"]closed['\"]"
        r"[\s\S]{0,200}?"
        r"strategy\s*=\s*['\"]pair_trade['\"]"
        r"[\s\S]{0,150}?"
        r"['\"]open['\"]",
        re.IGNORECASE,
    )
    for m in pat.finditer(src):
        ctx = m.group(0)
        if "qty" in ctx.lower():
            continue  # FIFO-aware fix has qty references
        raise AssertionError(
            f"Blanket pair-trade close pattern back in "
            f"stat_arb_pair_book.py: {ctx[:200]!r}"
        )


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
                status TEXT,
                ai_confidence REAL,
                ai_reasoning TEXT
            )
        """)
        conn.commit()


def _insert(path: str, ts: str, symbol: str, side: str, qty: float,
            strategy: str = "pair_trade", status: str = "open") -> int:
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            "INSERT INTO trades(timestamp, symbol, side, qty, price, "
            "order_id, strategy, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, symbol, side, qty, 100.0,
             f"oid-{ts}-{symbol}", strategy, status),
        )
        conn.commit()
        return cur.lastrowid


def _status(path: str, tid: int) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT status FROM trades WHERE id=?", (tid,),
        ).fetchone()[0]


def _apply_pair_fifo_close(path: str, symbol: str, close_side: str) -> None:
    """Mirror of the new pair_book FIFO logic — exposed standalone so
    the test can drive it without spinning up the full pair book.

    Note pair_book's non-standard convention: SHORT entries are stored
    as side='sell' (vs. side='short' in the rest of the codebase).
    So opp_side = 'sell' when closing a short, 'buy' when closing a
    long."""
    opp_side = "sell" if close_side == "buy" else "buy"
    exit_side = close_side
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(
            "SELECT id, side, qty FROM trades "
            "WHERE symbol = ? "
            "  AND strategy = 'pair_trade' "
            "  AND side IN (?, ?) "
            "  AND COALESCE(status, 'open') != 'canceled' "
            "ORDER BY timestamp ASC, id ASC",
            (symbol, opp_side, exit_side),
        ).fetchall()
        lots = []
        for r in rows:
            r_side = r[1]
            r_qty = float(r[2] or 0)
            if r_side == opp_side:
                lots.append([r[0], r_qty])
            else:
                rem = r_qty
                for lot in lots:
                    if rem <= 0:
                        break
                    if lot[1] <= 0:
                        continue
                    consumed = min(lot[1], rem)
                    lot[1] -= consumed
                    rem -= consumed
        for lot_id, lot_rem in lots:
            if lot_rem <= 1e-6:
                conn.execute(
                    "UPDATE trades SET status='closed' "
                    "WHERE id = ? AND COALESCE(status,'open')='open'",
                    (lot_id,),
                )
        conn.commit()


def test_partial_pair_exit_leaves_entry_open(tmp_path):
    """BUY 100 KO (pair entry) + SELL 30 KO (partial pair exit).
    Pre-fix: BUY status flipped to closed despite 70 shares still held.
    Post-fix: BUY stays open with FIFO consuming 30 from its lot."""
    db = str(tmp_path / "p.db")
    _make_db(db)
    entry_id = _insert(db, "2026-05-18T10:00:00", "KO", "buy", 100.0)
    _insert(db, "2026-05-18T11:00:00", "KO", "sell", 30.0,
            status="closed")
    _apply_pair_fifo_close(db, "KO", close_side="sell")
    assert _status(db, entry_id) == "open", (
        "Partial pair exit wrongly closed the entry — the same "
        "class of bug as multi_scheduler.py:1320 pre-fix."
    )


def test_full_pair_exit_closes_entry(tmp_path):
    """BUY 100 + SELL 100 — entry should close."""
    db = str(tmp_path / "p.db")
    _make_db(db)
    entry_id = _insert(db, "2026-05-18T10:00:00", "KO", "buy", 100.0)
    _insert(db, "2026-05-18T11:00:00", "KO", "sell", 100.0,
            status="closed")
    _apply_pair_fifo_close(db, "KO", close_side="sell")
    assert _status(db, entry_id) == "closed"


def test_short_leg_partial_cover_leaves_short_open(tmp_path):
    """Pair-book SHORT (stored as side='sell') 100 + cover BUY 30
    (partial). SHORT entry stays open with 70 remaining."""
    db = str(tmp_path / "p.db")
    _make_db(db)
    # pair_book convention: SHORT entries are side='sell'
    short_id = _insert(db, "2026-05-18T10:00:00", "PEP", "sell", 100.0)
    _insert(db, "2026-05-18T11:00:00", "PEP", "buy", 30.0, status="closed")
    _apply_pair_fifo_close(db, "PEP", close_side="buy")
    assert _status(db, short_id) == "open"
