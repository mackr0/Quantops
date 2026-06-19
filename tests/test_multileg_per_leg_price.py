"""Pin per-leg price extraction in `_record_multileg_legs` for combo
path. Caught 2026-05-11: combo path was writing the combo's signed
NET premium as the price on every leg (negative number for credit
spreads), which made `get_virtual_positions` silently drop the rows
via `if price <= 0: continue`. 10+ multileg legs invisible to the
AI's portfolio context across 4 profiles.

This test pins:

1. Combo path: per-leg price comes from `combo_order.legs[i].
   filled_avg_price`, matched by OCC symbol. Each leg gets its own
   POSITIVE premium, never the combo's signed net.
2. Sequential path: per-leg price comes from each leg's own
   `api.get_order(leg_id).filled_avg_price` (existing behavior
   preserved when `order_id != combo_order_id`).
3. Defense-in-depth: if upstream still produces a non-positive leg
   price, `_record_multileg_legs` writes NULL instead of letting it
   land as the per-leg price (so `_task_update_fills` can backfill).
4. `get_virtual_positions` logs a WARNING when it skips rows due to
   non-positive price (no silent failure — same shape Issue 9 spent
   the day eradicating).
"""

import logging
import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from journal import init_db
    init_db(path)
    return path


def _strategy_2leg_credit_spread():
    """Simulate a bull put spread: short higher-strike put, long
    lower-strike put. Net credit (we receive money)."""
    from options_multileg import OptionStrategy, OptionLeg
    legs = [
        OptionLeg(
            occ_symbol="RTX260618P00170000",  # short higher-strike P
            underlying="RTX",
            expiry="2026-06-18",
            strike=170.0,
            right="P",
            side="sell",
            qty=1,
        ),
        OptionLeg(
            occ_symbol="RTX260618P00160000",  # long lower-strike P
            underlying="RTX",
            expiry="2026-06-18",
            strike=160.0,
            right="P",
            side="buy",
            qty=1,
        ),
    ]
    return OptionStrategy(
        name="bull_put_spread", underlying="RTX",
        expiry="2026-06-18", legs=legs, qty=1,
        spread_width_points=10.0, is_credit=True,
        thesis="test",
    )


