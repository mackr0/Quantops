"""Proactive chain-walk sweep — keep pending_protective rows' order_id
in sync with the broker's live replace chain, BEFORE a fill arrives
(2026-06-04).

Closes gap #3 from the post-reset orphan-prevention list. The reconciler
walks Alpaca's replace chain forward at fill time, but if a chain has
grown long while the system was offline / slow, the walk can hit
max_depth and the fill falls through to fuzzy fallback (orphan, halt).
This sweep runs every cycle and keeps the journaled order_id within
1-2 hops of the live id, so the fill-time walk is always near-trivial.

Tests pin:
  1. Replaced-status pending row: walk forward, UPDATE order_id to
     the live id, count as 'advanced'.
  2. Canceled / expired / rejected broker order: mark the pending
     row 'canceled' so it stops counting as pending.
  3. Alive (new/accepted/held) broker order: no-op.
  4. Filled broker order: leave for the reconciler (don't pre-empt
     the closed-with-fill-data write).
  5. Entry-row pointer (protective_*_order_id) is healed when the
     advance happens, so journal == Alpaca after the sweep.
  6. API errors don't crash; counted as 'errored'.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def journal_db(tmp_path):
    """Trades schema including the protective_*_order_id pointer columns
    so the entry-row heal can be exercised."""
    db = tmp_path / "sync.db"
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
            status TEXT,
            reason TEXT,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    conn.commit()
    conn.close()
    return str(db)


def _broker_order(oid, status, replaced_by=None, side="sell",
                   filled_qty=0, qty=100):
    o = MagicMock()
    o.id = oid
    o.side = side
    o.status = status
    o.qty = qty
    o.filled_qty = filled_qty
    o.replaced_by = replaced_by
    return o


def _api_with_chain(chain):
    api = MagicMock()
    api.get_order.side_effect = lambda oid: chain[oid]
    return api


def _insert_pending(db, **cols):
    keys = ", ".join(cols.keys())
    qs = ", ".join("?" * len(cols))
    with closing(sqlite3.connect(db)) as conn:
        cur = conn.execute(
            f"INSERT INTO trades ({keys}, status) VALUES ({qs}, 'pending_protective')",
            tuple(cols.values()),
        )
        conn.commit()
        return cur.lastrowid


def _insert_entry(db, **cols):
    keys = ", ".join(cols.keys())
    qs = ", ".join("?" * len(cols))
    with closing(sqlite3.connect(db)) as conn:
        cur = conn.execute(
            f"INSERT INTO trades ({keys}) VALUES ({qs})",
            tuple(cols.values()),
        )
        conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------------------
# 1. Replaced chain → advance order_id forward
# ---------------------------------------------------------------------------

def test_replaced_chain_advances_pending_row_order_id(journal_db):
    """The journaled placement order has been replaced twice at the
    broker. Sweep walks to the live terminal and updates the row."""
    from bracket_orders import sync_pending_protective_order_ids
    pending_id = _insert_pending(
        journal_db, symbol="CRM", side="sell", qty=127,
        order_id="placement", signal_type="PROTECTIVE_TRAILING",
    )
    chain = {
        "placement": _broker_order("placement", "replaced", replaced_by="mid"),
        "mid": _broker_order("mid", "replaced", replaced_by="live"),
        "live": _broker_order("live", "new"),
    }
    api = _api_with_chain(chain)
    stats = sync_pending_protective_order_ids(api, journal_db)
    assert stats == {"checked": 1, "advanced": 1,
                     "marked_canceled": 0, "errored": 0}
    with closing(sqlite3.connect(journal_db)) as conn:
        row = conn.execute(
            "SELECT order_id, status, reason FROM trades WHERE id=?",
            (pending_id,),
        ).fetchone()
    assert row[0] == "live", "order_id must be advanced to chain terminal"
    assert row[1] == "pending_protective", "status stays pending"
    assert "sync 2026-06-04" in row[2]


# ---------------------------------------------------------------------------
# 2. Entry-row pointer healed on advance (journal == Alpaca)
# ---------------------------------------------------------------------------

def test_entry_pointer_healed_when_pending_row_advances(journal_db):
    """The entry BUY row's protective_trailing_order_id pointed at the
    old placement id. After sync, both the pending row's order_id AND
    the entry row's pointer should reference the live id."""
    from bracket_orders import sync_pending_protective_order_ids
    entry_id = _insert_entry(
        journal_db, symbol="CRM", side="buy", qty=127, price=175.0,
        order_id="entry-buy", status="open",
        protective_trailing_order_id="placement",
    )
    _insert_pending(
        journal_db, symbol="CRM", side="sell", qty=127,
        order_id="placement", signal_type="PROTECTIVE_TRAILING",
    )
    chain = {
        "placement": _broker_order(
            "placement", "replaced", replaced_by="live"),
        "live": _broker_order("live", "new"),
    }
    api = _api_with_chain(chain)
    sync_pending_protective_order_ids(api, journal_db)
    with closing(sqlite3.connect(journal_db)) as conn:
        ptr = conn.execute(
            "SELECT protective_trailing_order_id FROM trades WHERE id=?",
            (entry_id,),
        ).fetchone()[0]
    assert ptr == "live", (
        "Entry-row pointer must also be healed to the live id so the "
        "fill-time chain walk is near-trivial."
    )


