"""Regression tests for `_task_update_fills` covering multileg legs.

The May 6 fix for "$--" multileg legs added an immediate-fetch in
`_log_strategy_legs`, but Alpaca paper accounts don't fill within
the same microsecond as submit, so `filled_avg_price` is always None
at log time. The task `_task_update_fills` is the catch-up — but it
was filtering on `decision_price IS NOT NULL`, which excluded every
multileg leg (decision_price isn't set for option legs because no
quote is available cheaply at submit time).

These tests pin three behaviors after the 2026-05-07 fix:

1. Multileg legs (decision_price NULL) ARE picked up by the catch-up.
2. Stock entries (decision_price NOT NULL) still get slippage.
3. `price` is populated alongside `fill_price` so the dashboard's
   `t.price` cell stops showing "$--".
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


def _insert_trade(db, **kw):
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
        self.display_name = "Test Profile"
        self.segment = "test"


class TestUpdateFillsMultileg(unittest.TestCase):

    def setUp(self):
        self.db = _new_db()

    def tearDown(self):
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def _run_task(self, fake_api):
        # Patch get_api so the scheduler picks up our fake.
        import multi_scheduler as ms
        orig = ms.get_api if hasattr(ms, "get_api") else None
        # Inject by monkey-patching the module-level lookup used in the
        # task function (`from client import get_api` is local — patch
        # the source module).
        import client
        prev = client.get_api
        client.get_api = lambda ctx: fake_api
        try:
            ms._task_update_fills(_FakeCtx(self.db))
        finally:
            client.get_api = prev
            if orig is not None:
                ms.get_api = orig

    def test_multileg_leg_with_null_decision_price_is_picked_up(self):
        """Caught 2026-05-07: TECK/WMT/MSFT bull_put_spread legs sat
        with NULL price/fill_price for hours because the catch-up
        task filtered on `decision_price IS NOT NULL`. Drop the
        filter; multileg legs (which lack decision_price) MUST be
        picked up so the dashboard stops showing "$--"."""
        rid = _insert_trade(
            self.db,
            symbol="TECK", side="sell", qty=2.0,
            order_id="leg-1",
            signal_type="MULTILEG", strategy="bull_put_spread",
            occ_symbol="TECK260612P00060000",
            # price, fill_price, decision_price all NULL — exactly
            # what _log_strategy_legs writes when the immediate-fetch
            # races and fap is None at log time.
        )

        fake_api = MagicMock()
        order = MagicMock()
        order.filled_avg_price = "0.5"
        fake_api.get_order.return_value = order

        self._run_task(fake_api)

        r = _row(self.db, rid)
        self.assertEqual(r["fill_price"], 0.5)
        self.assertEqual(r["price"], 0.5,
            "price must also be populated; the dashboard reads t.price, "
            "not t.fill_price — leaving price NULL keeps showing '$--'")
        # Slippage stays NULL because there's no decision baseline.
        self.assertIsNone(r["slippage_pct"])

    def test_stock_entry_with_decision_price_still_gets_slippage(self):
        """The catch-up still computes slippage_pct when
        decision_price is set. Existing stock-trade contract is not
        regressed by the multileg loosening."""
        rid = _insert_trade(
            self.db,
            symbol="AAPL", side="buy", qty=10,
            price=200.0, decision_price=200.0,
            order_id="aapl-1",
            signal_type="BUY",
        )

        fake_api = MagicMock()
        order = MagicMock()
        order.filled_avg_price = "201.0"  # 50 bps adverse slippage
        fake_api.get_order.return_value = order

        self._run_task(fake_api)

        r = _row(self.db, rid)
        self.assertEqual(r["fill_price"], 201.0)
        # Slippage = (201 - 200) / 200 * 100 = 0.5%
        self.assertAlmostEqual(r["slippage_pct"], 0.5, places=4)
        # Existing price wasn't NULL — leave it alone.
        self.assertEqual(r["price"], 200.0)

    def test_unfilled_order_is_skipped(self):
        """When Alpaca says filled_avg_price is None (still pending),
        the row stays NULL — we'll catch it on the next cycle."""
        rid = _insert_trade(
            self.db,
            symbol="WMT", side="buy", qty=1,
            order_id="pending-1",
            signal_type="MULTILEG",
        )

        fake_api = MagicMock()
        order = MagicMock()
        order.filled_avg_price = None
        fake_api.get_order.return_value = order

        self._run_task(fake_api)

        r = _row(self.db, rid)
        self.assertIsNone(r["fill_price"])
        self.assertIsNone(r["price"])

    def test_get_order_exception_does_not_crash_task(self):
        """A transient Alpaca error on one row must not abort the
        whole batch. Other rows in the same pass should still be
        updated."""
        bad_rid = _insert_trade(
            self.db,
            symbol="X", side="buy", qty=1,
            order_id="will-throw",
            signal_type="BUY", price=10.0, decision_price=10.0,
        )
        good_rid = _insert_trade(
            self.db,
            symbol="Y", side="buy", qty=1,
            order_id="will-fill",
            signal_type="BUY", price=20.0, decision_price=20.0,
        )

        order = MagicMock()
        order.filled_avg_price = "20.10"

        def get_order_side_effect(oid):
            if oid == "will-throw":
                raise RuntimeError("boom")
            return order

        fake_api = MagicMock()
        fake_api.get_order.side_effect = get_order_side_effect

        self._run_task(fake_api)

        self.assertIsNone(_row(self.db, bad_rid)["fill_price"])
        self.assertEqual(_row(self.db, good_rid)["fill_price"], 20.10)

    def test_no_unfilled_rows_is_a_quick_noop(self):
        """When nothing needs updating, the task exits cleanly without
        calling the broker."""
        fake_api = MagicMock()
        self._run_task(fake_api)
        fake_api.get_order.assert_not_called()

    # --- 2026-05-16 audit additions ---

    def test_combo_multileg_uses_per_leg_price_not_combo_net(self):
        """Caught 2026-05-16: `_task_update_fills` was reading
        `order.filled_avg_price` for MULTILEG rows. For a COMBO
        order that's the SIGNED NET PREMIUM (negative for credit
        spreads), not per-leg prices. 7+ rows on prod ended up with
        `price=-0.64` etc., invisible to `get_virtual_positions`.
        Fix: when `order.legs[]` is non-empty, match by OCC and use
        the per-leg `filled_avg_price` (positive)."""
        # Both legs of a credit spread share the SAME combo order id.
        short_id = _insert_trade(
            self.db,
            symbol="EQT", side="sell", qty=1.0,
            order_id="combo-1",
            signal_type="MULTILEG", strategy="bull_put_spread",
            occ_symbol="EQT260618C00057500",
        )
        long_id = _insert_trade(
            self.db,
            symbol="EQT", side="buy", qty=1.0,
            order_id="combo-1",
            signal_type="MULTILEG", strategy="bull_put_spread",
            occ_symbol="EQT260618C00060000",
        )

        # Combo order: net premium is NEGATIVE (credit). Per-leg
        # filled_avg_price on `legs[]` is POSITIVE.
        combo = MagicMock()
        combo.filled_avg_price = "-0.64"   # SIGNED NET (the trap)
        short_leg = MagicMock()
        short_leg.symbol = "EQT260618C00057500"
        short_leg.filled_avg_price = "1.50"
        long_leg = MagicMock()
        long_leg.symbol = "EQT260618C00060000"
        long_leg.filled_avg_price = "0.86"
        combo.legs = [short_leg, long_leg]

        fake_api = MagicMock()
        fake_api.get_order.return_value = combo

        self._run_task(fake_api)

        self.assertEqual(_row(self.db, short_id)["fill_price"], 1.50)
        self.assertEqual(_row(self.db, short_id)["price"], 1.50)
        self.assertEqual(_row(self.db, long_id)["fill_price"], 0.86)
        self.assertEqual(_row(self.db, long_id)["price"], 0.86)
        # And: combo net must NEVER appear as a leg price.
        for rid in (short_id, long_id):
            self.assertNotEqual(_row(self.db, rid)["fill_price"], -0.64)
            self.assertNotEqual(_row(self.db, rid)["price"], -0.64)

    def test_combo_with_no_matching_leg_skips_and_does_not_write_combo_net(self):
        """Defense-in-depth: if `order.legs[]` exists but no leg
        matches our OCC (e.g. combo is for a different spread),
        skip the row — never fall back to writing the combo's
        signed net premium."""
        rid = _insert_trade(
            self.db,
            symbol="EQT", side="sell", qty=1.0,
            order_id="combo-2",
            signal_type="MULTILEG", strategy="bull_put_spread",
            occ_symbol="EQT260618C00057500",
        )
        combo = MagicMock()
        combo.filled_avg_price = "-0.64"
        wrong_leg = MagicMock()
        wrong_leg.symbol = "SOMETHING_ELSE"
        wrong_leg.filled_avg_price = "1.00"
        combo.legs = [wrong_leg]
        fake_api = MagicMock()
        fake_api.get_order.return_value = combo

        self._run_task(fake_api)

        r = _row(self.db, rid)
        self.assertIsNone(r["fill_price"])
        self.assertIsNone(r["price"])

    def test_combo_self_heals_pre_existing_negative_price_rows(self):
        """Pre-fix rows on prod have `fill_price=-0.64` (the bug).
        After the fix, the next `_task_update_fills` cycle must
        re-process these rows (they're excluded by the old
        `fill_price IS NULL` filter). The new query also matches
        `signal_type='MULTILEG' AND fill_price <= 0`."""
        rid = _insert_trade(
            self.db,
            symbol="WMT", side="sell", qty=1.0,
            order_id="combo-3",
            signal_type="MULTILEG", strategy="bull_put_spread",
            occ_symbol="WMT260618P00125000",
            price=-0.9, fill_price=-0.9,   # the bug's signature
            status="open",
        )
        combo = MagicMock()
        combo.filled_avg_price = "-0.9"
        leg = MagicMock()
        leg.symbol = "WMT260618P00125000"
        leg.filled_avg_price = "1.80"
        combo.legs = [leg]
        fake_api = MagicMock()
        fake_api.get_order.return_value = combo

        self._run_task(fake_api)

        r = _row(self.db, rid)
        self.assertEqual(r["fill_price"], 1.80)
        # `price` was non-positive (-0.9) — must also be overwritten.
        self.assertEqual(r["price"], 1.80)

    def test_sequential_multileg_uses_order_filled_avg_directly(self):
        """SEQUENTIAL path: each leg has its OWN order_id (not the
        combo's). `order.legs[]` is empty (single-leg order), so
        `order.filled_avg_price` IS the per-leg price. Don't break
        the sequential path with the combo fix."""
        rid = _insert_trade(
            self.db,
            symbol="TECK", side="sell", qty=2.0,
            order_id="seq-leg-1",
            signal_type="MULTILEG", strategy="bull_put_spread",
            occ_symbol="TECK260612P00060000",
        )
        order = MagicMock()
        order.filled_avg_price = "0.50"
        order.legs = []   # single-leg order: no legs[]
        fake_api = MagicMock()
        fake_api.get_order.return_value = order

        self._run_task(fake_api)

        r = _row(self.db, rid)
        self.assertEqual(r["fill_price"], 0.50)
        self.assertEqual(r["price"], 0.50)

    def test_refuses_to_write_non_positive_price_on_any_row(self):
        """Defense-in-depth: even a STOCK row whose broker reply
        somehow has a non-positive `filled_avg_price` must NOT be
        written. Leave NULL for the next cycle."""
        rid = _insert_trade(
            self.db,
            symbol="AAPL", side="buy", qty=10,
            order_id="aapl-bad",
            signal_type="BUY",
        )
        order = MagicMock()
        order.filled_avg_price = "0.0"
        fake_api = MagicMock()
        fake_api.get_order.return_value = order

        self._run_task(fake_api)

        r = _row(self.db, rid)
        self.assertIsNone(r["fill_price"])
        self.assertIsNone(r["price"])


if __name__ == "__main__":
    unittest.main()
