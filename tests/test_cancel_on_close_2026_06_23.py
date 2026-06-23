"""2026-06-23 — cancel-on-close leak: a CLOSED entry's protective
orders must not keep resting at the broker.

THE LEAK (prod, profile 158 NFLX, account PA3A2LULLCAL): the broker
held NO NFLX stock position, yet two live GTC sells were resting —
`limit` 294 (4da44dff) and `trailing_stop` 294 (61d17292) — both owned
by p158's journal. The journal's NFLX BUY entry (trades.id=10) was
`status='closed'`, but its protective_tp_order_id /
protective_trailing_order_id still pointed at those two orders, and the
matching pending_protective rows (77, 78) were still
`status='pending_protective'`. A resting sell can then fire on a flat
position -> an unintended SHORT — the exact "sell the wrong thing" risk
the oversell door cannot stop once the order is already at the broker.

Root cause: `cancel_for_symbol` and the trader exit path only scan
`status='open'`, so a protective tied to a now-`closed` entry is never
cancelled; `ensure_protective_stops` only iterates OPEN positions so it
never visits the flat symbol; `verify_protective_order_sync` detects
the staleness but is pure read.

`bracket_orders.cancel_orphaned_protective_orders` is the active fix
(wired into the per-cycle reconcile). This file pins:
  1. closed-entry protective pair -> both cancelled, entry pointers
     cleared, pending_protective rows marked terminal;
  2. a protective that ALREADY FILLED is left intact (pointer + row);
  3. an OPEN position's protective is never touched;
  4. an option (occ_symbol) leg on the same symbol is never touched;
  5. multi_scheduler wires the reconciler into the reconcile path.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Fixtures — a real journal DB + a fake api with per-order_id statuses
# ---------------------------------------------------------------------------

def _init_db(tmp_path):
    import journal
    db = str(tmp_path / "journal.db")
    journal.init_db(db)
    return db


def _insert(db, **cols):
    keys = ", ".join(cols.keys())
    qs = ", ".join("?" for _ in cols)
    with closing(sqlite3.connect(db)) as conn:
        cur = conn.execute(
            f"INSERT INTO trades ({keys}) VALUES ({qs})",
            tuple(cols.values()),
        )
        conn.commit()
        return cur.lastrowid


def _row(db, rid):
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM trades WHERE id = ?", (rid,)).fetchone()


def _fake_api(statuses):
    """api.get_order(oid) reports statuses[oid] (default 'new'=live);
    api.cancel_order records the ids it was asked to cancel."""
    api = MagicMock()

    def _get_order(oid, *a, **k):
        from types import SimpleNamespace
        return SimpleNamespace(id=oid, status=statuses.get(oid, "new"))

    api.get_order.side_effect = _get_order
    api.cancel_order.return_value = None
    return api


def _cancelled(api):
    return {c.args[0] for c in api.cancel_order.call_args_list}


# ---------------------------------------------------------------------------
# 1. The exact prod shape — closed entry + live protective pair
# ---------------------------------------------------------------------------

def test_closed_entry_live_pair_is_cancelled_and_rows_terminated(tmp_path):
    db = _init_db(tmp_path)
    tp_oid = "4da44dff-4e2c-48d1-a9b4-767a2b887e0c"
    trail_oid = "61d17292-92bf-4bbd-8c10-b7cf9a772211"

    # Closed 294-share NFLX BUY with both protective pointers live.
    entry_id = _insert(
        db, symbol="NFLX", side="buy", qty=294, price=20.0,
        order_id="44d43393", signal_type="BUY", status="closed",
        protective_tp_order_id=tp_oid,
        protective_trailing_order_id=trail_oid,
    )
    # The two pending_protective rows (journal side of the live orders).
    pend_trail = _insert(
        db, symbol="NFLX", side="sell", qty=294, order_id=trail_oid,
        signal_type="PROTECTIVE_TRAILING", status="pending_protective")
    pend_tp = _insert(
        db, symbol="NFLX", side="sell", qty=294, order_id=tp_oid,
        signal_type="PROTECTIVE_TAKE_PROFIT", status="pending_protective")
    # Open OPTION legs on the SAME symbol — must NOT be touched.
    opt_buy = _insert(
        db, symbol="NFLX", side="buy", qty=1, order_id="4def7d45",
        signal_type="MULTILEG", status="open",
        occ_symbol="NFLX260731C00077000")
    opt_sell = _insert(
        db, symbol="NFLX", side="sell", qty=1, order_id="4def7d45",
        signal_type="MULTILEG", status="open",
        occ_symbol="NFLX260731C00081000")

    api = _fake_api({tp_oid: "new", trail_oid: "new"})

    from bracket_orders import cancel_orphaned_protective_orders
    out = cancel_orphaned_protective_orders(api, db)

    # Both resting orders cancelled at the broker.
    assert _cancelled(api) == {tp_oid, trail_oid}, (
        "both resting protective sells on the closed/flat NFLX position "
        "must be cancelled")
    assert out["canceled"] == 2

    # Entry pointers cleared so they can never fire again.
    entry = _row(db, entry_id)
    assert entry["protective_tp_order_id"] is None
    assert entry["protective_trailing_order_id"] is None

    # pending_protective rows marked terminal (no longer 'pending').
    assert _row(db, pend_trail)["status"] == "canceled"
    assert _row(db, pend_tp)["status"] == "canceled"

    # Option legs untouched (different lifecycle).
    assert _row(db, opt_buy)["status"] == "open"
    assert _row(db, opt_sell)["status"] == "open"


# ---------------------------------------------------------------------------
# 2. A protective that ALREADY FILLED is left intact (no double-handling)
# ---------------------------------------------------------------------------

def test_filled_protective_on_closed_entry_is_kept(tmp_path):
    db = _init_db(tmp_path)
    filled_oid = "fill-0001"

    entry_id = _insert(
        db, symbol="PLUG", side="buy", qty=100, price=5.0,
        order_id="entry-PLUG", signal_type="BUY", status="closed",
        protective_stop_order_id=filled_oid)
    pend_id = _insert(
        db, symbol="PLUG", side="sell", qty=100, order_id=filled_oid,
        signal_type="PROTECTIVE_STOP", status="pending_protective")

    api = _fake_api({filled_oid: "filled"})

    from bracket_orders import cancel_orphaned_protective_orders
    out = cancel_orphaned_protective_orders(api, db)

    # A filled protective fired and closed the slice — never cancel it,
    # never terminate its row; the fill state machine owns it.
    assert _cancelled(api) == set()
    assert out["canceled"] == 0
    assert out["filled_kept"] >= 1
    assert _row(db, entry_id)["protective_stop_order_id"] == filled_oid
    assert _row(db, pend_id)["status"] == "pending_protective"


# ---------------------------------------------------------------------------
# 3. An OPEN position's protective is NEVER touched (live coverage)
# ---------------------------------------------------------------------------

def test_open_position_protective_is_untouched(tmp_path):
    db = _init_db(tmp_path)
    live_oid = "live-stop-1"

    entry_id = _insert(
        db, symbol="AMD", side="buy", qty=50, price=100.0,
        order_id="entry-AMD", signal_type="BUY", status="open",
        protective_trailing_order_id=live_oid)
    pend_id = _insert(
        db, symbol="AMD", side="sell", qty=50, order_id=live_oid,
        signal_type="PROTECTIVE_TRAILING", status="pending_protective")

    api = _fake_api({live_oid: "new"})

    from bracket_orders import cancel_orphaned_protective_orders
    cancel_orphaned_protective_orders(api, db)

    # Position is OPEN — its protective is legitimate coverage.
    assert _cancelled(api) == set()
    assert _row(db, entry_id)["protective_trailing_order_id"] == live_oid
    assert _row(db, pend_id)["status"] == "pending_protective"


# ---------------------------------------------------------------------------
# 4. Pass-2 orphan: a pending_protective row on a FLAT symbol with NO
#    closed-entry pointer is still cancelled + terminated.
# ---------------------------------------------------------------------------

def test_orphan_pending_on_flat_symbol_is_cancelled(tmp_path):
    db = _init_db(tmp_path)
    orphan_oid = "orphan-1"

    # No entry row at all for SOFI — just an orphaned pending protective
    # (e.g. the entry was hard-deleted, or its pointer was already
    # cleared). The position is flat, so the resting order must go.
    pend_id = _insert(
        db, symbol="SOFI", side="sell", qty=200, order_id=orphan_oid,
        signal_type="PROTECTIVE_STOP", status="pending_protective")

    api = _fake_api({orphan_oid: "new"})

    from bracket_orders import cancel_orphaned_protective_orders
    cancel_orphaned_protective_orders(api, db)

    assert _cancelled(api) == {orphan_oid}
    assert _row(db, pend_id)["status"] == "canceled"


def test_orphan_pending_already_terminal_at_broker_is_synced(tmp_path):
    """If the broker already shows the order terminal (expired/canceled)
    we don't re-cancel, but we DO sync the stale pending row."""
    db = _init_db(tmp_path)
    expired_oid = "expired-1"

    pend_id = _insert(
        db, symbol="ETHA", side="sell", qty=10, order_id=expired_oid,
        signal_type="PROTECTIVE_TRAILING", status="pending_protective")

    api = _fake_api({expired_oid: "expired"})

    from bracket_orders import cancel_orphaned_protective_orders
    cancel_orphaned_protective_orders(api, db)

    # Nothing to cancel, but the row is synced to the broker's terminal.
    assert _cancelled(api) == set()
    assert _row(db, pend_id)["status"] == "expired"


# ---------------------------------------------------------------------------
# 5. The reconciler is WIRED into the per-cycle reconcile (class fix —
#    detection alone is what let the leak persist).
# ---------------------------------------------------------------------------

def test_multi_scheduler_wires_cancel_on_close():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "multi_scheduler.py")) as fh:
        src = fh.read()
    assert "cancel_orphaned_protective_orders" in src, (
        "multi_scheduler must invoke cancel_orphaned_protective_orders "
        "every reconcile cycle — verify_protective_order_sync only "
        "detects the staleness; detection alone is what let the "
        "profile-158 NFLX protective pair keep resting on a flat "
        "position.")
    # Must sit in the reconcile task alongside the read-only detector.
    assert "verify_protective_order_sync" in src
    assert src.index("verify_protective_order_sync") < src.index(
        "cancel_orphaned_protective_orders"), (
        "active cancel-on-close should follow the read-only sync check "
        "in the reconcile path")