# ---------------------------------------------------------------------------
# 3. Canceled broker order → mark pending row canceled
# ---------------------------------------------------------------------------

def test_canceled_broker_order_marks_pending_canceled(journal_db):
    """Broker order was canceled (not replaced). The pending row no
    longer references a live order — mark it canceled so the next
    ensure_protective_stops sweep can place a fresh protective."""
    from bracket_orders import sync_pending_protective_order_ids
    pending_id = _insert_pending(
        journal_db, symbol="V", side="sell", qty=76,
        order_id="dead", signal_type="PROTECTIVE_STOP",
    )
    api = _api_with_chain({"dead": _broker_order("dead", "canceled")})
    stats = sync_pending_protective_order_ids(api, journal_db)
    assert stats["marked_canceled"] == 1
    with closing(sqlite3.connect(journal_db)) as conn:
        row = conn.execute(
            "SELECT status, reason FROM trades WHERE id=?",
            (pending_id,),
        ).fetchone()
    assert row[0] == "canceled"
    assert "broker order dead" in row[1] and "canceled" in row[1]


def test_expired_and_rejected_also_marked(journal_db):
    """Same treatment for `expired` and `rejected` — none of those
    are live orders the journal can rely on."""
    from bracket_orders import sync_pending_protective_order_ids
    p1 = _insert_pending(journal_db, symbol="X", side="sell", qty=1,
                          order_id="exp", signal_type="PROTECTIVE_STOP")
    p2 = _insert_pending(journal_db, symbol="Y", side="sell", qty=1,
                          order_id="rej", signal_type="PROTECTIVE_STOP")
    api = _api_with_chain({
        "exp": _broker_order("exp", "expired"),
        "rej": _broker_order("rej", "rejected"),
    })
    stats = sync_pending_protective_order_ids(api, journal_db)
    assert stats["marked_canceled"] == 2
    with closing(sqlite3.connect(journal_db)) as conn:
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM trades WHERE id IN (?, ?)",
            (p1, p2),
        ).fetchall()]
    assert all(s == "canceled" for s in statuses)


# ---------------------------------------------------------------------------
# 4. Alive orders: no-op
# ---------------------------------------------------------------------------

def test_alive_order_is_noop(journal_db):
    """If the broker order is alive (new/accepted/held/pending_new),
    the journaled id is fresh — leave everything alone."""
    from bracket_orders import sync_pending_protective_order_ids
    pid = _insert_pending(
        journal_db, symbol="AMZN", side="sell", qty=29,
        order_id="alive", signal_type="PROTECTIVE_TRAILING",
    )
    api = _api_with_chain({"alive": _broker_order("alive", "accepted")})
    stats = sync_pending_protective_order_ids(api, journal_db)
    assert stats == {"checked": 1, "advanced": 0,
                     "marked_canceled": 0, "errored": 0}
    with closing(sqlite3.connect(journal_db)) as conn:
        row = conn.execute(
            "SELECT order_id, status FROM trades WHERE id=?", (pid,),
        ).fetchone()
    assert row[0] == "alive"
    assert row[1] == "pending_protective"


