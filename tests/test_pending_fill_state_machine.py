"""The 2026-05-07 refactor: SELL/COVER rows write status='pending_fill'
on submit instead of 'closed'. _task_update_fills flips
'pending_fill' → 'closed' once Alpaca confirms the fill via
filled_avg_price, and at the same time flips matching open BUY/SHORT
rows to 'closed'.

Why: the previous immediate 'closed' write created a phantom-SELL
window between submit and the next reconcile cycle (~15 min). If
Alpaca async-canceled the close (wash trade, off-hours, etc.), the
journal claimed a SELL that never happened. The deferred
state-machine ties the journal's "closed" claim to broker
confirmation, eliminating the phantom window.

Key invariants:
- A SELL row in 'pending_fill' has the same FIFO effect as 'closed'
  (FIFO doesn't filter on status except 'canceled') so the position
  book stays correct.
- Confirmation flow: api.get_order(...).filled_avg_price populates
  → row flips 'pending_fill' → 'closed' AND matching BUYs flip
  'open' → 'closed'.
- Phantom flow (broker async-cancels): reconcile detects, undoes
  the pending_fill row + reopens the BUYs. (Tested separately in
  test_reconcile_journal_to_broker.py.)
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


_TRADES_DDL = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    symbol TEXT, side TEXT, qty REAL, price REAL,
    order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
    ai_reasoning TEXT, ai_confidence REAL,
    stop_loss REAL, take_profit REAL,
    status TEXT DEFAULT 'open', pnl REAL,
    decision_price REAL, fill_price REAL, slippage_pct REAL,
    occ_symbol TEXT, option_strategy TEXT, expiry TEXT, strike REAL,
    predicted_slippage_bps REAL, adv_at_decision REAL
)
"""


def _new_db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute(_TRADES_DDL)
    conn.commit()
    conn.close()
    return f.name


