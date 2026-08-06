"""2026-08-05 — canceled ≠ unfilled reaches the multileg rollback (the
p213 NEE orphan).

A bull call spread's leg 0 (buy 5× NEE $91C) filled at the broker;
leg 1 died on a 429 rate-limit storm — and so did the rollback
reversal. The old code journaled the open only AFTER a successful
reversal submit, so the except path skipped journaling entirely and
$485 of filled calls existed in no book (broker_orphan + cash-parity
drift). Separately, the cancel-based rollback treated "already filled"
as rollback success and _mark_legs_canceled voided rows whose orders
had really filled.

Pins:
  1. Sequential leg-failure rollback journals the OPEN row BEFORE the
     reversal attempt — a reversal failure cannot orphan the fill.
  2. Reversal success journals open + close (regression).
  3. _rollback_multileg_broker_orders reads back every cancel and
     returns {order_id: filled_qty}; unknown read-backs fail closed.
  4. _mark_legs_canceled never cancel-flags a row whose order filled.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

EXPIRY = date(2026, 9, 11)


def _bull_call_spread():
    from options_multileg import build_bull_call_spread
    return build_bull_call_spread("NEE", EXPIRY, 91, 95, qty=5)


def _mk_db(tmp_path):
    from journal import init_db
    db = str(tmp_path / "p.db")
    init_db(db)
    return db


def _rows(db):
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT id, side, qty, order_id, status, occ_symbol "
            "FROM trades ORDER BY id")]


class TestLegFailureRollbackJournalsOpenFirst:
    def _run(self, reversal_fails):
        """Leg 0 submit succeeds (returns order leg0-open), leg 1
        raises 429; reversal submit raises too when reversal_fails."""
        from options_multileg import execute_multileg_strategy
        strategy = _bull_call_spread()
        calls = {"n": 0}

        def _submit(api, payload):
            calls["n"] += 1
            o = MagicMock()
            if payload.get("position_intent", "").endswith("_to_open"):
                if payload["side"] == "buy":
                    o.id = "leg0-open"
                    return o
                raise RuntimeError(
                    'Alpaca order rejected (429): rate limit exceeded')
            # reversal (close intent)
            if reversal_fails:
                raise RuntimeError(
                    'Alpaca order rejected (429): rate limit exceeded')
            o.id = "leg0-close"
            return o

        db = _mk_db(self.tmp_path)
        api = MagicMock()
        with patch("options_multileg._combo_submit_with_retry",
                   side_effect=RuntimeError(
                       "Alpaca order rejected (429): rate limit "
                       "exceeded")), \
             patch("options_multileg._submit_alpaca_order_raw",
                   side_effect=_submit), \
             patch("options_chain_alpaca.list_available_contracts",
                   return_value=[]):
            ctx = SimpleNamespace(db_path=db)
            result = execute_multileg_strategy(
                api, strategy, ctx=ctx, log=True, use_combo=True,
            )
        return db, result

    def test_reversal_failure_still_journals_the_open(self, tmp_path):
        self.tmp_path = tmp_path
        db, result = self._run(reversal_fails=True)
        rows = _rows(db)
        opens = [r for r in rows if r["order_id"] == "leg0-open"]
        assert opens, (
            "the FILLED open leg must be journaled even when the "
            "reversal submit dies (the p213 NEE orphan shape)")
        assert opens[0]["status"] == "pending_fill"
        assert result["action"] == "ERROR"

    def test_reversal_success_journals_open_and_close(self, tmp_path):
        self.tmp_path = tmp_path
        db, result = self._run(reversal_fails=False)
        ids = {r["order_id"] for r in _rows(db)}
        assert "leg0-open" in ids and "leg0-close" in ids


class TestRollbackReadsBackFills:
    def _api(self, filled_qty, raise_on_cancel=None):
        api = MagicMock()
        if raise_on_cancel:
            api.cancel_order.side_effect = Exception(raise_on_cancel)
        post = MagicMock()
        post.filled_qty = filled_qty
        post.status = "canceled" if not filled_qty else "filled"
        post.legs = None
        api.get_order.return_value = post
        return api

    def test_filled_order_reported(self):
        from options_multileg import _rollback_multileg_broker_orders
        api = self._api(5, raise_on_cancel="order is already filled")
        out = _rollback_multileg_broker_orders(api, None, ["oid-1"])
        assert out == {"oid-1": 5.0}

    def test_unfilled_cancel_reports_nothing(self):
        from options_multileg import _rollback_multileg_broker_orders
        api = self._api(0)
        out = _rollback_multileg_broker_orders(api, None, ["oid-1"])
        assert out == {}

    def test_unreadable_order_fails_closed(self):
        from options_multileg import _rollback_multileg_broker_orders
        api = MagicMock()
        api.get_order.side_effect = RuntimeError("api down")
        out = _rollback_multileg_broker_orders(api, None, ["oid-1"])
        assert out.get("oid-1") == -1.0


class TestMarkLegsCanceledRefusesFilledRows:
    def test_filled_row_stays_alive(self, tmp_path):
        from options_multileg import _mark_legs_canceled
        db = _mk_db(tmp_path)
        with sqlite3.connect(db) as c:
            c.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, order_id, status, occ_symbol) VALUES "
                "('2026-08-05T17:12:28','NEE','buy',5,0.97,"
                "'oid-filled','open','NEE260911C00091000')")
            c.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, order_id, status, occ_symbol) VALUES "
                "('2026-08-05T17:12:29','NEE','sell',5,0.4,"
                "'oid-unfilled','open','NEE260911C00095000')")
            ids = [r[0] for r in c.execute(
                "SELECT id FROM trades ORDER BY id")]
        _mark_legs_canceled(db, ids,
                            filled_order_ids={"oid-filled": 5.0})
        rows = {r["order_id"]: r["status"] for r in _rows(db)}
        assert rows["oid-filled"] == "open", (
            "a row whose broker order FILLED must never be voided")
        assert rows["oid-unfilled"] == "canceled"
