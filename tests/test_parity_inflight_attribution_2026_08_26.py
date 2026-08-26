"""2026-08-26 — parity audits attribute gaps to OWN in-flight orders
before alarming.

The operator's standing concept: every money movement is attributable
to an own order id. The parity audits used to compare two raw sums and
report the fill handshake's latency as drift (TSM $1.9K/$16.8K on
08-25, the BMNR cover $6.4K on 08-26 — every one $0.00 a cycle later).
Now `_inflight_cash_attribution` values each mid-handshake journal row
against its OWN broker order's actual fills; a gap fully explained by
identified own orders is reported as kind='in_flight' and never enters
the drift (ERROR) list. Fail-closed edges pinned here:

  * a row older than the 30-minute window NEVER attributes — a stuck
    fill machine still alarms;
  * an unverifiable order attributes nothing;
  * a partially explained gap still alarms, with the residual shown.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import aggregate_audit as agg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _db(tmp_path, name="quantopsai_profile_7.db"):
    path = str(tmp_path / name)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades ("
        " id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, qty REAL,"
        " price REAL, fill_price REAL, status TEXT, order_id TEXT,"
        " occ_symbol TEXT, timestamp TEXT)")
    conn.commit()
    conn.close()
    return path


def _row(path, id_, symbol, side, qty, price, fill_price, status,
         order_id, occ=None, age_min=0):
    ts = (datetime.utcnow() - timedelta(minutes=age_min)).isoformat()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO trades (id, symbol, side, qty, price, fill_price,"
        " status, order_id, occ_symbol, timestamp)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (id_, symbol, side, qty, price, fill_price, status, order_id,
         occ, ts))
    conn.commit()
    conn.close()


class _Order:
    def __init__(self, filled_qty, filled_avg_price):
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price


class _FakeApi:
    def __init__(self, orders=None):
        self._orders = orders or {}

    def get_order(self, oid):
        return self._orders.get(oid)


def _cached(api, oid):
    return api.get_order(oid)


# ---------------------------------------------------------------------------
# 1. The attribution helper
# ---------------------------------------------------------------------------

class TestInflightHelper:
    def test_attributes_inflight_cover(self, tmp_path):
        """THE BMNR shape: cover journaled with no price yet, broker
        already filled 254 @ 25.12 → broker paid $6,380.48 the journal
        hasn't booked."""
        db = _db(tmp_path)
        _row(db, 1, "BMNR", "cover", 254, None, None, "open", "ord-1")
        api = _FakeApi({"ord-1": _Order(254, 25.12)})
        with patch("order_status_cache.get_order_cached", _cached):
            out = agg._inflight_cash_attribution(api, [db])
        assert out["total"] == pytest.approx(-254 * 25.12)
        assert [o["order_id"] for o in out["orders"]] == ["ord-1"]

    def test_stale_row_never_attributes(self, tmp_path):
        """A 45-minute-old unstamped row is a REAL desync, not an
        in-flight order — it must stay unexplained so the gap alarms."""
        db = _db(tmp_path)
        _row(db, 1, "BMNR", "cover", 254, None, None, "open", "ord-1",
             age_min=45)
        api = _FakeApi({"ord-1": _Order(254, 25.12)})
        with patch("order_status_cache.get_order_cached", _cached):
            out = agg._inflight_cash_attribution(api, [db])
        assert out["total"] == 0.0
        assert out["orders"] == []

    def test_unverifiable_order_attributes_nothing(self, tmp_path):
        db = _db(tmp_path)
        _row(db, 1, "BMNR", "cover", 254, None, None, "open", "ord-1")
        api = _FakeApi({})  # order lookup returns None
        with patch("order_status_cache.get_order_cached", _cached):
            out = agg._inflight_cash_attribution(api, [db])
        assert out["total"] == 0.0

    def test_pending_fill_exit_at_decision_price(self, tmp_path):
        """Exit booked at decision price, broker not yet filled: the
        journal credited proceeds early — attribution is the negative
        of that early credit."""
        db = _db(tmp_path)
        _row(db, 1, "TGT", "sell", 73, 170.0, None, "pending_fill",
             "ord-2")
        api = _FakeApi({"ord-2": _Order(0, 0)})
        with patch("order_status_cache.get_order_cached", _cached):
            out = agg._inflight_cash_attribution(api, [db])
        assert out["total"] == pytest.approx(-73 * 170.0)

    def test_option_rows_respect_stock_only_flag(self, tmp_path):
        db = _db(tmp_path)
        _row(db, 1, "JPM", "buy", 1, None, None, "open", "ord-3",
             occ="JPM261002C00375000")
        api = _FakeApi({"ord-3": _Order(1, 3.8)})
        with patch("order_status_cache.get_order_cached", _cached):
            full = agg._inflight_cash_attribution(api, [db])
            stock = agg._inflight_cash_attribution(
                api, [db], include_options=False)
        assert full["total"] == pytest.approx(-380.0)  # 100x multiplier
        assert stock["total"] == 0.0

    def test_stamped_settled_row_contributes_zero(self, tmp_path):
        """A pending_fill exit whose fill already stamped: journal and
        broker agree — no attribution noise."""
        db = _db(tmp_path)
        _row(db, 1, "KLAC", "sell", 67, 181.35, 182.54, "pending_fill",
             "ord-4")
        api = _FakeApi({"ord-4": _Order(67, 182.54)})
        with patch("order_status_cache.get_order_cached", _cached):
            out = agg._inflight_cash_attribution(api, [db])
        assert out["total"] == 0.0
        assert out["orders"] == []


