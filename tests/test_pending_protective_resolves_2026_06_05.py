"""RC1 root-cause fix: pending_protective rows must transition to
'closed' when the broker fills the protective order, and the
matching entry row's BUY/SHORT lot must be FIFO-consumed.

Before this fix, `_write_pending_protective_row` wrote rows at
placement time but no state-machine branch in `_task_update_fills`
handled the `pending_protective` status. When the protective stop
fired at the broker, the row sat unchanged forever — the virtual
position book kept counting the entry as open, while the broker
had already sold the position. This is the single biggest drift
accumulator that produced the 2026-06-05 journal/broker mismatch.

Contract pinned:
  pending_protective + side IN ('sell','cover') + broker fill
    → row.status='closed', entry's BUY/SHORT row FIFO-consumed.
  pending_protective + broker order's chain replaced N times
    → walk forward to terminal, then resolve fill.
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
            adv_at_decision REAL
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


def _run_task_update_fills(ctx, monkeypatch):
    """Drive `_task_update_fills` against a controllable test API."""
    from multi_scheduler import _task_update_fills
    _task_update_fills(ctx)


def _make_ctx(db_path: str, api):
    ctx = MagicMock()
    ctx.db_path = db_path
    ctx.display_name = "TEST"
    ctx.segment = "test"
    # client.get_api(ctx) is what _task_update_fills uses
    return ctx


def test_pending_protective_transitions_to_closed_on_broker_fill(
        tmp_path, monkeypatch,
):
    """The core RC1 contract. BUY 100 + pending_protective SELL 100;
    the broker fills the protective; both rows must be 'closed'."""
    conn, db_path = _make_db(tmp_path)

    # Long entry: BUY NVDA 100 @ $500 (already filled — fill_price set)
    buy_id = _insert(
        conn,
        timestamp="2026-06-04T14:00:00",
        symbol="NVDA",
        side="buy",
        qty=100,
        price=500.0,
        fill_price=500.0,
        order_id="entry-order-1",
        signal_type="BUY",
        status="open",
        decision_price=500.0,
    )
    # Protective trailing stop placed; pending_protective row written
    prot_id = _insert(
        conn,
        timestamp="2026-06-04T14:00:30",
        symbol="NVDA",
        side="sell",
        qty=100,
        price=None,  # NULL — set when broker fills
        order_id="prot-order-1",
        signal_type="PROTECTIVE_TRAILING",
        status="pending_protective",
        reason="protective placement; awaiting fill",
    )
    conn.close()

    # Broker has filled the protective: returns status='filled' with
    # filled_avg_price set.
    fake_order = SimpleNamespace(
        id="prot-order-1",
        status="filled",
        filled_qty="100",
        filled_avg_price="510.50",
    )
    api = MagicMock()
    api.get_order = MagicMock(return_value=fake_order)
    monkeypatch.setattr("client.get_api", lambda ctx: api)

    ctx = _make_ctx(db_path, api)
    _run_task_update_fills(ctx, monkeypatch)

    with closing(sqlite3.connect(db_path)) as conn:
        rows = {
            r[0]: {"status": r[1], "fill_price": r[2], "price": r[3]}
            for r in conn.execute(
                "SELECT id, status, fill_price, price FROM trades "
                "WHERE id IN (?, ?)",
                (buy_id, prot_id),
            ).fetchall()
        }

    assert rows[prot_id]["status"] == "closed", (
        f"Protective SELL must transition to 'closed' after broker fill "
        f"(got {rows[prot_id]['status']!r}). This is the RC1 root-cause "
        f"fix — without it, drift accumulates indefinitely."
    )
    assert rows[prot_id]["fill_price"] == pytest.approx(510.50)
    assert rows[buy_id]["status"] == "closed", (
        f"Entry BUY must be FIFO-consumed by the matching protective "
        f"fill (got {rows[buy_id]['status']!r})"
    )


def test_pending_protective_partial_fill_keeps_entry_open(
        tmp_path, monkeypatch,
):
    """Defensive: protective fires for less qty than the entry holds
    (broker partial). The protective row closes; the entry stays open
    with reduced effective lot."""
    conn, db_path = _make_db(tmp_path)
    buy_id = _insert(
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
    prot_id = _insert(
        conn,
        timestamp="2026-06-04T14:00:30",
        symbol="NVDA",
        side="sell",
        qty=50,
        price=None,
        order_id="prot-2",
        signal_type="PROTECTIVE_TRAILING",
        status="pending_protective",
    )
    conn.close()

    fake_order = SimpleNamespace(
        id="prot-2", status="filled",
        filled_qty="50", filled_avg_price="495.00",
    )
    api = MagicMock()
    api.get_order = MagicMock(return_value=fake_order)
    monkeypatch.setattr("client.get_api", lambda ctx: api)

    _run_task_update_fills(_make_ctx(db_path, api), monkeypatch)

    with closing(sqlite3.connect(db_path)) as conn:
        prot_status = conn.execute(
            "SELECT status FROM trades WHERE id=?", (prot_id,),
        ).fetchone()[0]
        buy_status = conn.execute(
            "SELECT status FROM trades WHERE id=?", (buy_id,),
        ).fetchone()[0]
    assert prot_status == "closed"
    # Entry should still be open — FIFO consumed only 50 of the 100
    assert buy_status == "open", (
        f"Entry BUY must stay 'open' on partial protective fill "
        f"(got {buy_status!r})"
    )


def test_pending_protective_with_replaced_chain_walks_to_terminal(
        tmp_path, monkeypatch,
):
    """Trailing stops are replaced server-side as the trail bumps. The
    journaled order_id may be mid-chain by the time the protective
    actually fires. The state machine must walk forward via
    `replaced_by` to find the terminal filled order."""
    conn, db_path = _make_db(tmp_path)
    buy_id = _insert(
        conn,
        timestamp="2026-06-04T14:00:00",
        symbol="NVDA",
        side="buy",
        qty=100,
        price=500.0,
        fill_price=500.0,  # already filled — out of unfilled set
        order_id="entry-3",
        signal_type="BUY",
        status="open",
    )
    prot_id = _insert(
        conn,
        timestamp="2026-06-04T14:00:30",
        symbol="NVDA",
        side="sell",
        qty=100,
        price=None,
        order_id="prot-stale-1",
        signal_type="PROTECTIVE_TRAILING",
        status="pending_protective",
    )
    conn.close()

    # The journaled id is mid-chain: status='replaced' with
    # replaced_by pointing at the terminal id which has the actual fill.
    stale_order = SimpleNamespace(
        id="prot-stale-1",
        status="replaced",
        replaced_by="prot-terminal",
    )
    terminal_order = SimpleNamespace(
        id="prot-terminal",
        status="filled",
        filled_qty="100",
        filled_avg_price="512.25",
        replaced_by=None,
    )
    by_id = {
        "prot-stale-1": stale_order,
        "prot-terminal": terminal_order,
    }
    api = MagicMock()
    api.get_order = lambda oid: by_id.get(oid)
    monkeypatch.setattr("client.get_api", lambda ctx: api)

    _run_task_update_fills(_make_ctx(db_path, api), monkeypatch)

    with closing(sqlite3.connect(db_path)) as conn:
        prot_row = conn.execute(
            "SELECT status, fill_price FROM trades WHERE id=?",
            (prot_id,),
        ).fetchone()
        buy_status = conn.execute(
            "SELECT status FROM trades WHERE id=?", (buy_id,),
        ).fetchone()[0]
    assert prot_row[0] == "closed", (
        "Protective row must transition to 'closed' after walking "
        "the replace chain to the terminal filled order"
    )
    assert prot_row[1] == pytest.approx(512.25), (
        "fill_price must come from the TERMINAL order, not the "
        "stale mid-chain id"
    )
    assert buy_status == "closed", (
        "Entry BUY must be FIFO-consumed after the terminal fill is found"
    )


def test_state_machine_branch_includes_pending_protective():
    """Structural pin: the SELL/COVER branch in _task_update_fills
    must include 'pending_protective' in its status check.
    Without this, the fix is invisible to a future refactor."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "multi_scheduler.py").read_text()
    pattern = re.compile(
        r'if\s*\(\s*trade\["status"\]\s+in\s+\(\s*'
        r'"pending_fill"\s*,\s*"pending_protective"\s*\)',
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "multi_scheduler.py:_task_update_fills SELL/COVER branch is "
        "missing 'pending_protective' in its status check. Without "
        "this, broker-fired protective stops never close their "
        "journal row and drift accumulates indefinitely (RC1, "
        "2026-06-05)."
    )
