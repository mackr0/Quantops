"""Test the safety-net backfill script that resolves orphan trailing-stop
fills accumulated 2026-05-28 through 2026-06-03 across 10 profiles.

The script is the fallback for cases the structural replace-chain fix
in reconcile_journal_to_broker can't auto-resolve (broken chain links,
stale entry pointers). Tests pin:
  1. Audit-alert detail parsing extracts every backfill_sell line.
  2. _resolve_orphan finds pending + entry rows, computes pnl, and
     identifies stale sibling pending_protective rows.
  3. _apply_plan flips pending to closed, closes entry with pnl, and
     marks stale siblings as canceled.
  4. Verify-or-refuse: missing pending row OR missing entry row -> skip.
  5. Idempotent: running twice doesn't double-apply.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "scripts")))


SAMPLE_DETAIL = """\
The reconciler detected broker fill(s) that would have required SYNTHESIZING journal rows. Per the atomic-journaling contract, this indicates a submit_order code path failed to journal in-line.

  backfill_sell: CRM qty=127.0 sell_order=df787e44 @ $172.84 (trailing_stop)
  backfill_sell: V qty=76.0 sell_order=5e6a03a6 @ $316.50 (trailing_stop)
  backfill_sell: SCHW qty=174.0 sell_order=a9d787db @ $86.81 (trailing_stop)
"""


def _journal_with_orphan(tmp_path, symbol="CRM", qty=127.0, entry_price=175.00,
                         num_stale_siblings: int = 2):
    """Build a journal DB mirroring the pid15 CRM orphan shape: an open
    BUY entry, the latest pending_protective row, and N stale sibling
    pending_protective rows from prior replace cycles."""
    db = tmp_path / "profile.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            order_id TEXT,
            signal_type TEXT,
            reason TEXT,
            status TEXT,
            pnl REAL,
            fill_price REAL,
            occ_symbol TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    # Open entry BUY (id=25).
    conn.execute(
        "INSERT INTO trades (id, timestamp, symbol, side, qty, price, "
        "order_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (25, "2026-05-20T14:11:22", symbol, "buy", qty, entry_price,
         "entry-buy-oid", "open"),
    )
    # Stale sibling pending_protective rows (older entries in the
    # replace chain that never got closed when Alpaca replaced them).
    sibling_ids = []
    for i in range(num_stale_siblings):
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, "
            "order_id, status, signal_type) "
            "VALUES (?, ?, 'sell', ?, ?, 'pending_protective', ?)",
            (f"2026-05-{28 + i}T13:00:00", symbol, qty,
             f"stale-sibling-{i}", "PROTECTIVE_TRAILING"),
        )
        sibling_ids.append(conn.execute(
            "SELECT last_insert_rowid()").fetchone()[0])
    # NEWEST pending_protective row (the one the backfill targets).
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, order_id, "
        "status, signal_type) "
        "VALUES (?, ?, 'sell', ?, ?, 'pending_protective', ?)",
        ("2026-06-03T13:57:06", symbol, qty,
         "newest-placement-oid", "PROTECTIVE_TRAILING"),
    )
    pending_id = conn.execute(
        "SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return str(db), {"entry_id": 25, "pending_id": pending_id,
                     "sibling_ids": sibling_ids}


# ---------------------------------------------------------------------------
# 1. Parsing
# ---------------------------------------------------------------------------

def test_parse_orphans_extracts_all_backfill_lines():
    from backfill_orphan_trailing_stops_2026_06_04 import _parse_orphans
    out = _parse_orphans(SAMPLE_DETAIL)
    assert len(out) == 3
    assert out[0] == {
        "symbol": "CRM", "qty": 127.0, "sell_order": "df787e44",
        "sell_price": 172.84, "order_type": "trailing_stop",
    }
    assert out[1]["symbol"] == "V"
    assert out[2]["symbol"] == "SCHW"


def test_parse_orphans_ignores_unrelated_lines():
    from backfill_orphan_trailing_stops_2026_06_04 import _parse_orphans
    detail = "header text\n\n  backfill_cover: AAPL qty=5 cover_order=x @ $100 (stop)\nfooter"
    out = _parse_orphans(detail)
    assert out == [], "Only backfill_sell lines should match"


# ---------------------------------------------------------------------------
# 2. _resolve_orphan finds rows + computes pnl + identifies siblings
# ---------------------------------------------------------------------------

def test_resolve_orphan_finds_pending_entry_and_stale_siblings(tmp_path):
    from backfill_orphan_trailing_stops_2026_06_04 import _resolve_orphan
    db, ids = _journal_with_orphan(tmp_path, num_stale_siblings=2)
    orphan = {
        "symbol": "CRM", "qty": 127.0, "sell_order": "df787e44",
        "sell_price": 172.84, "order_type": "trailing_stop",
    }
    with closing(sqlite3.connect(db)) as conn:
        plan = _resolve_orphan(conn, orphan)
    assert "skip_reason" not in plan
    assert plan["pending_id"] == ids["pending_id"]
    assert plan["pending_oid"] == "newest-placement-oid"
    assert plan["entry_id"] == 25
    assert plan["fill_price"] == 172.84
    # Realized pnl = (172.84 - 175.00) * 127 = -274.32
    assert plan["realized_pnl"] == -274.32
    assert len(plan["stale_siblings"]) == 2
    assert {s["order_id"] for s in plan["stale_siblings"]} == {
        "stale-sibling-0", "stale-sibling-1",
    }


def test_resolve_orphan_skips_when_no_pending_row(tmp_path):
    from backfill_orphan_trailing_stops_2026_06_04 import _resolve_orphan
    # Empty journal — no pending_protective row for V.
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, status TEXT, occ_symbol TEXT
        )
    """)
    conn.commit()
    conn.close()
    with closing(sqlite3.connect(str(db))) as c:
        plan = _resolve_orphan(c, {
            "symbol": "V", "qty": 76.0, "sell_order": "x",
            "sell_price": 316.50, "order_type": "trailing_stop",
        })
    assert "skip_reason" in plan
    assert "no pending_protective" in plan["skip_reason"]