class TestComboPathPerLegPrice:
    def test_combo_per_leg_prices_extracted_not_combo_net(self):
        """The exact prod bug: combo's filled_avg_price is the SIGNED
        NET premium (-1.41 for a credit spread). Per-leg prices live
        on combo_order.legs[i].filled_avg_price. Each leg row must
        carry its OWN positive per-leg price."""
        db = _make_db()
        try:
            from options_multileg import _log_strategy_legs
            strategy = _strategy_2leg_credit_spread()

            # Combo order from Alpaca — net premium is NEGATIVE (credit)
            combo_order = MagicMock()
            combo_order.filled_avg_price = -1.41  # the bug input
            short_leg = MagicMock()
            short_leg.symbol = "RTX260618P00170000"
            short_leg.filled_avg_price = 3.15  # positive — actual fill
            long_leg = MagicMock()
            long_leg.symbol = "RTX260618P00160000"
            long_leg.filled_avg_price = 1.74  # positive — actual fill
            combo_order.legs = [short_leg, long_leg]

            api = MagicMock()
            api.get_order.return_value = combo_order

            ctx = MagicMock()
            ctx.db_path = db

            _log_strategy_legs(
                strategy, "combo-1", ctx, api=api,
                ai_confidence=78,
            )

            # Read back what got written
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT side, occ_symbol, price, fill_price "
                "FROM trades WHERE signal_type='MULTILEG' "
                "ORDER BY id"
            ).fetchall()
            conn.close()

            assert len(rows) == 2

            short = next(r for r in rows
                         if r["occ_symbol"] == "RTX260618P00170000")
            long_ = next(r for r in rows
                         if r["occ_symbol"] == "RTX260618P00160000")

            # Each leg gets its OWN positive per-leg price
            assert short["price"] == pytest.approx(3.15), (
                f"short leg price should be 3.15, got {short['price']} "
                "(if -1.41, the combo-net bug is back)"
            )
            assert long_["price"] == pytest.approx(1.74)
            # fill_price mirrors price
            assert short["fill_price"] == pytest.approx(3.15)
            assert long_["fill_price"] == pytest.approx(1.74)
        finally:
            os.unlink(db)

    def test_sequential_path_uses_each_leg_order_id(self):
        """When each leg has its OWN order_id (sequential fallback),
        per-leg price still comes from that leg's order, not the
        combo. Existing behavior preserved."""
        db = _make_db()
        try:
            from options_multileg import _log_strategy_legs
            strategy = _strategy_2leg_credit_spread()

            api = MagicMock()

            def get_order(oid):
                o = MagicMock()
                if oid == "leg-short":
                    o.filled_avg_price = 3.15
                elif oid == "leg-long":
                    o.filled_avg_price = 1.74
                else:
                    o.filled_avg_price = None
                o.legs = []
                return o
            api.get_order.side_effect = get_order

            ctx = MagicMock()
            ctx.db_path = db

            # Sequential path: combo_order_id=None, leg_order_ids set
            _log_strategy_legs(
                strategy, None, ctx,
                leg_order_ids=["leg-short", "leg-long"],
                api=api,
            )

            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT occ_symbol, price FROM trades "
                "ORDER BY id"
            ).fetchall()
            conn.close()

            short = next(r for r in rows
                         if r["occ_symbol"] == "RTX260618P00170000")
            long_ = next(r for r in rows
                         if r["occ_symbol"] == "RTX260618P00160000")
            assert short["price"] == pytest.approx(3.15)
            assert long_["price"] == pytest.approx(1.74)
        finally:
            os.unlink(db)

    def test_defense_in_depth_refuses_negative_leg_price(self):
        """If something upstream still produces a negative per-leg
        price (e.g., combo legs[] returns a negative), the row gets
        NULL instead — keeps the position invisible but recoverable
        via _task_update_fills, plus a WARNING log so we know."""
        db = _make_db()
        try:
            from options_multileg import _log_strategy_legs
            strategy = _strategy_2leg_credit_spread()

            combo_order = MagicMock()
            combo_order.filled_avg_price = None
            bad_leg_a = MagicMock()
            bad_leg_a.symbol = "RTX260618P00170000"
            bad_leg_a.filled_avg_price = -1.41  # bad
            bad_leg_b = MagicMock()
            bad_leg_b.symbol = "RTX260618P00160000"
            bad_leg_b.filled_avg_price = -1.41  # bad
            combo_order.legs = [bad_leg_a, bad_leg_b]

            api = MagicMock()
            api.get_order.return_value = combo_order

            ctx = MagicMock()
            ctx.db_path = db

            with patch.object(
                __import__("options_multileg").logger, "warning"
            ) as warn:
                _log_strategy_legs(strategy, "combo-1", ctx, api=api)
                # Both legs should have triggered the refuse-and-warn
                # path (negative price refused, NULL written).
                assert warn.call_count >= 2

            conn = sqlite3.connect(db)
            rows = conn.execute(
                "SELECT price, fill_price FROM trades"
            ).fetchall()
            conn.close()

            # All legs got NULL price (not the bad negative number)
            assert all(r[0] is None for r in rows), (
                f"Expected NULL prices, got {[r[0] for r in rows]}"
            )
        finally:
            os.unlink(db)


