"""2026-08-31 — the quantity audit re-checks before alarming.

The PEP +93 false broker_orphan: the sweep reads 12 journals then the
broker over several seconds; an entry filling mid-sweep is counted
broker-side but not journal-side. The audit now (1) re-reads JUST the
drifting symbol at one instant — read-skew vanishes; (2) attributes
any residual to OWN submitted-but-not-yet-journaled orders (the
durable ledger, written before broker submit); (3) only an
unexplained residual flags. Unverifiable never suppresses.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import aggregate_audit as agg


class _Order:
    def __init__(self, filled_qty):
        self.filled_qty = filled_qty


class _FakeApi:
    def __init__(self, orders=None):
        self._orders = orders or {}

    def get_order(self, oid):
        return self._orders.get(oid)


def _mk_db(tmp_path, name):
    path = str(tmp_path / name)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, order_id TEXT)")
    conn.execute(
        "CREATE TABLE submitted_orders (order_id TEXT PRIMARY KEY,"
        " symbol TEXT, side TEXT, qty REAL, occ_symbol TEXT,"
        " intent TEXT, submitted_at TEXT NOT NULL"
        " DEFAULT (datetime('now')))")
    conn.commit()
    conn.close()
    return path


def _run(tmp_path, journal_reads, broker_reads, orders=None,
         submitted=None):
    """Drive audit_aggregate_drift with scripted journal/broker reads.
    `journal_reads` and `broker_reads` are consumed in call order so
    the sweep read and the fresh re-check can differ (read-skew)."""
    db = _mk_db(tmp_path, "quantopsai_profile_7.db")
    if submitted:
        conn = sqlite3.connect(db)
        for oid, sym, side in submitted:
            conn.execute(
                "INSERT INTO submitted_orders (order_id, symbol, side,"
                " qty) VALUES (?,?,?,0)", (oid, sym, side))
        conn.commit()
        conn.close()
    api = _FakeApi(orders or {})
    ctx = SimpleNamespace(alpaca_account_id=61, db_path=db,
                          get_alpaca_api=lambda: api)
    j_seq = list(journal_reads)
    b_seq = list(broker_reads)
    with patch("models.build_user_context_from_profile",
               return_value=ctx), \
         patch.object(agg, "_journal_open_qty_per_symbol",
                      side_effect=lambda p: dict(j_seq.pop(0))), \
         patch.object(agg, "_broker_qty_per_symbol",
                      side_effect=lambda a: dict(b_seq.pop(0))), \
         patch("order_status_cache.get_order_cached",
               lambda a, o: a.get_order(o)):
        return agg.audit_aggregate_drift([7])


class TestQtyAuditRecheck:
    def test_read_skew_suppressed_as_in_flight(self, tmp_path):
        """Sweep saw 161 vs broker 254 (the PEP shape); the fresh
        re-read sees 254 == 254 → in_flight, no ERROR."""
        res = _run(tmp_path,
                   journal_reads=[{"PEP": 161}, {"PEP": 254}],
                   broker_reads=[{"PEP": 254}, {"PEP": 254}])
        assert res["drift"] == []
        assert res["accounts"][61]["PEP"]["kind"] == "in_flight"

    def test_missing_row_order_attributes_residual(self, tmp_path):
        """Journal row not yet written but the durable ledger has the
        order and the broker filled it → attributed, no ERROR."""
        res = _run(tmp_path,
                   journal_reads=[{"PEP": 161}, {"PEP": 161}],
                   broker_reads=[{"PEP": 254}, {"PEP": 254}],
                   orders={"ord-93": _Order(93)},
                   submitted=[("ord-93", "PEP", "buy")])
        assert res["drift"] == []
        assert res["accounts"][61]["PEP"]["kind"] == "in_flight"

    def test_genuine_orphan_still_flags(self, tmp_path):
        """No own order explains the gap → the ERROR survives."""
        res = _run(tmp_path,
                   journal_reads=[{"PEP": 161}, {"PEP": 161}],
                   broker_reads=[{"PEP": 254}, {"PEP": 254}])
        assert len(res["drift"]) == 1
        assert res["drift"][0]["kind"] == "broker_orphan"

    def test_unverifiable_recheck_never_suppresses(self, tmp_path):
        """Broker unreadable on re-check → flag stands (unverifiable
        is not an explanation)."""
        db = _mk_db(tmp_path, "quantopsai_profile_7.db")
        api = _FakeApi()
        ctx = SimpleNamespace(alpaca_account_id=61, db_path=db,
                              get_alpaca_api=lambda: api)
        b_seq = [{"PEP": 254}, None]
        with patch("models.build_user_context_from_profile",
                   return_value=ctx), \
             patch.object(agg, "_journal_open_qty_per_symbol",
                          side_effect=lambda p: {"PEP": 161}), \
             patch.object(agg, "_broker_qty_per_symbol",
                          side_effect=lambda a: b_seq.pop(0)):
            res = agg.audit_aggregate_drift([7])
        assert len(res["drift"]) == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
