"""2026-06-23 — protective stops must reserve each slice ONCE.

The bug (prod 2026-06-22, 51 "insufficient qty available" failures):
`ensure_protective_stops` placed a `trailing_stop` AND a `limit`
take-profit for the SAME slice. Alpaca holds shares per open sell-side
order, so each profile reserved its slice TWICE, consuming 2× its shares
from the shared Alpaca account pool. The NEXT profile's protective stop
then could not place ("insufficient qty available, requested: N,
available: M<N"), leaving its position NAKED.

A categorized broker+journal pull proved the cause was purely the second
reservation: 0 orphan orders, every order owned by exactly one profile's
journal, and `position - sell_reserved == broker available` on every
symbol (no drift, no mis-tracking). E.g. PLUG: pos 17255, reserved
16846 (8421 trailing + 8421 limit + small siblings), avail 409 — drop
the redundant limit and avail jumps to ~8832, exactly the un-armed slice.

The fix (this file pins it):
  1. ONE sell-side protective order per slice — the trailing (or static
     stop). No broker-side take-profit. The AI's profit target reverts
     to the per-cycle polling check (`check_stop_loss_take_profit`).
  2. Any lingering broker-side TP from the reverted 2026-06-09 design is
     CANCELLED (sunset) at the top of the sweep so its reserved shares
     are freed for the actual stop the same cycle.

These guarantee Σ(reservations) == Σ(positions) on the shared account,
so a profile's own-slice stop always fits — never naked from a self-
inflicted double reservation.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Helpers — a real journal DB + a fake api that records orders/cancels
# ---------------------------------------------------------------------------

def _mk_ctx(use_trailing=True):
    return SimpleNamespace(
        stop_loss_pct=0.05,
        short_stop_loss_pct=None,
        use_trailing_stops=use_trailing,
    )


def _mk_position(symbol="BMNR", qty=2977, avg=20.0):
    return {
        "symbol": symbol, "qty": qty,
        "avg_entry_price": avg,
        "current_price": avg * 1.01,
        "is_option": False, "occ_symbol": None,
    }


def _entry_with(db_path, *, symbol="BMNR", qty=2977, take_profit=None,
                trailing_id=None, tp_id=None, stop_id=None):
    """Insert an open stock BUY entry, optionally pre-stamped with
    protective order ids + a take_profit target."""
    import journal
    journal.init_db(db_path)
    journal.log_trade(symbol=symbol, side="buy", qty=qty, price=20.0,
                      order_id=f"entry-{symbol}", signal_type="BUY",
                      db_path=db_path)
    sets, vals = [], []
    if take_profit is not None:
        sets.append("take_profit = ?"); vals.append(take_profit)
    if trailing_id is not None:
        sets.append("protective_trailing_order_id = ?"); vals.append(trailing_id)
    if tp_id is not None:
        sets.append("protective_tp_order_id = ?"); vals.append(tp_id)
    if stop_id is not None:
        sets.append("protective_stop_order_id = ?"); vals.append(stop_id)
    if sets:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                f"UPDATE trades SET {', '.join(sets)} WHERE symbol = ? "
                f"AND side = 'buy' AND status = 'open'",
                (*vals, symbol),
            )
            conn.commit()


def _fake_api(active_ids=()):
    """api whose get_order reports `active_ids` as live (status=new),
    everything else as canceled; submit_order returns a fresh order
    echoing the requested qty; records submit/cancel calls."""
    api = MagicMock()

    def _get_order(oid, *a, **k):
        if oid in active_ids:
            return SimpleNamespace(id=oid, status="new", order_class="",
                                   legs=[], qty="0")
        return SimpleNamespace(id=oid, status="canceled", order_class="",
                               legs=[], qty="0")

    api.get_order.side_effect = _get_order
    api.list_orders.return_value = []
    api.list_positions.return_value = []

    counter = {"n": 0}

    def _submit(**kwargs):
        counter["n"] += 1
        return SimpleNamespace(id=f"new-{counter['n']}",
                               status="new", qty=str(kwargs.get("qty", 0)))

    api.submit_order.side_effect = _submit
    api.cancel_order.return_value = None
    return api


def _submitted_types(api):
    return [c.kwargs.get("type") for c in api.submit_order.call_args_list]


def _run(api, db_path, ctx, position, monkeypatch, coverage=None):
    # Force the broker-coverage probe to a known value so the test
    # exercises the placement path deterministically.
    monkeypatch.setattr("bracket_orders.active_protective_coverage",
                        lambda _api: coverage or {})
    from bracket_orders import ensure_protective_stops
    ensure_protective_stops(api=api, positions=[position], ctx=ctx,
                            db_path=db_path)


# ---------------------------------------------------------------------------
# 1. Exactly ONE sell-side reservation per slice (no broker TP)
# ---------------------------------------------------------------------------

def test_trailing_mode_places_one_order_no_tp_limit(tmp_path, monkeypatch):
    db = str(tmp_path / "p.db")
    _entry_with(db, take_profit=24.0)          # a TP target IS set...
    api = _fake_api()
    _run(api, db, _mk_ctx(use_trailing=True), _mk_position(), monkeypatch)

    types = _submitted_types(api)
    assert types == ["trailing_stop"], (
        "Exactly one sell-side order (the trailing) must be placed; a "
        f"broker-side TP limit double-reserves the slice. Got: {types}"
    )
    assert "limit" not in types, "no broker-side take-profit limit"
    # The trailing id is stored; the TP column stays empty.
    with closing(sqlite3.connect(db)) as conn:
        trail, tp = conn.execute(
            "SELECT protective_trailing_order_id, protective_tp_order_id "
            "FROM trades WHERE side='buy' AND status='open'").fetchone()
    assert trail and not tp


def test_static_mode_places_one_order_no_tp_limit(tmp_path, monkeypatch):
    db = str(tmp_path / "p.db")
    _entry_with(db, take_profit=24.0)
    api = _fake_api()
    _run(api, db, _mk_ctx(use_trailing=False), _mk_position(), monkeypatch)

    types = _submitted_types(api)
    assert types == ["stop"], (
        f"Static-stop mode must place exactly one stop, no TP. Got: {types}"
    )
    assert "limit" not in types


# ---------------------------------------------------------------------------
# 2. A lingering broker-side TP (reverted design) is sunset
# ---------------------------------------------------------------------------

def test_lingering_broker_tp_is_cancelled_and_cleared(tmp_path, monkeypatch):
    """An entry already stamped with a live trailing + a stale broker TP:
    the TP is cancelled (its reservation freed) and the column cleared,
    while the live trailing is left alone (not re-armed)."""
    db = str(tmp_path / "p.db")
    _entry_with(db, take_profit=24.0,
                trailing_id="trail-live", tp_id="tp-stale")
    api = _fake_api(active_ids=("trail-live",))
    _run(api, db, _mk_ctx(use_trailing=True), _mk_position(), monkeypatch)

    api.cancel_order.assert_any_call("tp-stale")
    # No new order placed: trailing already live, TP no longer placed.
    assert _submitted_types(api) == [], (
        "No new sell-side order should be placed — trailing is live and "
        "the broker TP is retired, not replaced."
    )
    with closing(sqlite3.connect(db)) as conn:
        trail, tp = conn.execute(
            "SELECT protective_trailing_order_id, protective_tp_order_id "
            "FROM trades WHERE side='buy' AND status='open'").fetchone()
    assert trail == "trail-live", "live trailing must be preserved"
    assert tp is None, "sunset must clear the protective_tp_order_id column"


def test_tp_sunset_runs_even_when_position_already_covered(tmp_path, monkeypatch):
    """The TP drain must happen before the broker-coverage skip — a
    position whose trailing the broker already covers still gets its
    stale TP reservation freed."""
    db = str(tmp_path / "p.db")
    _entry_with(db, take_profit=24.0,
                trailing_id="trail-live", tp_id="tp-stale")
    api = _fake_api(active_ids=("trail-live",))
    # broker_coverage reports the trailing fully covers the slice → the
    # coverage check would `continue` early; the sunset must already have run.
    coverage = {("BMNR", "sell"): [
        {"order_id": "trail-live", "qty": 2977, "type": "trailing_stop"}]}
    _run(api, db, _mk_ctx(use_trailing=True), _mk_position(), monkeypatch,
         coverage=coverage)

    api.cancel_order.assert_any_call("tp-stale")
    with closing(sqlite3.connect(db)) as conn:
        tp = conn.execute(
            "SELECT protective_tp_order_id FROM trades "
            "WHERE side='buy' AND status='open'").fetchone()[0]
    assert tp is None


# ---------------------------------------------------------------------------
# 3. Structural pin — the sweep must never place a broker TP again
# ---------------------------------------------------------------------------

def _sweep_body():
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "bracket_orders.py")).read()
    s = src.find("def ensure_protective_stops")
    e = src.find("\ndef ", s + 1)
    return src[s:e if e > 0 else len(src)]


def test_sweep_does_not_place_broker_take_profit():
    body = _sweep_body()
    assert "submit_protective_take_profit(" not in body, (
        "ensure_protective_stops must NOT place a broker-side take-profit "
        "— it double-reserves the slice and starves stops into naked "
        "exposure. TP is enforced by the polling check."
    )
    assert '"type": "limit"' not in body, (
        "no `limit` sell order may be built in the sweep"
    )


def test_sweep_sunsets_lingering_broker_tp():
    body = _sweep_body()
    assert "protective_tp_order_id = NULL" in body, (
        "the sweep must cancel + clear any lingering broker-side TP so "
        "its share reservation is freed for the actual protective stop"
    )