class TestOptionSellToOpenBuildsShortPosition:
    """Pin the second-order fix: an option SELL row with no long lot
    to consume must be treated as a sell-to-open (short option),
    not silently ignored. Without this, every multileg short leg
    produces zero position state — the AI thinks the spread is just
    the long leg. Caught 2026-05-11 (same incident).
    """

    def test_multileg_short_leg_creates_short_position(self):
        """A bull put spread: long leg + short leg both at status='open'.
        Both legs must produce positions in get_virtual_positions
        (long qty>0, short qty<0)."""
        db = _make_db()
        try:
            conn = sqlite3.connect(db)
            # Short leg (sell-to-open higher-strike put)
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, fill_price, occ_symbol, signal_type, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-05-11T13:44:20", "RTX", "sell", 1.0, 3.15,
                 3.15, "RTX260618P00170000", "MULTILEG", "open"),
            )
            # Long leg (buy-to-open lower-strike put)
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, fill_price, occ_symbol, signal_type, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-05-11T13:44:21", "RTX", "buy", 1.0, 1.74,
                 1.74, "RTX260618P00160000", "MULTILEG", "open"),
            )
            conn.commit()
            conn.close()

            from journal import get_virtual_positions
            positions = get_virtual_positions(db)

            summary = [(p["symbol"], p.get("occ_symbol"), p["qty"])
                       for p in positions]
            assert len(positions) == 2, (
                f"Both legs should produce positions, got {summary}"
            )
            short = next(p for p in positions
                         if p["occ_symbol"] == "RTX260618P00170000")
            long_ = next(p for p in positions
                         if p["occ_symbol"] == "RTX260618P00160000")
            # Short leg: qty<0, avg_entry from the sell price
            assert short["qty"] < 0
            assert abs(short["qty"]) == pytest.approx(1.0)
            assert short["avg_entry_price"] == pytest.approx(3.15)
            # Long leg: qty>0, avg_entry from the buy price
            assert long_["qty"] > 0
            assert long_["qty"] == pytest.approx(1.0)
            assert long_["avg_entry_price"] == pytest.approx(1.74)
        finally:
            os.unlink(db)

    def test_stock_sell_with_no_long_opens_a_real_short(self):
        """Order-id truth (2026-06-18): a FILLED stock `side='sell'` with
        no long lot is a real broker short sale and MUST surface as a
        short — it is NOT dropped. The old 'stocks drop a bare sell' rule
        is exactly what hid the UWMC oversell and produced ~$187K of
        phantom equity (the sell proceeds stayed in cash with no
        offsetting position). Intentional shorts should still journal
        `side='short'` so the close routes through 'cover'; options keep
        their own sell-to-open handling."""
        db = _make_db()
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, fill_price, occ_symbol, signal_type, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-05-11T10:00:00", "AAPL", "sell", 100, 150.0,
                 150.0, None, "BUY", "open"),
            )
            conn.commit()
            conn.close()

            from journal import get_virtual_positions
            positions = get_virtual_positions(db)
            # A bare filled stock sell = sold 100 shares never owned = a
            # real -100 short. Surfacing it keeps virtual == broker
            # (order-id truth); dropping it was the phantom-equity bug.
            assert len(positions) == 1
            assert positions[0]["symbol"] == "AAPL"
            assert positions[0]["qty"] == -100
        finally:
            os.unlink(db)

    def test_option_sell_after_long_consumed_remainder_opens_short(self):
        """Mixed case: long 1ct already, then sell 3ct. First 1ct
        consumes the long; remaining 2ct opens a short position.
        The function should produce the resulting short position."""
        db = _make_db()
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, fill_price, occ_symbol, signal_type, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-05-10T10:00:00", "RTX", "buy", 1.0, 1.74,
                 1.74, "RTX260618P00160000", "MULTILEG", "open"),
            )
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, fill_price, occ_symbol, signal_type, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-05-11T10:00:00", "RTX", "sell", 3.0, 2.00,
                 2.00, "RTX260618P00160000", "MULTILEG", "open"),
            )
            conn.commit()
            conn.close()

            from journal import get_virtual_positions
            positions = get_virtual_positions(db)
            assert len(positions) == 1
            p = positions[0]
            # 1 long consumed, 2 remaining → short 2 contracts
            assert p["qty"] == pytest.approx(-2.0)
        finally:
            os.unlink(db)


class TestGetVirtualPositionsSurfacesBadPrice:
    def test_warning_logged_when_skipping_negative_price_row(self, caplog):
        """When `get_virtual_positions` encounters a row with qty>0
        but price<=0, it must skip it (existing behavior) AND log a
        WARNING naming the count + db_path. No silent failure."""
        db = _make_db()
        try:
            conn = sqlite3.connect(db)
            # Insert one bad-price multileg row (the prod bug shape)
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, fill_price, occ_symbol, signal_type, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-05-11T13:44:20", "RTX", "buy", 1.0, -1.41,
                 -1.41, "RTX260618P00160000", "MULTILEG", "open"),
            )
            conn.commit()
            conn.close()

            from journal import get_virtual_positions
            with caplog.at_level(logging.WARNING, logger="root"):
                positions = get_virtual_positions(db)

            # No position output (correctly invisible — the bug)
            assert positions == []
            # But a WARNING was logged naming the count + db
            matching = [r for r in caplog.records
                        if "skipped" in r.getMessage().lower()
                        and db in r.getMessage()]
            assert matching, (
                "Expected WARNING naming the skipped count + db_path. "
                f"Got: {[r.getMessage() for r in caplog.records]}"
            )
        finally:
            os.unlink(db)
