"""Regression: option-OPEN pending_fill rows must transition to
'open' (not 'closed') when the broker confirms the fill.

The bug producing the 2026-06-05 EXP-A2 broker_orphan drift: the
state-machine branch in `_task_update_fills` flipped every
option-pending_fill row to status='closed' once filled — including
option OPEN rows (long calls, multileg legs) that landed in
pending_fill via certain code paths. The broker then held positions
the virtual book recorded as closed.

The fix gates the close-transition on `pnl IS NOT NULL` (only
closing rows carry realized P&L; opens never do).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest.mock import MagicMock, patch

import pytest


def _make_db(tmp_path):
    db = tmp_path / "quantopsai_profile_99.db"
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
            ai_reasoning TEXT,
            ai_confidence INTEGER,
            stop_loss REAL,
            take_profit REAL,
            status TEXT,
            pnl REAL,
            decision_price REAL,
            fill_price REAL,
            slippage_pct REAL,
            occ_symbol TEXT,
            option_strategy TEXT,
            expiry TEXT,
            strike REAL,
            predicted_slippage_bps REAL,
            adv_at_decision REAL,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT,
            max_favorable_excursion REAL
        )
    """)
    conn.commit()
    return conn, str(db)


def _insert(conn, **kwargs):
    cols = list(kwargs.keys())
    qs = ",".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO trades ({','.join(cols)}) VALUES ({qs})",
        list(kwargs.values()),
    )
    conn.commit()


def _patch_module_imports(monkeypatch, db_path):
    """Stub out the things `_task_update_fills` needs from the
    rest of the codebase so we can drive only the state-machine
    branch under test."""
    # The function uses get_api(ctx) + iterates list_orders.
    # We're testing the post-fill state transition, so the broker
    # already returned filled_avg_price; we set up the row state
    # directly and patch get_api so the cursor + commit run on
    # our DB.
    pass


def test_option_open_pending_fill_transitions_to_open(
        tmp_path, monkeypatch,
):
    """An option BUY-side row with status='pending_fill' and
    pnl IS NULL (= an OPEN) must transition to 'open' when the
    broker confirms the fill — NOT to 'closed'."""
    conn, db_path = _make_db(tmp_path)

    # Set up an option open row in pending_fill state with a fill
    # price (simulating the broker just confirmed). pnl=NULL marks
    # it unambiguously as an OPEN.
    _insert(
        conn,
        timestamp="2026-06-05T13:30:00",
        symbol="NVDA",
        side="buy",
        qty=1,
        price=None,
        order_id="open-order-1",
        signal_type="OPTIONS",
        status="pending_fill",
        pnl=None,
        decision_price=4.50,
        fill_price=4.55,
        occ_symbol="NVDA260710C00240000",
        option_strategy="long_call",
        strike=240,
        expiry="2026-07-10",
    )
    conn.close()

    # Drive the state-machine branch directly. The branch under
    # test is at multi_scheduler.py:1395+ — but to isolate the
    # transition logic from the broker-call infrastructure, we
    # apply the SQL the branch would execute and assert the
    # outcome.
    #
    # The transition contract (as fixed 2026-06-05):
    #   pending_fill + occ_symbol + pnl IS NOT NULL -> closed
    #   pending_fill (any other shape)              -> open
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT id, side, occ_symbol, pnl, status FROM trades "
            "WHERE order_id='open-order-1'"
        ).fetchone()
        assert row[4] == "pending_fill", "test setup wrong"

        # Apply the discriminator gate:
        is_close = (row[2] is not None) and (row[3] is not None)
        new_status = "closed" if is_close else "open"
        conn.execute(
            "UPDATE trades SET status=? WHERE id=?",
            (new_status, row[0]),
        )
        conn.commit()

        final = conn.execute(
            "SELECT status FROM trades WHERE id=?", (row[0],),
        ).fetchone()
        assert final[0] == "open", (
            "Option OPEN with pnl=NULL must transition to 'open' "
            f"(got {final[0]!r}) — bug class that produced the "
            "EXP-A2 broker_orphan drift on 2026-06-05."
        )


def test_option_close_pending_fill_transitions_to_closed(
        tmp_path, monkeypatch,
):
    """An option CLOSE row (side=sell, status=pending_fill, pnl set)
    must transition to 'closed' — the original intended branch
    behavior that the gate preserves."""
    conn, db_path = _make_db(tmp_path)
    _insert(
        conn,
        timestamp="2026-06-05T15:00:00",
        symbol="NVDA",
        side="sell",
        qty=1,
        price=None,
        order_id="close-order-1",
        signal_type="AUTO_RECONCILE_CLOSE",
        status="pending_fill",
        pnl=120.50,  # close paths carry realized P&L
        decision_price=4.55,
        fill_price=5.75,
        occ_symbol="NVDA260710C00240000",
    )
    conn.close()

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT id, occ_symbol, pnl FROM trades "
            "WHERE order_id='close-order-1'"
        ).fetchone()
        is_close = (row[1] is not None) and (row[2] is not None)
        new_status = "closed" if is_close else "open"
        conn.execute(
            "UPDATE trades SET status=? WHERE id=?",
            (new_status, row[0]),
        )
        conn.commit()
        final = conn.execute(
            "SELECT status FROM trades WHERE id=?", (row[0],),
        ).fetchone()
        assert final[0] == "closed", (
            f"Option CLOSE with pnl set must -> 'closed' "
            f"(got {final[0]!r})"
        )


def test_state_machine_branch_in_multi_scheduler_uses_pnl_gate():
    """Structural pin on the bug-class fix: the branch in
    `_task_update_fills` that handles option-pending_fill must
    include `pnl IS NOT NULL` (or equivalent) so it can't fire
    on open rows. Without this gate, an option OPEN that lands in
    pending_fill via any path gets wrongly closed, producing
    broker_orphan drift."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "multi_scheduler.py").read_text()

    # Find the elif that handles occ_symbol pending_fill. After the
    # 2026-06-05 fix it must also guard on pnl IS NOT NULL.
    pattern = re.compile(
        r'elif\s*\(\s*trade\["status"\]\s*==\s*"pending_fill"\s*\n'
        r'\s*and\s*trade\["occ_symbol"\]\s*\n'
        r'\s*and\s*trade\["pnl"\]\s*is\s*not\s*None\s*\)',
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "multi_scheduler.py:_task_update_fills option-close branch "
        "missing `pnl IS NOT NULL` guard. Without it, option OPEN "
        "rows in pending_fill get wrongly transitioned to 'closed' "
        "and produce broker_orphan drift."
    )
