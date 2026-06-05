"""RC3 root-cause fix: protective placement must be idempotent.

`ensure_protective_stops` runs every cycle. When the broker's
active-protective query (broker_coverage) returns empty for a
symbol because of an API blip or cache lag, the existing skip
check fell through and placed a duplicate protective stop — that's
how BMNR pid=29 ended up with two `pending_protective` SELL rows
from different days for the same entry.

Contract pinned by these tests:

  1. If the journal records an active pending_protective row for
     this symbol+close_side, and the broker confirms the order is
     still alive, the second placement attempt is a no-op.
  2. If the journal records a pending_protective but the broker
     check fails (network blip etc.), default to SKIP — never
     place a possible duplicate.
  3. If the journal's pending_protective is now broker-side
     expired/canceled/rejected, mark the old row terminal then
     allow a fresh placement.
  4. If the journal's pending_protective is now broker-side
     filled, RC1's transition will handle it; skip placement
     because the position is no longer at the broker.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _make_db(tmp_path):
    db = tmp_path / "p.db"
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
            strategy TEXT,
            reason TEXT,
            status TEXT,
            pnl REAL,
            fill_price REAL,
            occ_symbol TEXT,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    conn.commit()
    return conn, str(db)


def _insert(conn, **kwargs):
    cols = ",".join(kwargs.keys())
    qs = ",".join("?" for _ in kwargs)
    cur = conn.execute(
        f"INSERT INTO trades ({cols}) VALUES ({qs})",
        list(kwargs.values()),
    )
    conn.commit()
    return cur.lastrowid


def _mk_ctx():
    return SimpleNamespace(
        stop_loss_pct=0.03,
        short_stop_loss_pct=None,
        use_trailing_stops=True,
    )


def _mk_position(symbol="NVDA", qty=100, avg=500.0):
    return {
        "symbol": symbol, "qty": qty,
        "avg_entry_price": avg,
        "current_price": avg * 1.02,
        "is_option": False, "occ_symbol": None,
    }


def _api_with_orders(by_id: dict, positions=None):
    """Build a MagicMock api where get_order returns from by_id and
    list_positions / list_orders return empty (force broker_coverage
    to find nothing → exercises the journal-side dedup path)."""
    api = MagicMock()
    api.get_order = lambda oid: by_id.get(oid)
    api.list_positions = MagicMock(return_value=positions or [])
    api.list_orders = MagicMock(return_value=[])
    api.submit_order = MagicMock(side_effect=AssertionError(
        "submit_order should NOT be called when journal has active "
        "pending_protective + broker confirms alive"
    ))
    return api


def test_no_duplicate_when_journal_pending_protective_is_alive(
        tmp_path, monkeypatch,
):
    """Journal has a pending_protective row; broker confirms the
    order is still active. Skip placement."""
    conn, db_path = _make_db(tmp_path)
    _insert(
        conn,
        timestamp="2026-06-04T14:00:00",
        symbol="NVDA",
        side="buy",
        qty=100,
        price=500.0,
        fill_price=500.0,
        order_id="entry-1",
        signal_type="BUY",
        status="open",
    )
    _insert(
        conn,
        timestamp="2026-06-04T14:00:30",
        symbol="NVDA",
        side="sell",
        qty=100,
        order_id="prot-1",
        signal_type="PROTECTIVE_TRAILING",
        status="pending_protective",
    )
    conn.close()

    api = _api_with_orders({
        "prot-1": SimpleNamespace(id="prot-1", status="new"),
    })

    # active_protective_coverage queries the broker — patched to
    # return empty so we force the fall-through to RC3's journal
    # dedup path. This simulates the broker_coverage blip pattern.
    monkeypatch.setattr(
        "bracket_orders.active_protective_coverage",
        lambda _api: {},
    )

    from bracket_orders import ensure_protective_stops
    ensure_protective_stops(
        api=api,
        positions=[_mk_position()],
        ctx=_mk_ctx(),
        db_path=db_path,
    )
    api.submit_order.assert_not_called()


def test_skip_when_broker_check_fails_to_avoid_duplicate(
        tmp_path, monkeypatch,
):
    """If we can't reach the broker to verify, default to SKIP —
    never place a potential duplicate. The position is already
    journal-protected; next cycle will reattempt."""
    conn, db_path = _make_db(tmp_path)
    _insert(
        conn,
        timestamp="2026-06-04T14:00:00",
        symbol="NVDA",
        side="buy",
        qty=100,
        price=500.0,
        fill_price=500.0,
        order_id="entry-2",
        signal_type="BUY",
        status="open",
    )
    _insert(
        conn,
        timestamp="2026-06-04T14:00:30",
        symbol="NVDA",
        side="sell",
        qty=100,
        order_id="prot-2",
        signal_type="PROTECTIVE_TRAILING",
        status="pending_protective",
    )
    conn.close()

    def _fail(oid):
        raise RuntimeError("broker timeout")
    api = MagicMock()
    api.get_order = _fail
    api.submit_order = MagicMock(side_effect=AssertionError(
        "Must NOT submit when broker can't verify the existing "
        "pending_protective"
    ))
    monkeypatch.setattr(
        "bracket_orders.active_protective_coverage",
        lambda _api: {},
    )

    from bracket_orders import ensure_protective_stops
    ensure_protective_stops(
        api=api,
        positions=[_mk_position()],
        ctx=_mk_ctx(),
        db_path=db_path,
    )
    api.submit_order.assert_not_called()


def test_terminal_pending_protective_is_marked_then_replaced(
        tmp_path, monkeypatch,
):
    """If the journaled pending_protective's broker order is
    terminal-but-unfilled (expired, canceled, rejected), the old
    row is marked terminal AND a new placement is allowed."""
    conn, db_path = _make_db(tmp_path)
    _insert(
        conn,
        timestamp="2026-06-04T14:00:00",
        symbol="NVDA",
        side="buy",
        qty=100,
        price=500.0,
        fill_price=500.0,
        order_id="entry-3",
        signal_type="BUY",
        status="open",
    )
    stale_id = _insert(
        conn,
        timestamp="2026-06-04T14:00:30",
        symbol="NVDA",
        side="sell",
        qty=100,
        order_id="prot-stale",
        signal_type="PROTECTIVE_TRAILING",
        status="pending_protective",
    )
    conn.close()

    api = MagicMock()
    api.get_order = lambda oid: SimpleNamespace(
        id=oid, status="canceled",
    )
    # Submit replies for the placement that should occur after the
    # stale row is marked terminal.
    api.submit_order = MagicMock(return_value=SimpleNamespace(
        id="prot-new", status="new",
    ))
    monkeypatch.setattr(
        "bracket_orders.active_protective_coverage",
        lambda _api: {},
    )

    from bracket_orders import ensure_protective_stops
    ensure_protective_stops(
        api=api,
        positions=[_mk_position()],
        ctx=_mk_ctx(),
        db_path=db_path,
    )

    with closing(sqlite3.connect(db_path)) as conn:
        stale_row_status = conn.execute(
            "SELECT status FROM trades WHERE id=?", (stale_id,),
        ).fetchone()[0]
    assert stale_row_status == "canceled", (
        f"Stale pending_protective must be marked terminal once the "
        f"broker confirms it's no longer alive — got {stale_row_status!r}"
    )


def test_filled_pending_protective_skips_placement(
        tmp_path, monkeypatch,
):
    """If the broker says the previous protective FILLED, the
    position is no longer at the broker. RC1 will close the
    pending_protective row next cycle. Don't place a new protective
    — there's nothing to protect anymore."""
    conn, db_path = _make_db(tmp_path)
    _insert(
        conn,
        timestamp="2026-06-04T14:00:00",
        symbol="NVDA",
        side="buy",
        qty=100,
        price=500.0,
        fill_price=500.0,
        order_id="entry-4",
        signal_type="BUY",
        status="open",
    )
    _insert(
        conn,
        timestamp="2026-06-04T14:00:30",
        symbol="NVDA",
        side="sell",
        qty=100,
        order_id="prot-filled",
        signal_type="PROTECTIVE_TRAILING",
        status="pending_protective",
    )
    conn.close()

    api = MagicMock()
    api.get_order = lambda oid: SimpleNamespace(
        id=oid, status="filled", filled_qty="100",
        filled_avg_price="498.50",
    )
    api.submit_order = MagicMock(side_effect=AssertionError(
        "Must NOT submit a new protective when the previous one "
        "is broker-filled (position is gone; RC1 will close the row)"
    ))
    monkeypatch.setattr(
        "bracket_orders.active_protective_coverage",
        lambda _api: {},
    )

    from bracket_orders import ensure_protective_stops
    ensure_protective_stops(
        api=api,
        positions=[_mk_position()],
        ctx=_mk_ctx(),
        db_path=db_path,
    )
    api.submit_order.assert_not_called()
