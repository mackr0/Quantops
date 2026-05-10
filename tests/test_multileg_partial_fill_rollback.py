"""Pin the multileg partial-fill rollback in `_task_update_fills`.

Caught 2026-05-10: three half-filled multileg spreads sat on prod
for 2 days (CWAN, BKLN on profiles 6 + 7). Each was a 2-leg spread
where the BUY leg filled but the SELL leg expired unfilled — leaving
the AI with a naked long position it never decided to take.

Root cause: `execute_multileg_strategy`'s sequential fallback (used
when Alpaca's MLEG combo path 500s) submits each leg, returns
success the moment all submit calls return without exception, and
has rollback ONLY for submit-failure (immediate exception). It has
no logic for fill-failure (one leg later expires while the partner
fills). `_task_update_fills` previously skipped expired/canceled
orders silently (`if not order.filled_avg_price: continue`), so the
naked position was never detected.

This test pins:

1. `_task_update_fills` marks rows status='expired' / 'canceled' /
   'rejected' when the broker confirms terminal-unfilled state
   (instead of silently skipping forever).
2. The pre-existing regular fill backfill path still works.
3. When a MULTILEG leg ends terminal-unfilled and a partner leg
   has already filled (status='open' + fill_price set), the
   partner is auto-closed via opposite-side market order, the close
   is logged as a new trade row, and the original entry row is
   flipped to status='closed'.
4. Pairing rule rejects siblings that don't share `option_strategy`
   (different combo), don't share underlying `symbol`, or are
   outside the 60-second timestamp window (different submission
   batch).
5. Already-marked terminal rows are excluded from the SELECT so we
   don't re-poll Alpaca for the same expired order forever.
"""