# ---------------------------------------------------------------------------
# 5. Filled broker order: leave for the reconciler
# ---------------------------------------------------------------------------

def test_filled_broker_order_left_for_reconciler(journal_db):
    """When the broker order is filled, the reconciler's pending-row
    UPDATE path (with full fill_price + filled_at) is the right code
    path. Sync must not pre-empt that with a half-write."""
    from bracket_orders import sync_pending_protective_order_ids
    pid = _insert_pending(
        journal_db, symbol="NFLX", side="sell", qty=153,
        order_id="filled-now", signal_type="PROTECTIVE_TRAILING",
    )
    api = _api_with_chain({
        "filled-now": _broker_order("filled-now", "filled",
                                     filled_qty=153),
    })
    stats = sync_pending_protective_order_ids(api, journal_db)
    assert stats["advanced"] == 0
    assert stats["marked_canceled"] == 0
    with closing(sqlite3.connect(journal_db)) as conn:
        row = conn.execute(
            "SELECT status FROM trades WHERE id=?", (pid,),
        ).fetchone()
    assert row[0] == "pending_protective", (
        "Sync must not touch a filled pending row — the reconciler "
        "owns the closed-with-fill-data transition."
    )


# ---------------------------------------------------------------------------
# 6. API errors don't crash; counted
# ---------------------------------------------------------------------------

def test_api_error_does_not_crash(journal_db):
    """If get_order raises, count as errored and continue with the
    rest of the rows."""
    from bracket_orders import sync_pending_protective_order_ids
    _insert_pending(journal_db, symbol="A", side="sell", qty=1,
                     order_id="boom", signal_type="PROTECTIVE_STOP")
    _insert_pending(journal_db, symbol="B", side="sell", qty=1,
                     order_id="alive", signal_type="PROTECTIVE_STOP")
    api = MagicMock()
    def side(oid):
        if oid == "boom":
            raise Exception("api error")
        return _broker_order(oid, "new")
    api.get_order.side_effect = side
    stats = sync_pending_protective_order_ids(api, journal_db)
    assert stats == {"checked": 2, "advanced": 0,
                     "marked_canceled": 0, "errored": 1}


# ---------------------------------------------------------------------------
# 7. Walk-replace-chain max_depth bumped to 50 (B telemetry test)
# ---------------------------------------------------------------------------

def test_max_depth_constant_is_fifty():
    """Documenting the bumped max_depth value as a structural pin —
    if someone reverts it to 10 the test fails. With the proactive
    sync running every cycle, chain depth at fill time should be near
    0; 50 is a generous safety margin."""
    from reconcile_journal_to_broker import _REPLACE_CHAIN_MAX_DEPTH
    assert _REPLACE_CHAIN_MAX_DEPTH == 50


def test_max_depth_hit_logs_critical(caplog):
    """When max_depth IS hit (pathological chain), the log level is
    CRITICAL — distinguishes 'chain too deep, fix the sweep' from
    'no protective order at all, normal fall-through'."""
    import logging
    from reconcile_journal_to_broker import (
        walk_replace_chain_forward, _REPLACE_CHAIN_MAX_DEPTH,
    )
    # Build a chain 1 longer than max_depth, all in 'replaced' state
    n = _REPLACE_CHAIN_MAX_DEPTH + 5
    chain = {
        f"oid-{i}": _broker_order(
            f"oid-{i}", "replaced", replaced_by=f"oid-{i+1}")
        for i in range(n)
    }
    chain[f"oid-{n}"] = _broker_order(f"oid-{n}", "replaced",
                                       replaced_by=None)
    api = _api_with_chain(chain)
    with caplog.at_level(logging.CRITICAL):
        order, depth = walk_replace_chain_forward(api, "oid-0")
    assert order is None
    assert depth == _REPLACE_CHAIN_MAX_DEPTH
    critical_msgs = [r.message for r in caplog.records
                      if r.levelname == "CRITICAL"]
    assert any("max_depth" in m for m in critical_msgs), (
        "Hitting max_depth must log CRITICAL — operator needs to know "
        "the root cause is 'chain too deep' not 'no journal row'."
    )
