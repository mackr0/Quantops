"""Slice 2 — the oversell door's freshness gate (2026-06-23).

The recurring oversell class (p166 PLUG, SMCI, the phantom-equity incident) is
always: the door is asked to sell on a journal that has not been reconciled to
broker truth this cycle. The door is journal-only by design — so if the
journal is stale, the door is fooled.

These tests pin the gate: before any sell, the door forces the symbol fresh
this cycle (just-in-time reconcile if stale), and FAILS CLOSED if it can't.
The p166 oracle FAILS on the pre-gate door (which would have let the naked
re-exit through) and PASSES once the gate is wired.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _db_with_open_buy(tmp_path, symbol, qty, price=2.84):
    import journal
    db = str(tmp_path / f"{symbol}.db")
    journal.init_db(db)
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO trades (symbol, side, qty, price, status, order_id) "
            "VALUES (?,?,?,?,'open','o-buy')", (symbol, "buy", qty, price))
        c.commit()
    return db


def test_p166_oracle_door_refuses_stale_long_broker_already_closed(tmp_path):
    """p166 PLUG mechanism: the journal shows an open long the broker already
    closed (trailing stop fired overnight, never booked). On a stale journal
    the door must NOT allow the re-exit. The freshness gate forces a reconcile
    that books the position flat, so the door refuses the naked sell.

    FAILS on the pre-gate door (journal-only, sees 8421, allows the sell)."""
    import journal, cycle_epoch, order_guard
    import reconcile_journal_to_broker as R
    db = _db_with_open_buy(tmp_path, "PLUG", 8421)
    cycle_epoch.bump()  # symbol now stale this cycle

    # Faithful reconcile stand-in: broker is flat, so reconcile books the
    # open buy closed -> journal long becomes 0.
    def _fake_reconcile(ctx, apply_changes=False, cross_profile_used_ids=None):
        with sqlite3.connect(ctx.db_path) as cc:
            cc.execute("UPDATE trades SET status='closed' "
                       "WHERE symbol='PLUG' AND side='buy'")
            cc.commit()
        return {}

    ctx = SimpleNamespace(db_path=db, display_name="p166",
                          get_alpaca_api=lambda: SimpleNamespace())
    gapi = order_guard.GuardedAlpacaApi(SimpleNamespace(), ctx)
    with patch.object(R, "reconcile_with_ctx", side_effect=_fake_reconcile):
        with pytest.raises(order_guard.OversellGuardError):
            gapi.submit_order(symbol="PLUG", side="sell", qty=8421,
                              type="market", time_in_force="day")


def test_door_fails_closed_when_freshen_raises(tmp_path):
    """If the symbol is stale and the just-in-time reconcile cannot complete
    (broker unreachable), the door REFUSES rather than act on a maybe-stale
    journal. FAILS on the pre-gate door (which never freshens)."""
    import cycle_epoch, order_guard
    import reconcile_journal_to_broker as R
    db = _db_with_open_buy(tmp_path, "GME", 100, price=20.0)
    cycle_epoch.bump()  # stale
    ctx = SimpleNamespace(db_path=db, display_name="p1",
                          get_alpaca_api=lambda: SimpleNamespace())
    gapi = order_guard.GuardedAlpacaApi(SimpleNamespace(), ctx)
    with patch.object(R, "reconcile_with_ctx",
                      side_effect=RuntimeError("broker down")):
        with pytest.raises(order_guard.OversellGuardError):
            gapi.submit_order(symbol="GME", side="sell", qty=100,
                              type="market", time_in_force="day")


def test_door_allows_fresh_owned_sell(tmp_path):
    """The normal path: symbol is fresh this cycle and the profile owns the
    qty -> the door allows the sell and forwards it to the broker (no
    needless reconcile)."""
    import journal, cycle_epoch, order_guard
    import reconcile_journal_to_broker as R
    db = _db_with_open_buy(tmp_path, "AAPL", 100, price=150.0)
    journal.stamp_symbols_fresh(db, ["AAPL"], cycle_epoch.current())
    ctx = SimpleNamespace(db_path=db, display_name="p1",
                          get_alpaca_api=lambda: SimpleNamespace())
    api = MagicMock()
    api.submit_order.return_value = SimpleNamespace(id="ok")
    gapi = order_guard.GuardedAlpacaApi(api, ctx)
    with patch.object(R, "reconcile_with_ctx") as m:
        gapi.submit_order(symbol="AAPL", side="sell", qty=100,
                          type="market", time_in_force="day")
        m.assert_not_called()                 # fresh -> no reconcile
    api.submit_order.assert_called_once()     # forwarded to broker


def test_door_refuses_when_reconcile_returns_error_dict(tmp_path):
    """CRITICAL regression (adversarial review 2026-06-23): reconcile_with_ctx
    RETURNS {"error": ...} on broker-unreachable — it does NOT raise. The door
    must treat that as a FAILED reconcile: do not stamp the symbol fresh, and
    REFUSE the sell. The original code stamped fresh and forwarded the naked
    sell (fail-OPEN) — the exact vector the invariant exists to kill."""
    import journal, cycle_epoch, order_guard
    import reconcile_journal_to_broker as R
    db = _db_with_open_buy(tmp_path, "PLUG", 8421)
    cycle_epoch.bump()
    ep = cycle_epoch.current()
    ctx = SimpleNamespace(db_path=db, display_name="p166",
                          get_alpaca_api=lambda: SimpleNamespace())
    gapi = order_guard.GuardedAlpacaApi(SimpleNamespace(), ctx)
    with patch.object(R, "reconcile_with_ctx",
                      return_value={"error": "failed to fetch positions "
                                    "after retries: timeout"}):
        with pytest.raises(order_guard.OversellGuardError):
            gapi.submit_order(symbol="PLUG", side="sell", qty=8421,
                              type="market", time_in_force="day")
    # the failed reconcile must NOT have marked the symbol fresh
    assert journal.get_symbol_epoch(db, "PLUG") < ep


def test_door_allows_first_time_short_entry(tmp_path):
    """CRITICAL regression (2026-06-24): a declared open_short on a symbol the
    profile has NEVER traded must be ALLOWED. The freshness gate reconciles,
    then stamps the brand-new symbol fresh, so the open_short qty-exemption
    permits it. The buggy version refused EVERY first-time short (the symbol
    stayed stale → the defense-in-depth re-check raised)."""
    import journal, cycle_epoch, order_guard
    import reconcile_journal_to_broker as R
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    cycle_epoch.bump()
    ctx = SimpleNamespace(db_path=db, display_name="p1",
                          get_alpaca_api=lambda: SimpleNamespace())
    api = MagicMock()
    api.submit_order.return_value = SimpleNamespace(id="o-short")
    gapi = order_guard.GuardedAlpacaApi(api, ctx)
    with patch.object(R, "reconcile_with_ctx", return_value={}):
        gapi.submit_order(symbol="NEWCO", side="sell", qty=10,
                          type="market", intent="open_short")
    api.submit_order.assert_called_once()
    # the intent marker must be stripped before it reaches the broker
    assert "intent" not in api.submit_order.call_args.kwargs
