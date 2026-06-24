"""Slice 1 of the broker/journal divergence-class elimination (2026-06-23).

The freshness ledger is the foundation of the invariant: every (profile,
symbol) reconciled to broker truth is stamped with the live cycle epoch, and
the oversell door refuses to act on any symbol whose stamp is older than the
current epoch. These tests pin the ledger + epoch clock + the
ensure_symbol_fresh just-in-time reconcile behavior.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _fresh_db(tmp_path) -> str:
    import journal
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    return db


def test_cycle_epoch_monotonic():
    import cycle_epoch
    a = cycle_epoch.current()
    b = cycle_epoch.bump()
    # Strictly increasing is the contract. bump() advances by at least 1, but
    # may jump further to catch up to wall-clock seconds (the restart-safety
    # seeding) — so assert monotonic, not exactly +1.
    assert b > a
    assert cycle_epoch.current() == b
    assert cycle_epoch.bump() > b


def test_cycle_epoch_wall_clock_seeded_for_restart_safety():
    """The epoch must be seeded from the wall clock so it is MONOTONIC ACROSS
    RESTARTS — otherwise a stamp written by a prior run would read 'fresh'
    against a reset counter and the door's just-in-time gate would fail OPEN
    after every restart. A unix-time-scale epoch makes any small prior stamp
    (and the never-stamped 0) older than the live epoch → stale → reconciled."""
    import cycle_epoch
    assert cycle_epoch.current() >= 1_600_000_000   # unix-time scale, not 1
    assert cycle_epoch.current() > 113              # a prior-run stamp is stale


def test_stamp_and_get_roundtrip(tmp_path):
    import journal
    db = _fresh_db(tmp_path)
    assert journal.get_symbol_epoch(db, "AAPL") == 0  # never stamped -> stale
    journal.stamp_symbols_fresh(db, ["AAPL", "msft"], 7)
    assert journal.get_symbol_epoch(db, "AAPL") == 7
    assert journal.get_symbol_epoch(db, "MSFT") == 7   # case-insensitive
    assert journal.get_symbol_epoch(db, "TSLA") == 0
    journal.stamp_symbols_fresh(db, ["AAPL"], 9)       # re-stamp advances
    assert journal.get_symbol_epoch(db, "AAPL") == 9


def test_get_symbol_epoch_missing_table_is_stale(tmp_path):
    """A raw DB with no reconcile_state table reads epoch 0 (fail-safe stale),
    never raises."""
    import journal
    db = str(tmp_path / "raw.db")
    sqlite3.connect(db).close()
    assert journal.get_symbol_epoch(db, "AAPL") == 0


def test_ensure_symbol_fresh_skips_when_fresh(tmp_path):
    import journal, cycle_epoch
    import reconcile_journal_to_broker as R
    db = _fresh_db(tmp_path)
    ep = cycle_epoch.current()
    journal.stamp_symbols_fresh(db, ["AAPL"], ep)
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: SimpleNamespace())
    with patch.object(R, "reconcile_with_ctx") as m:
        R.ensure_symbol_fresh(ctx, "AAPL")
        m.assert_not_called()  # already fresh -> no broker reconcile


def test_ensure_symbol_fresh_reconciles_when_stale(tmp_path):
    import journal, cycle_epoch
    import reconcile_journal_to_broker as R
    db = _fresh_db(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO trades (symbol, side, qty, price, status) "
                  "VALUES ('AAPL','buy',10,100,'open')")
        c.commit()
    cycle_epoch.bump()                       # invalidate any prior stamp
    ep = cycle_epoch.current()
    assert journal.get_symbol_epoch(db, "AAPL") < ep   # stale
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: SimpleNamespace())
    with patch.object(R, "reconcile_with_ctx", return_value={}) as m:
        R.ensure_symbol_fresh(ctx, "AAPL")
        m.assert_called_once()               # stale -> forced reconcile
    assert journal.get_symbol_epoch(db, "AAPL") == ep  # now fresh


def test_ensure_symbol_fresh_skips_when_ctx_has_no_broker_handle(tmp_path):
    """A ctx with no broker handle (no get_alpaca_api / api) is a non-broker
    context — nothing to reconcile against — so the gate is a no-op rather
    than erroring. Production UserContexts always have get_alpaca_api, so this
    only spares unit-test doubles."""
    import cycle_epoch
    import reconcile_journal_to_broker as R
    db = _fresh_db(tmp_path)
    cycle_epoch.bump()
    ctx = SimpleNamespace(db_path=db)        # no get_alpaca_api, no api
    with patch.object(R, "reconcile_with_ctx") as m:
        R.ensure_symbol_fresh(ctx, "AAPL")   # must not raise
        m.assert_not_called()


def _db_with_open_aapl(tmp_path):
    import journal
    db = _fresh_db(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO trades (symbol,side,qty,price,status) "
                  "VALUES ('AAPL','buy',10,100,'open')")
        c.commit()
    return db


def test_reconcile_and_stamp_refuses_to_stamp_on_broker_error(tmp_path):
    """CRITICAL (adversarial review): reconcile_with_ctx RETURNS {"error":...}
    on broker-unreachable (it never raises). reconcile_and_stamp must NOT stamp
    fresh and must raise ReconcileUnavailable so the door fails closed."""
    import journal, cycle_epoch
    import reconcile_journal_to_broker as R
    db = _db_with_open_aapl(tmp_path)
    cycle_epoch.bump()
    ep = cycle_epoch.current()
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: SimpleNamespace())
    with patch.object(R, "reconcile_with_ctx",
                      return_value={"error": "failed to fetch positions"}):
        with pytest.raises(R.ReconcileUnavailable):
            R.reconcile_and_stamp(ctx, epoch=ep)
    assert journal.get_symbol_epoch(db, "AAPL") < ep   # NOT stamped fresh


def test_ensure_symbol_fresh_refuses_on_broker_error(tmp_path):
    import cycle_epoch
    import reconcile_journal_to_broker as R
    db = _db_with_open_aapl(tmp_path)
    cycle_epoch.bump()
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: SimpleNamespace())
    with patch.object(R, "reconcile_with_ctx",
                      return_value={"error": "broker down"}):
        with pytest.raises(R.ReconcileUnavailable):
            R.ensure_symbol_fresh(ctx, "AAPL")


def test_reconcile_and_stamp_skipped_profile_no_stamp_no_raise(tmp_path):
    """A 'skipped' profile (no broker account) reconciles nothing: don't stamp
    and don't raise — its journal is its only truth."""
    import journal, cycle_epoch
    import reconcile_journal_to_broker as R
    db = _db_with_open_aapl(tmp_path)
    cycle_epoch.bump()
    ep = cycle_epoch.current()
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: SimpleNamespace())
    with patch.object(R, "reconcile_with_ctx",
                      return_value={"skipped": "no alpaca_account_id"}):
        R.reconcile_and_stamp(ctx, epoch=ep)          # must NOT raise
    assert journal.get_symbol_epoch(db, "AAPL") < ep  # not stamped