# ---------------------------------------------------------------------------
# 2. Wiring — cash parity
# ---------------------------------------------------------------------------

def _ctx(db_path):
    return SimpleNamespace(
        alpaca_account_id=61, db_path=db_path, initial_capital=100_000.0,
        get_alpaca_api=lambda api=None: _FakeApi(),
    )


def _run_cash(db_path, api, broker_cash, journal_cash):
    ctx = SimpleNamespace(
        alpaca_account_id=61, db_path=db_path,
        initial_capital=100_000.0, get_alpaca_api=lambda: api)
    with patch("models.build_user_context_from_profile",
               return_value=ctx), \
         patch.object(agg, "_broker_cash", lambda a: broker_cash), \
         patch.object(agg, "_journal_cash",
                      lambda p, i: journal_cash), \
         patch.object(agg, "_account_fee_net", lambda a: 0.0), \
         patch("order_status_cache.get_order_cached", _cached):
        return agg.audit_account_cash_parity([7])


class TestCashParityWiring:
    def test_fully_attributed_gap_is_in_flight_not_error(self, tmp_path):
        db = _db(tmp_path)
        _row(db, 1, "BMNR", "cover", 254, None, None, "open", "ord-1")
        api = _FakeApi({"ord-1": _Order(254, 25.12)})
        res = _run_cash(db, api, broker_cash=93_619.52,
                        journal_cash=100_000.0)
        assert res["drift"] == []
        row = res["accounts"][61]
        assert row["kind"] == "in_flight"
        assert row["residual"] == pytest.approx(0.0, abs=0.02)
        assert [o["order_id"] for o in row["in_flight"]] == ["ord-1"]

    def test_unattributed_gap_still_alarms(self, tmp_path):
        db = _db(tmp_path)  # no in-flight rows at all
        api = _FakeApi({})
        res = _run_cash(db, api, broker_cash=93_619.52,
                        journal_cash=100_000.0)
        assert len(res["drift"]) == 1
        row = res["drift"][0]
        assert row["kind"] == "journal_cash_phantom"
        assert row["residual"] == row["drift"]

    def test_partially_attributed_gap_alarms_with_residual(self, tmp_path):
        """In-flight explains $6,380.48 of a $16,380.48 gap — the
        $10,000 nobody's order explains must still scream."""
        db = _db(tmp_path)
        _row(db, 1, "BMNR", "cover", 254, None, None, "open", "ord-1")
        api = _FakeApi({"ord-1": _Order(254, 25.12)})
        res = _run_cash(db, api, broker_cash=83_619.52,
                        journal_cash=100_000.0)
        assert len(res["drift"]) == 1
        row = res["drift"][0]
        assert row["kind"] == "journal_cash_phantom"
        assert row["residual"] == pytest.approx(-10_000.0, abs=0.02)


# ---------------------------------------------------------------------------
# 3. Wiring — value parity
# ---------------------------------------------------------------------------

class TestValueParityWiring:
    def test_inflight_cover_suppresses_value_orphan(self, tmp_path):
        """BMNR value side: journal still shows the short (-$6,380),
        broker already flat → +$6,380 'orphan' fully explained by the
        in-flight cover."""
        db = _db(tmp_path)
        _row(db, 1, "BMNR", "cover", 254, None, None, "open", "ord-1")
        api = _FakeApi({"ord-1": _Order(254, 25.12)})
        ctx = SimpleNamespace(
            alpaca_account_id=61, db_path=db,
            initial_capital=100_000.0, get_alpaca_api=lambda: api)
        with patch("models.build_user_context_from_profile",
                   return_value=ctx), \
             patch("client._make_price_fetcher",
                   return_value=None), \
             patch.object(agg, "_journal_positions_value",
                          lambda p, price_fetcher=None: -6380.48), \
             patch.object(agg, "_broker_positions_value",
                          lambda a: 0.0), \
             patch("order_status_cache.get_order_cached", _cached):
            res = agg.audit_account_value_parity([7])
        assert res["drift"] == []
        row = res["accounts"][61]
        assert row["kind"] == "in_flight"
        assert row["residual"] == pytest.approx(0.0, abs=0.02)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