import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_journal_db():
    """Create a temp journal DB with the trades table schema. Use the
    real init_db so column set matches production exactly."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from journal import init_db
    init_db(path)
    return path


def _insert_trade(db_path, **kwargs):
    """Insert a row and return its id. Defaults reflect a typical
    multileg leg row."""
    defaults = {
        "timestamp": "2026-05-08T18:54:05",
        "symbol": "CWAN",
        "side": "buy",
        "qty": 3,
        "price": None,
        "fill_price": None,
        "order_id": "order-default",
        "signal_type": "MULTILEG",
        "strategy": "bull_call_spread",
        "reason": "test",
        "status": "open",
        "occ_symbol": "CWAN260612C00026000",
        "option_strategy": "bull_call_spread",
        "decision_price": None,
    }
    defaults.update(kwargs)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(["?"] * len(defaults))
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
        tuple(defaults.values()),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def _make_ctx(db_path):
    ctx = MagicMock()
    ctx.db_path = db_path
    ctx.display_name = "TestProfile"
    ctx.segment = "test"
    return ctx


def _mock_order(status, filled_qty=0, filled_avg_price=None):
    o = MagicMock()
    o.status = status
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    return o


class TestTerminalUnfilledStatusPinning:
    def test_expired_unfilled_order_marks_row_expired(self):
        """When Alpaca says order expired with filled_qty=0, the
        journal row must update to status='expired' and price=0."""
        db = _make_journal_db()
        try:
            rid = _insert_trade(
                db, side="sell", order_id="exp-1", price=None,
                fill_price=None, status="open",
            )

            from multi_scheduler import _task_update_fills
            with patch("client.get_api") as get_api:
                api = MagicMock()
                api.get_order.return_value = _mock_order(
                    "expired", filled_qty=0, filled_avg_price=None,
                )
                get_api.return_value = api
                _task_update_fills(_make_ctx(db))

            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT status, price FROM trades WHERE id=?", (rid,),
            ).fetchone()
            conn.close()
            assert row[0] == "expired"
            assert row[1] == 0
        finally:
            os.unlink(db)

    def test_canceled_unfilled_order_marks_row_canceled(self):
        db = _make_journal_db()
        try:
            rid = _insert_trade(
                db, side="sell", order_id="can-1", price=None,
                fill_price=None, status="open",
            )

            from multi_scheduler import _task_update_fills
            with patch("client.get_api") as get_api:
                api = MagicMock()
                api.get_order.return_value = _mock_order(
                    "canceled", filled_qty=0,
                )
                get_api.return_value = api
                _task_update_fills(_make_ctx(db))

            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT status FROM trades WHERE id=?", (rid,),
            ).fetchone()
            conn.close()
            assert row[0] == "canceled"
        finally:
            os.unlink(db)

    def test_rejected_unfilled_order_marks_row_rejected(self):
        db = _make_journal_db()
        try:
            rid = _insert_trade(db, order_id="rej-1", status="open")
            from multi_scheduler import _task_update_fills
            with patch("client.get_api") as get_api:
                api = MagicMock()
                api.get_order.return_value = _mock_order(
                    "rejected", filled_qty=0,
                )
                get_api.return_value = api
                _task_update_fills(_make_ctx(db))

            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT status FROM trades WHERE id=?", (rid,),
            ).fetchone()
            conn.close()
            assert row[0] == "rejected"
        finally:
            os.unlink(db)

    def test_filled_order_still_backfills_price(self):
        """Regression: the existing fill backfill path must keep
        working — terminal-status detection only intercepts orders
        with filled_qty=0, not normal fills."""
        db = _make_journal_db()
        try:
            rid = _insert_trade(
                db, side="buy", order_id="fil-1", price=None,
                fill_price=None, status="open",
                decision_price=4.80,
            )
            from multi_scheduler import _task_update_fills
            with patch("client.get_api") as get_api:
                api = MagicMock()
                api.get_order.return_value = _mock_order(
                    "filled", filled_qty=3, filled_avg_price=4.85,
                )
                get_api.return_value = api
                _task_update_fills(_make_ctx(db))

            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT status, price, fill_price, slippage_pct "
                "FROM trades WHERE id=?", (rid,),
            ).fetchone()
            conn.close()
            assert row[0] == "open"  # not flipped
            assert row[1] == 4.85
            assert row[2] == 4.85
            assert row[3] is not None
        finally:
            os.unlink(db)

    def test_already_marked_terminal_row_not_repolled(self):
        """A row already at status='expired' must not be re-fetched
        from Alpaca — otherwise we'd hit the API forever for every
        old expired order."""
        db = _make_journal_db()
        try:
            _insert_trade(
                db, order_id="old-exp", status="expired",
                price=0, fill_price=None,
            )
            from multi_scheduler import _task_update_fills
            with patch("client.get_api") as get_api:
                api = MagicMock()
                # If get_order is called, the test fails — the WHERE
                # filter should have excluded this row.
                api.get_order.side_effect = AssertionError(
                    "Should not have polled an already-terminal row"
                )
                get_api.return_value = api
                # Must complete without raising
                _task_update_fills(_make_ctx(db))
        finally:
            os.unlink(db)


class TestMultilegPartialFillRollback:
    def test_orphan_filled_partner_is_auto_closed(self):
        """The exact prod CWAN case: BUY leg filled (status='open',
        price=4.80), SELL leg later expires unfilled. Running
        update_fills must auto-close the BUY leg via opposite-side
        market order, log the close as a new row, flip the BUY row
        to status='closed'."""
        db = _make_journal_db()
        try:
            buy_id = _insert_trade(
                db, timestamp="2026-05-08T18:54:05.582409",
                symbol="CWAN", side="buy", qty=3,
                price=4.80, fill_price=4.80, status="open",
                order_id="buy-leg",
                occ_symbol="CWAN260612C00026000",
                option_strategy="bull_call_spread",
                ai_confidence=78,
                ai_reasoning="momentum + cheap IV",
            )
            sell_id = _insert_trade(
                db, timestamp="2026-05-08T18:54:05.610395",
                symbol="CWAN", side="sell", qty=3,
                price=None, fill_price=None, status="open",
                order_id="sell-leg",
                occ_symbol="CWAN260612C00027000",
                option_strategy="bull_call_spread",
                ai_confidence=78,
                ai_reasoning="momentum + cheap IV",
            )

            close_order_obj = MagicMock()
            close_order_obj.id = "rollback-close-1"

            from multi_scheduler import _task_update_fills
            with patch("client.get_api") as get_api:
                api = MagicMock()

                def get_order_side_effect(oid):
                    if oid == "sell-leg":
                        return _mock_order(
                            "expired", filled_qty=0,
                            filled_avg_price=None,
                        )
                    # buy-leg already has fill_price so won't be
                    # re-polled (price IS NOT NULL in WHERE)
                    return _mock_order(
                        "filled", filled_qty=3, filled_avg_price=4.80,
                    )
                api.get_order.side_effect = get_order_side_effect
                api.submit_order.return_value = close_order_obj
                get_api.return_value = api
                _task_update_fills(_make_ctx(db))

                # api.submit_order was called with opposite-side
                # market close on the BUY leg's OCC
                assert api.submit_order.called, (
                    "Rollback close was not submitted"
                )
                close_kwargs = api.submit_order.call_args.kwargs
                assert close_kwargs["symbol"] == "CWAN260612C00026000"
                assert close_kwargs["qty"] == 3
                assert close_kwargs["side"] == "sell"  # opposite of buy
                assert close_kwargs["type"] == "market"

            conn = sqlite3.connect(db)
            # Original BUY row: now closed
            buy_row = conn.execute(
                "SELECT status FROM trades WHERE id=?", (buy_id,),
            ).fetchone()
            assert buy_row[0] == "closed"

            # Original SELL row: marked expired
            sell_row = conn.execute(
                "SELECT status FROM trades WHERE id=?", (sell_id,),
            ).fetchone()
            assert sell_row[0] == "expired"

            # New rollback close row exists, with rollback metadata
            rb_row = conn.execute(
                "SELECT side, qty, occ_symbol, signal_type, "
                "       option_strategy, reason, ai_confidence "
                "FROM trades WHERE order_id=?",
                ("rollback-close-1",),
            ).fetchone()
            conn.close()
            assert rb_row is not None, "Rollback close row not logged"
            assert rb_row[0] == "sell"
            assert rb_row[1] == 3
            assert rb_row[2] == "CWAN260612C00026000"
            assert rb_row[3] == "MULTILEG"
            assert rb_row[4] == "bull_call_spread"
            assert "Auto-rollback" in (rb_row[5] or "")
            assert rb_row[6] == 78  # AI confidence carried over
        finally:
            os.unlink(db)

    def test_unfilled_sibling_not_closed(self):
        """If both legs ended terminal-unfilled, neither needs
        closing — there's no orphan position. submit_order must
        NOT be called."""
        db = _make_journal_db()
        try:
            _insert_trade(
                db, timestamp="2026-05-08T18:54:05.582",
                side="buy", price=None, fill_price=None,
                order_id="buy-leg", status="open",
                occ_symbol="CWAN260612C00026000",
                option_strategy="bull_call_spread",
            )
            _insert_trade(
                db, timestamp="2026-05-08T18:54:05.610",
                side="sell", price=None, fill_price=None,
                order_id="sell-leg", status="open",
                occ_symbol="CWAN260612C00027000",
                option_strategy="bull_call_spread",
            )

            from multi_scheduler import _task_update_fills
            with patch("client.get_api") as get_api:
                api = MagicMock()
                api.get_order.return_value = _mock_order(
                    "expired", filled_qty=0,
                )
                get_api.return_value = api
                _task_update_fills(_make_ctx(db))

                assert not api.submit_order.called, (
                    "Rollback fired even though no leg ever filled — "
                    "there was no orphan to close."
                )
        finally:
            os.unlink(db)

    def test_different_option_strategy_not_paired(self):
        """A SELL row from `bear_put_spread` must not be considered
        a partner of a BUY row from `bull_call_spread` even on the
        same symbol within the time window."""
        db = _make_journal_db()
        try:
            _insert_trade(
                db, timestamp="2026-05-08T18:54:05.582",
                side="buy", price=4.80, fill_price=4.80,
                order_id="bull-leg", status="open",
                occ_symbol="CWAN260612C00026000",
                option_strategy="bull_call_spread",
            )
            _insert_trade(
                db, timestamp="2026-05-08T18:54:05.610",
                side="sell", price=None, fill_price=None,
                order_id="bear-leg", status="open",
                occ_symbol="CWAN260612P00023000",
                option_strategy="bear_put_spread",  # different combo
            )

            from multi_scheduler import _task_update_fills
            with patch("client.get_api") as get_api:
                api = MagicMock()
                api.get_order.return_value = _mock_order(
                    "expired", filled_qty=0,
                )
                get_api.return_value = api
                _task_update_fills(_make_ctx(db))

                assert not api.submit_order.called, (
                    "Rollback paired legs across different combos "
                    "(bull_call vs bear_put) — pairing rule broken."
                )
        finally:
            os.unlink(db)

    def test_outside_timestamp_window_not_paired(self):
        """A filled BUY from yesterday must not be paired with an
        expired SELL today — different submission batches."""
        db = _make_journal_db()
        try:
            _insert_trade(
                db, timestamp="2026-05-07T10:00:00",  # yesterday
                side="buy", price=4.80, fill_price=4.80,
                order_id="old-buy", status="open",
                occ_symbol="CWAN260612C00026000",
                option_strategy="bull_call_spread",
            )
            _insert_trade(
                db, timestamp="2026-05-08T18:54:05",  # today
                side="sell", price=None, fill_price=None,
                order_id="new-sell", status="open",
                occ_symbol="CWAN260612C00027000",
                option_strategy="bull_call_spread",
            )

            from multi_scheduler import _task_update_fills
            with patch("client.get_api") as get_api:
                api = MagicMock()
                api.get_order.return_value = _mock_order(
                    "expired", filled_qty=0,
                )
                get_api.return_value = api
                _task_update_fills(_make_ctx(db))

                assert not api.submit_order.called, (
                    "Rollback paired legs from different submission "
                    "batches (>60s apart) — timestamp window broken."
                )
        finally:
            os.unlink(db)

    def test_different_underlying_not_paired(self):
        """An expired BKLN SELL must not roll back a CWAN BUY even
        in the same combo type within the time window."""
        db = _make_journal_db()
        try:
            _insert_trade(
                db, timestamp="2026-05-08T18:54:05.582",
                symbol="CWAN", side="buy", price=4.80, fill_price=4.80,
                order_id="cwan-buy", status="open",
                occ_symbol="CWAN260612C00026000",
                option_strategy="bull_call_spread",
            )
            _insert_trade(
                db, timestamp="2026-05-08T18:54:05.610",
                symbol="BKLN", side="sell", price=None, fill_price=None,
                order_id="bkln-sell", status="open",
                occ_symbol="BKLN260612P00019500",
                option_strategy="bull_call_spread",  # same strategy name
            )

            from multi_scheduler import _task_update_fills
            with patch("client.get_api") as get_api:
                api = MagicMock()
                api.get_order.return_value = _mock_order(
                    "expired", filled_qty=0,
                )
                get_api.return_value = api
                _task_update_fills(_make_ctx(db))

                assert not api.submit_order.called, (
                    "Rollback paired legs across different "
                    "underlyings — symbol filter broken."
                )
        finally:
            os.unlink(db)
