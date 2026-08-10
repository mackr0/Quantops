"""2026-08-08 — the evidence hierarchy + the resurrection net (the
p211 BAC 60P wrongful void).

A live short put (3 contracts, fill broker-verified, fill_price
backfilled onto the row) was VOIDED by the phantom sweep on the
strength of ONE transient 404 — the position became a broker orphan
and the account failed cash parity. Operator directive: no more
per-writer patches; make the class impossible.

Two mechanisms, both pinned here:
  1. EVIDENCE HIERARCHY at the void writers: a row booked on a
     verified fill can never be destroyed by a failed lookup, and a
     404 only voids after a direct cache-bypassing confirm.
  2. THE NET: every cycle, every dead row's broker order is checked;
     an order with FILLS is a real position and the row is restored
     from broker evidence — instrument-agnostic, writer-agnostic. A
     wrongful void survives at most one cycle no matter which code
     path caused it.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from phantom_sweep import _row_verdict, resurrect_wrongly_voided  # noqa: E402


class _Order:
    def __init__(self, status="filled", filled_qty=0, filled_avg_price=0):
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price


TERMINAL = {"canceled", "expired", "rejected", "done_for_day", "filled"}


class TestEvidenceHierarchyAtVoidWriters:
    def test_single_404_with_fill_evidence_never_voids(self):
        api = MagicMock()
        api.get_order.side_effect = RuntimeError("404 order not found")

        def cached(api_, oid):
            raise RuntimeError("404 order not found")

        v = _row_verdict(api, "oid", TERMINAL, cached,
                         has_fill_evidence=True)
        assert v is None, (
            "a row booked on a verified fill must never be voided by "
            "a failed lookup")
        api.get_order.assert_not_called()

    def test_transient_404_without_evidence_skips_on_confirm(self):
        """Cached read 404s; the direct confirm SUCCEEDS — transient,
        no void."""
        api = MagicMock()
        api.get_order.return_value = _Order("filled", 3, 0.72)

        def cached(api_, oid):
            raise RuntimeError("404 order not found")

        v = _row_verdict(api, "oid", TERMINAL, cached,
                         has_fill_evidence=False)
        assert v is None
        api.get_order.assert_called_once()

    def test_persistent_404_without_evidence_voids(self):
        api = MagicMock()
        api.get_order.side_effect = RuntimeError("404 order not found")

        def cached(api_, oid):
            raise RuntimeError("404 order not found")

        v = _row_verdict(api, "oid", TERMINAL, cached,
                         has_fill_evidence=False)
        assert v is not None and "404" in v

    def test_terminal_unfilled_with_fill_evidence_refused(self):
        """Broker says terminal-unfilled but the row carries a
        verified fill — a contradiction is an alarm, never a silent
        void."""
        api = MagicMock()

        def cached(api_, oid):
            return _Order("canceled", 0, 0)

        v = _row_verdict(api, "oid", TERMINAL, cached,
                         has_fill_evidence=True)
        assert v is None

    def test_terminal_unfilled_without_evidence_still_voids(self):
        api = MagicMock()

        def cached(api_, oid):
            return _Order("canceled", 0, 0)

        v = _row_verdict(api, "oid", TERMINAL, cached,
                         has_fill_evidence=False)
        assert v is not None and "terminal-unfilled" in v


def _mk_db(tmp_path):
    from journal import init_db
    db = str(tmp_path / "p.db")
    init_db(db)
    return db


def _insert(db, **kw):
    cols = ", ".join(kw)
    ph = ", ".join("?" for _ in kw)
    with sqlite3.connect(db) as c:
        return c.execute(
            f"INSERT INTO trades ({cols}) VALUES ({ph})",
            list(kw.values())).lastrowid


def _row(db, rid):
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute(
            "SELECT * FROM trades WHERE id=?", (rid,)).fetchone())


class _NetAPI:
    """get_order returns per-id orders; used via order_status_cache's
    signature (api, order_id)."""

    def __init__(self, orders):
        self.orders = orders

    def get_order(self, oid):
        if oid not in self.orders:
            raise RuntimeError("404 not found")
        return self.orders[oid]


def _run_net(db, orders, monkeypatch):
    import phantom_sweep
    import order_status_cache
    monkeypatch.setattr(order_status_cache, "get_order_cached",
                        lambda api, oid, **kw: api.get_order(oid))
    monkeypatch.setattr(order_status_cache, "rate_limited",
                        lambda: False)
    ctx = SimpleNamespace(db_path=db, display_name="test")
    return phantom_sweep.resurrect_wrongly_voided(
        ctx, api=_NetAPI(orders))


class TestResurrectionNet:
    def test_bac_shape_short_option_restored_to_open(self, tmp_path,
                                                     monkeypatch):
        """The live incident: sell-to-open put, filled 3 @ 0.72,
        voided (canceled, price=0). The net must restore it as an
        OPEN short position with broker truth."""
        db = _mk_db(tmp_path)
        rid = _insert(db, timestamp="2026-08-06T15:46:04", symbol="BAC",
                      side="sell", qty=3.0, price=0.0, fill_price=0.72,
                      order_id="oid-bac", status="canceled",
                      occ_symbol="BAC260918P00060000",
                      signal_type="OPTIONS")
        out = _run_net(db, {"oid-bac": _Order("filled", 3, 0.72)},
                       monkeypatch)
        assert out["resurrected"] == 1
        r = _row(db, rid)
        assert r["status"] == "open"
        assert r["qty"] == 3.0 and r["price"] == 0.72
        assert "RESURRECTED" in (r["reason"] or "")

    def test_stock_entry_restored_open_with_partial_qty(self, tmp_path,
                                                        monkeypatch):
        """A canceled BUY whose order partially filled (7 of 10) comes
        back as an open lot of 7 — broker truth, not journal hope."""
        db = _mk_db(tmp_path)
        rid = _insert(db, timestamp="2026-08-06T15:00:00", symbol="NEE",
                      side="buy", qty=10.0, price=0.0, fill_price=None,
                      order_id="oid-buy", status="canceled")
        out = _run_net(db, {"oid-buy": _Order("canceled", 7, 86.5)},
                       monkeypatch)
        assert out["resurrected"] == 1
        r = _row(db, rid)
        assert r["status"] == "open" and r["qty"] == 7.0
        assert r["fill_price"] == 86.5

    def test_stock_exit_restored_pending_fill(self, tmp_path,
                                              monkeypatch):
        db = _mk_db(tmp_path)
        _insert(db, timestamp="2026-08-01T10:00:00", symbol="XOM",
                side="buy", qty=5.0, price=100.0, fill_price=100.0,
                order_id="oid-open", status="open")
        rid = _insert(db, timestamp="2026-08-06T15:00:00", symbol="XOM",
                      side="sell", qty=5.0, price=0.0,
                      order_id="oid-exit", status="canceled")
        out = _run_net(db, {"oid-exit": _Order("filled", 5, 104.0)},
                       monkeypatch)
        assert out["resurrected"] == 1
        assert _row(db, rid)["status"] == "pending_fill", (
            "a filled exit resurrects as pending_fill so the "
            "update_fills state machine closes it through the FIFO")

    def test_genuinely_unfilled_void_stands(self, tmp_path, monkeypatch):
        db = _mk_db(tmp_path)
        rid = _insert(db, timestamp="2026-08-06T15:00:00", symbol="DIS",
                      side="buy", qty=4.0, price=0.0,
                      order_id="oid-dead", status="canceled")
        out = _run_net(db, {"oid-dead": _Order("canceled", 0, 0)},
                       monkeypatch)
        assert out["resurrected"] == 0
        assert _row(db, rid)["status"] == "canceled"

    def test_unverifiable_order_left_for_next_cycle(self, tmp_path,
                                                    monkeypatch):
        db = _mk_db(tmp_path)
        rid = _insert(db, timestamp="2026-08-06T15:00:00", symbol="F",
                      side="buy", qty=4.0, price=0.0,
                      order_id="oid-404", status="canceled")
        out = _run_net(db, {}, monkeypatch)  # every lookup 404s
        assert out["resurrected"] == 0
        assert _row(db, rid)["status"] == "canceled"

    def test_net_is_wired_into_every_sweep(self):
        src = open(os.path.join(
            os.path.dirname(__file__), os.pardir,
            "phantom_sweep.py")).read()
        i = src.index("def sweep_profile")
        j = src.index("def resurrect_wrongly_voided")
        body = src[i:j] if j > i else src[i:]
        assert "resurrect_wrongly_voided(" in body, (
            "the resurrection net must run wherever the sweep runs — "
            "every cycle, every profile")