def _insert(db, **kw):
    cols = ", ".join(kw.keys())
    placeholders = ", ".join(["?"] * len(kw))
    conn = sqlite3.connect(db)
    cur = conn.execute(
        f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
        tuple(kw.values()),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def _row(db, rid):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM trades WHERE id=?", (rid,)).fetchone()
    conn.close()
    return r


class _FakeCtx:
    def __init__(self, db_path):
        self.db_path = db_path
        self.display_name = "Test"
        self.segment = "test"


def _run_update_fills(db, fake_api):
    import client
    import multi_scheduler as ms
    prev = client.get_api
    client.get_api = lambda ctx: fake_api
    try:
        ms._task_update_fills(_FakeCtx(db))
    finally:
        client.get_api = prev


class TestPendingFillTransitions(unittest.TestCase):

    def setUp(self):
        self.db = _new_db()

    def tearDown(self):
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_confirmed_sell_flips_to_closed_and_closes_buys(self):
        """The happy path: SELL was logged 'pending_fill' on submit;
        broker confirms fill via filled_avg_price; update_fills flips
        the SELL to 'closed' AND flips the matching open BUY rows."""
        # Pre-existing open BUY for this symbol
        buy_id = _insert(
            self.db, symbol="AAPL", side="buy", qty=10,
            price=200.0, order_id="entry-1",
            signal_type="BUY", status="open",
        )
        # SELL row in pending_fill with order_id wired
        sell_id = _insert(
            self.db, symbol="AAPL", side="sell", qty=10,
            price=205.0, decision_price=205.0,
            order_id="exit-1", pnl=50.0,
            signal_type="SELL", status="pending_fill",
        )

        fake_api = MagicMock()
        # Broker confirms the close fill
        order = MagicMock()
        order.filled_avg_price = "204.95"
        fake_api.get_order.return_value = order

        _run_update_fills(self.db, fake_api)

        sell = _row(self.db, sell_id)
        buy = _row(self.db, buy_id)
        self.assertEqual(sell["status"], "closed")
        self.assertEqual(sell["fill_price"], 204.95)
        # BUY flipped to closed too
        self.assertEqual(buy["status"], "closed")

    def test_unconfirmed_sell_stays_pending_buys_stay_open(self):
        """If broker hasn't reported fill yet (filled_avg_price=None),
        the SELL row stays 'pending_fill' and matching BUY rows stay
        'open'. Confirms the deferral semantics — we don't claim the
        position is closed until broker confirms."""
        buy_id = _insert(
            self.db, symbol="AAPL", side="buy", qty=10,
            price=200.0, order_id="entry-1",
            signal_type="BUY", status="open",
        )
        sell_id = _insert(
            self.db, symbol="AAPL", side="sell", qty=10,
            price=205.0, decision_price=205.0,
            order_id="exit-pending", pnl=50.0,
            signal_type="SELL", status="pending_fill",
        )

        fake_api = MagicMock()
        order = MagicMock()
        order.filled_avg_price = None  # broker hasn't reported yet
        fake_api.get_order.return_value = order

        _run_update_fills(self.db, fake_api)

        self.assertEqual(_row(self.db, sell_id)["status"], "pending_fill")
        self.assertEqual(_row(self.db, buy_id)["status"], "open")

    def test_confirmed_cover_flips_to_closed_and_closes_shorts(self):
        """Symmetric for shorts: COVER was logged 'pending_fill';
        broker confirms; update_fills flips COVER to 'closed' and
        flips matching SHORT rows."""
        short_id = _insert(
            self.db, symbol="TSLA", side="short", qty=5,
            price=300.0, order_id="entry-short-1",
            signal_type="SHORT", status="open",
        )
        cover_id = _insert(
            self.db, symbol="TSLA", side="cover", qty=5,
            price=290.0, decision_price=290.0,
            order_id="cover-1", pnl=50.0,
            signal_type="SELL", status="pending_fill",
        )

        fake_api = MagicMock()
        order = MagicMock()
        order.filled_avg_price = "290.50"
        fake_api.get_order.return_value = order

        _run_update_fills(self.db, fake_api)

        self.assertEqual(_row(self.db, cover_id)["status"], "closed")
        self.assertEqual(_row(self.db, short_id)["status"], "closed")

    def test_pending_fill_option_close_flips_to_closed_no_opp_side(self):
        """Roll-manager auto-close path: a credit option position
        was flipped to 'pending_fill' with the close order_id. Once
        broker confirms, update_fills flips to 'closed'. There's no
        opposite-side row to flip (option closes stand alone)."""
        rid = _insert(
            self.db, symbol="AAPL", side="buy", qty=1,
            price=2.00, decision_price=2.00,
            order_id="opt-close-1", pnl=170.0,
            signal_type="OPTIONS", strategy="cash_secured_put",
            occ_symbol="AAPL  990501P00150000",
            status="pending_fill",
        )

        fake_api = MagicMock()
        order = MagicMock()
        order.filled_avg_price = "0.30"
        fake_api.get_order.return_value = order

        _run_update_fills(self.db, fake_api)

        self.assertEqual(_row(self.db, rid)["status"], "closed")
        self.assertEqual(_row(self.db, rid)["fill_price"], 0.30)


class TestFIFOTreatsPendingFillAsClosed(unittest.TestCase):
    """FIFO get_virtual_positions filters only on
    `status != 'canceled'`. A 'pending_fill' SELL is included → the
    BUY's lot is consumed → the position correctly shows as flat
    even before confirmation. Verify."""

    def test_pending_fill_sell_consumes_buy_lot_in_fifo(self):
        from journal import get_virtual_positions

        db = _new_db()
        try:
            _insert(
                db, symbol="AAPL", side="buy", qty=10,
                price=200.0, status="open",
            )
            _insert(
                db, symbol="AAPL", side="sell", qty=10,
                price=205.0, status="pending_fill", pnl=50.0,
            )

            positions = get_virtual_positions(
                db_path=db, price_fetcher=lambda s: 200.0,
            )
            # Long 10 - sell 10 = flat. Position book should NOT
            # show AAPL.
            aapl = [p for p in positions if p.get("symbol") == "AAPL"]
            self.assertEqual(aapl, [],
                "FIFO should treat 'pending_fill' SELL same as 'closed' "
                "and consume the BUY lot. Otherwise the dashboard would "
                "show the position as still held during the pending "
                "window.")
        finally:
            try:
                os.unlink(db)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
