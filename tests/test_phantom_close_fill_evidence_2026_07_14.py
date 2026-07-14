"""Phantom-close fill-evidence guard (2026-07-14).

'auto_reconciled_phantom_close' means "the broker never filled this —
no money moved", and every accounting engine excludes such rows from
cash/positions/realized. The aggregate-drift auto-repair used to mark
rows with ONLY a 5-minute age guard and ZERO broker verification —
and branded p214's broker-FILLED CVX put buy ($122 debit) a phantom
nine minutes after its fill (option position-visibility lag), while
the contract was then genuinely SOLD the next day. One mislabeled row
became permanent +$122.50 equity-identity drift.

Pinned here: before marking any row no-money-moved, the row's OWN
order must be broker-verified UNFILLED. Fill evidence, a missing
order id, or an unverifiable lookup all REFUSE the mark (loudly) —
a real divergence stays visible to the integrity gate instead of
being silently absorbed as a false zero.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from reconcile_aggregate_drift import _close_journal_phantom

PROFILES = [{"id": 42}]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "quantopsai_profile_42.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, "
        "timestamp TEXT DEFAULT '2026-07-14T10:00:00', symbol TEXT, "
        "occ_symbol TEXT, side TEXT, qty REAL, price REAL, "
        "order_id TEXT, status TEXT, pnl REAL)")
    conn.commit()
    conn.close()
    return str(path)


def _add(db, symbol, side, oid, occ=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (symbol, occ_symbol, side, qty, price, "
        "order_id, status, timestamp) VALUES (?, ?, ?, 1, 1.22, ?, "
        "'open', '2026-07-01T10:00:00')", (symbol, occ, side, oid))
    conn.commit()
    conn.close()


def _status(db):
    conn = sqlite3.connect(db)
    st = conn.execute("SELECT status, pnl FROM trades").fetchone()
    conn.close()
    return st


def _order(status, filled_qty, replaced_by=None):
    # real-surface fake: the guard walks replace chains, and a bare
    # MagicMock's auto-attribute replaced_by would walk forever
    o = MagicMock()
    o.status = status
    o.filled_qty = filled_qty
    o.filled_avg_price = 1.22
    o.replaced_by = replaced_by
    return o


def _run(db, order):
    api = MagicMock()
    if isinstance(order, Exception):
        api.get_order.side_effect = order
    elif isinstance(order, dict):
        api.get_order.side_effect = lambda oid: order[oid]
    else:
        api.get_order.return_value = order
    ctx = MagicMock()
    ctx.get_alpaca_api.return_value = api
    with patch("models.build_user_context_from_profile",
               return_value=ctx):
        return _close_journal_phantom(PROFILES, 56, "CVX260717P00170000",
                                      apply=True)


def test_filled_order_refuses_the_mark(db, caplog):
    # THE p214 shape: the buy IS filled at the broker — visibility
    # lag, not a phantom. The row must stay open and the refusal loud.
    _add(db, "CVX", "buy", "oid-filled", occ="CVX260717P00170000")
    import logging as _l
    with caplog.at_level(_l.ERROR):
        n = _run(db, _order("filled", 1))
    assert n == 0
    assert _status(db) == ("open", None)  # untouched
    # the terminal-unfilled allowlist owns the refusal message now
    assert any("NOT terminal-unfilled" in r.message
               and "filled=1" in r.message for r in caplog.records)


def test_partial_fill_also_refuses(db):
    _add(db, "CVX", "buy", "oid-part", occ="CVX260717P00170000")
    n = _run(db, _order("canceled", 0.5))
    assert n == 0
    assert _status(db)[0] == "open"


def test_broker_verified_unfilled_marks(db):
    # a genuinely never-filled order: the mark proceeds
    _add(db, "CVX", "buy", "oid-dead", occ="CVX260717P00170000")
    n = _run(db, _order("canceled", 0))
    assert n == 1
    # pnl NULL, not 0: a never-filled order realized nothing —
    # 0 would read as a scratch loss to win-rate/tuning consumers
    assert _status(db) == ("auto_reconciled_phantom_close", None)


def test_missing_order_id_refuses(db):
    _add(db, "CVX", "buy", None, occ="CVX260717P00170000")
    n = _run(db, _order("canceled", 0))
    assert n == 0
    assert _status(db)[0] == "open"


def test_unverifiable_lookup_refuses(db):
    _add(db, "CVX", "buy", "oid-err", occ="CVX260717P00170000")
    n = _run(db, RuntimeError("broker on fire"))
    assert n == 0
    assert _status(db)[0] == "open"


def test_working_order_refuses_the_mark(db, caplog):
    # Round-2 H1: "unfilled RIGHT NOW" is not "never filled". A
    # resting limit older than the age guard reads status='new',
    # filled=0 — marking it no-money-moved and having it FILL later
    # is the drift class all over again. Terminal-unfilled only.
    _add(db, "CVX", "buy", "oid-live", occ="CVX260717P00170000")
    import logging as _l
    with caplog.at_level(_l.ERROR):
        n = _run(db, _order("new", 0))
    assert n == 0
    assert _status(db)[0] == "open"
    assert any("NOT terminal-unfilled" in r.message
               for r in caplog.records)


def test_replaced_order_walks_to_filled_successor_and_refuses(db):
    # Round-2 H1: the original id of a replaced order reads filled=0
    # forever; the fill lives on the successor. The guard must walk
    # the chain and judge the TERMINAL order.
    _add(db, "CVX", "buy", "oid-orig", occ="CVX260717P00170000")
    orders = {
        "oid-orig": _order("replaced", 0, replaced_by="oid-succ"),
        "oid-succ": _order("filled", 1),
    }
    n = _run(db, orders)
    assert n == 0
    assert _status(db)[0] == "open"


def test_replaced_order_with_dead_successor_marks(db):
    _add(db, "CVX", "buy", "oid-orig2", occ="CVX260717P00170000")
    orders = {
        "oid-orig2": _order("replaced", 0, replaced_by="oid-succ2"),
        "oid-succ2": _order("canceled", 0),
    }
    n = _run(db, orders)
    assert n == 1
    assert _status(db)[0] == "auto_reconciled_phantom_close"