def test_resolve_orphan_skips_when_no_open_entry(tmp_path):
    from backfill_orphan_trailing_stops_2026_06_04 import _resolve_orphan
    # Pending row exists but the BUY entry is already closed.
    db = tmp_path / "no_entry.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, status TEXT, occ_symbol TEXT
        )
    """)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, order_id, status) "
        "VALUES ('SCHW', 'sell', 174.0, 'x', 'pending_protective')",
    )
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, order_id, status) "
        "VALUES ('SCHW', 'buy', 174.0, 'y', 'closed')",
    )
    conn.commit()
    conn.close()
    with closing(sqlite3.connect(str(db))) as c:
        plan = _resolve_orphan(c, {
            "symbol": "SCHW", "qty": 174.0, "sell_order": "x",
            "sell_price": 86.81, "order_type": "trailing_stop",
        })
    assert "skip_reason" in plan
    assert "no open BUY entry" in plan["skip_reason"]


# ---------------------------------------------------------------------------
# 3. _apply_plan writes correctly + idempotent
# ---------------------------------------------------------------------------

def test_apply_plan_closes_pending_entry_and_cancels_siblings(tmp_path):
    from backfill_orphan_trailing_stops_2026_06_04 import (
        _resolve_orphan, _apply_plan,
    )
    db, ids = _journal_with_orphan(tmp_path, num_stale_siblings=2)
    orphan = {
        "symbol": "CRM", "qty": 127.0, "sell_order": "df787e44",
        "sell_price": 172.84, "order_type": "trailing_stop",
    }
    with closing(sqlite3.connect(db)) as conn:
        plan = _resolve_orphan(conn, orphan)
        _apply_plan(conn, plan, terminal_order_id="df787e44")
        conn.commit()
    with closing(sqlite3.connect(db)) as conn:
        pending = conn.execute(
            "SELECT status, price, fill_price FROM trades WHERE id=?",
            (ids["pending_id"],),
        ).fetchone()
        entry = conn.execute(
            "SELECT status, pnl FROM trades WHERE id=?",
            (ids["entry_id"],),
        ).fetchone()
        siblings = conn.execute(
            "SELECT status FROM trades WHERE id IN (?, ?)",
            tuple(ids["sibling_ids"]),
        ).fetchall()
    assert pending == ("closed", 172.84, 172.84)
    assert entry == ("closed", -274.32)
    for s in siblings:
        assert s[0] == "canceled"


def test_apply_plan_idempotent(tmp_path):
    """Running twice should skip the second pass — entry already closed."""
    from backfill_orphan_trailing_stops_2026_06_04 import (
        _resolve_orphan, _apply_plan,
    )
    db, _ = _journal_with_orphan(tmp_path, num_stale_siblings=0)
    orphan = {
        "symbol": "CRM", "qty": 127.0, "sell_order": "df787e44",
        "sell_price": 172.84, "order_type": "trailing_stop",
    }
    with closing(sqlite3.connect(db)) as conn:
        plan1 = _resolve_orphan(conn, orphan)
        _apply_plan(conn, plan1, terminal_order_id="df787e44")
        conn.commit()
    # Second pass — pending_protective row is now closed, so no
    # matching pending row exists.
    with closing(sqlite3.connect(db)) as conn:
        plan2 = _resolve_orphan(conn, orphan)
    assert "skip_reason" in plan2, (
        "Second run must skip — the pending_protective row was closed "
        "by the first run, so there's nothing left to backfill."
    )
