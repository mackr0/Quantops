"""2026-06-16 — restore stop-loss protection that three bugs had
quietly disabled, letting SUGP run to −35% across 5 profiles and
leaving 152 positions with no live broker stop.

Fix 1 (portfolio_manager): the polling stop-loss skipped EVERY sub-$2
position with a >5% drop as a "suspected option leg" — also skipping
legitimate penny stocks, so SUGP was never stopped. Real options are
already excluded by the occ_symbol/is_option filter; a genuine sub-$2
stock past its stop must now trigger.

Fix 2 (bracket_orders): the protective sweep deferred to "the broker
manages stop+TP" for ANY bracket entry without checking a child was
actually live. Dead-bracket positions (children canceled or never
materialized) stayed naked forever. Pinned structurally in
test_bracket_skip_sweep_2026_06_10.py.

Fix 3 (multi_scheduler + order_guard): the stale-limit canceller
canceled bracket take-profit children (limit orders that live for the
whole position), and the OCO link killed the paired stop too. Protective
orders must be excluded from the stale-cancel.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# Fix 1 — sub-$2 real stock gets stop-lossed; options still skipped
# ---------------------------------------------------------------------------


class TestPennyStockStopRestored:

    def test_sub2_REAL_stock_past_stop_triggers_sell(self, tmp_path):
        """SUGP shape: a real sub-$2 STOCK (journaled stock BUY) that is
        35% past its 5% stop MUST trigger — the journal confirms it's a
        stock, not a stranded option leg."""
        from journal import init_db, log_trade
        from portfolio_manager import check_stop_loss_take_profit
        db = str(tmp_path / "p.db")
        init_db(db)
        log_trade(symbol="SUGP", side="buy", qty=32277, price=1.71,
                  order_id="sugp-buy", signal_type="BUY", db_path=db)
        positions = [{
            "symbol": "SUGP", "current_price": 1.11,
            "avg_entry_price": 1.71, "qty": 32277, "occ_symbol": None,
        }]
        out = check_stop_loss_take_profit(positions, stop_loss_pct=0.05,
                                          db_path=db)
        assert len(out) == 1, (
            "a real sub-$2 stock 35% past its 5% stop MUST trigger a "
            "stop-loss SELL once the journal confirms it's a stock — the "
            "old blanket sub-$2 skip let SUGP crater unprotected"
        )
        assert out[0]["trigger"] == "stop_loss" and out[0]["symbol"] == "SUGP"

    def test_stranded_option_leg_still_skipped(self, tmp_path):
        """The 2026-05-11 bug shape: a sub-$2 position with NO stock BUY
        in the journal (only an option row) is a stranded option leg and
        must still be skipped — never fire a stock SELL on it."""
        from journal import init_db, log_trade
        from portfolio_manager import check_stop_loss_take_profit
        db = str(tmp_path / "p.db")
        init_db(db)
        # only an OPTIONS row exists for this symbol, no stock buy
        log_trade(symbol="ZZZ", side="buy", qty=2, price=0.20,
                  order_id="opt-1", signal_type="OPTIONS",
                  occ_symbol="ZZZ260101C00010000", db_path=db)
        positions = [{
            "symbol": "ZZZ", "current_price": 0.16,
            "avg_entry_price": 0.20, "qty": 2, "occ_symbol": None,
        }]
        out = check_stop_loss_take_profit(positions, stop_loss_pct=0.05,
                                          db_path=db)
        assert out == [], (
            "a sub-$2 position with no journaled stock BUY is a stranded "
            "option leg — must NOT fire a stock stop-loss (2026-05-11)"
        )

    def test_option_position_still_skipped(self):
        """An actual option (occ_symbol set) must still be skipped by
        the stock stop-loss — options have their own exit path."""
        from portfolio_manager import check_stop_loss_take_profit
        positions = [{
            "symbol": "SMR", "current_price": 0.24,
            "avg_entry_price": 0.59, "qty": 2,
            "occ_symbol": "SMR260724C00012000",
        }]
        out = check_stop_loss_take_profit(positions, stop_loss_pct=0.05)
        assert out == [], "option legs must not be stock-stop-lossed"

    def test_sub2_guard_is_journal_gated_in_source(self):
        """The sub-$2 skip must be gated on a journal stock-holding
        check, not a blanket price heuristic."""
        src = (REPO / "portfolio_manager.py").read_text()
        assert "_is_real_stock_holding" in src
        idx = src.find("suspected option\n            # leg")
        # the guard's condition must reference the journal check
        guard = src[src.find("sub-$2 \"suspected option leg\" guard"):
                    src.find("sub-$2 \"suspected option leg\" guard") + 1400]
        assert "_is_real_stock_holding(db_path, symbol)" in guard, (
            "the sub-$2 skip must consult the journal (a real stock "
            "holding fires the stop; only a stranded option leg is "
            "skipped)"
        )


# ---------------------------------------------------------------------------
# Fix 3 — stale-cancel excludes protective orders
# ---------------------------------------------------------------------------


def _db_with(tmp_path, rows):
    from journal import init_db
    db = str(tmp_path / "p.db")
    init_db(db)
    with closing(sqlite3.connect(db)) as c:
        for cols in rows:
            keys = ", ".join(cols)
            qs = ", ".join(["?"] * len(cols))
            c.execute("INSERT INTO trades (%s) VALUES (%s)" % (keys, qs),
                      list(cols.values()))
        c.commit()
    return db


class TestProtectiveExcludedFromStaleCancel:

    def test_own_protective_order_ids_collects_bracket_children(self, tmp_path):
        from order_guard import own_protective_order_ids
        db = _db_with(tmp_path, [
            {"symbol": "AAA", "side": "buy", "qty": 100, "price": 10,
             "order_id": "entry-1", "status": "open",
             "protective_stop_order_id": "stop-1",
             "protective_tp_order_id": "tp-1"},
            {"symbol": "AAA", "side": "sell", "qty": 100, "price": 11,
             "order_id": "prot-row-1", "status": "pending_protective",
             "signal_type": "PROTECTIVE_STOP"},
        ])
        prot = own_protective_order_ids(db)
        assert "stop-1" in prot and "tp-1" in prot
        assert "prot-row-1" in prot
        assert "entry-1" not in prot, (
            "the ENTRY order must NOT be classed protective — stale "
            "unfilled entries are still cancelable"
        )

    def test_stale_cancel_spares_bracket_take_profit(self, tmp_path):
        """A bracket TP (limit, >5min old, stamped on the entry) must
        NOT be canceled by the stale-limit sweep — that strips
        protection and OCO-cancels the stop too."""
        import multi_scheduler
        import client
        db = _db_with(tmp_path, [
            {"symbol": "AAA", "side": "buy", "qty": 100, "price": 10,
             "order_id": "entry-1", "status": "open",
             "protective_tp_order_id": "tp-limit-1"},
        ])
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        tp = MagicMock(); tp.id = "tp-limit-1"; tp.symbol = "AAA"
        tp.side = "sell"; tp.type = "limit"; tp.created_at = old
        api = MagicMock(); api.list_orders.return_value = [tp]
        ctx = MagicMock(); ctx.use_limit_orders = True; ctx.db_path = db
        ctx.display_name = "X"; ctx.segment = "s"; ctx.profile_id = 1
        ctx.user_id = 1
        with patch.object(client, "get_api", return_value=api), \
             patch.object(multi_scheduler, "_safe_log_activity"):
            multi_scheduler._task_cancel_stale_orders(ctx)
        api.cancel_order.assert_not_called(), (
            "the stale-limit sweep canceled a protective take-profit — "
            "this strips the bracket and leaves the position naked"
        )

    def test_stale_cancel_still_cancels_unfilled_entry_limit(self, tmp_path):
        """Sanity: a genuinely stale ENTRY limit (not protective) is
        still canceled — the task keeps doing its real job."""
        import multi_scheduler
        import client
        db = _db_with(tmp_path, [
            {"symbol": "BBB", "side": "buy", "qty": 50, "price": 9,
             "order_id": "entry-stale", "status": "open"},
        ])
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        eo = MagicMock(); eo.id = "entry-stale"; eo.symbol = "BBB"
        eo.side = "buy"; eo.type = "limit"; eo.created_at = old
        api = MagicMock(); api.list_orders.return_value = [eo]
        ctx = MagicMock(); ctx.use_limit_orders = True; ctx.db_path = db
        ctx.display_name = "X"; ctx.segment = "s"; ctx.profile_id = 1
        ctx.user_id = 1
        with patch.object(client, "get_api", return_value=api), \
             patch.object(multi_scheduler, "_safe_log_activity"):
            multi_scheduler._task_cancel_stale_orders(ctx)
        cancelled = [c.args[0] for c in api.cancel_order.call_args_list]
        assert cancelled == ["entry-stale"]
