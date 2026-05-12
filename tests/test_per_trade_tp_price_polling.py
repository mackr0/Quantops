"""Regression tests for per-trade TP/SL price polling (2026-05-12).

The UNH bug:
  - Entry: $356.37 with AI-calculated take_profit price $379.36
    (6.5% target)
  - Profile take_profit_pct: 0.15 (15%)
  - Current price: $396 (11.2% gain — well past the AI target,
    but BELOW the 15% profile threshold)
  - check_stop_loss_take_profit (portfolio_manager) used the profile
    PERCENT threshold, not the per-trade PRICE target → TP never
    fired → position rode unrealized gains to +11.2% with no
    capture

This test pins:
  1. When a position dict carries `take_profit_price`, the polling
     check fires the moment current_price >= that price, even when
     the profile-level percent threshold isn't reached.
  2. When `stop_loss_price` is present, fires on price-based stop.
  3. Legacy behavior preserved: without the per-trade price, the
     profile-level percent threshold still works.
  4. `get_virtual_positions` propagates the most-recent open BUY
     row's take_profit/stop_loss prices into the position dict so
     the polling check can see them.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# portfolio_manager.check_stop_loss_take_profit — per-trade price wins
# ---------------------------------------------------------------------------

class TestPerTradePriceFiresPolling:
    """The UNH case. AI target $379 on $356 entry; current $396.
    Profile percent threshold is 15%, but per-trade price target
    is in the position dict. TP should fire."""

    def _pos(self, current_price, tp_price=None, sl_price=None):
        return {
            "symbol": "UNH",
            "current_price": current_price,
            "avg_entry_price": 356.37,
            "qty": 11,
            "take_profit_price": tp_price,
            "stop_loss_price": sl_price,
        }

    def test_tp_price_fires_below_profile_percent(self):
        """current $396 >= AI target $379. Profile would need 15%
        (≥$410) to fire on percent — but the price target should
        win."""
        from portfolio_manager import check_stop_loss_take_profit
        pos = self._pos(current_price=396.44, tp_price=379.36)
        triggered = check_stop_loss_take_profit(
            [pos], stop_loss_pct=0.05, take_profit_pct=0.15,
        )
        tps = [t for t in triggered if t["trigger"] == "take_profit"]
        assert len(tps) == 1, triggered
        assert "AI target" in tps[0]["reason"]
        assert "$379.36" in tps[0]["reason"]

    def test_sl_price_fires_above_profile_percent(self):
        """current $341 <= AI stop $341.05. Profile would need 5%
        (≤$338.55) to fire on percent — but the stop price wins.
        Without this the AI's tighter stop is ignored."""
        from portfolio_manager import check_stop_loss_take_profit
        pos = self._pos(current_price=340.50, sl_price=341.05)
        triggered = check_stop_loss_take_profit(
            [pos], stop_loss_pct=0.05, take_profit_pct=0.15,
        )
        stops = [t for t in triggered if t["trigger"] == "stop_loss"]
        assert len(stops) == 1, triggered
        assert "AI target" in stops[0]["reason"]

    def test_no_price_falls_back_to_profile_percent(self):
        """Legacy behavior preserved: when the per-trade prices
        aren't carried, the profile percent threshold still fires."""
        from portfolio_manager import check_stop_loss_take_profit
        # 16% gain — past 15% profile threshold
        pos = self._pos(current_price=413.39, tp_price=None)
        triggered = check_stop_loss_take_profit(
            [pos], stop_loss_pct=0.05, take_profit_pct=0.15,
        )
        tps = [t for t in triggered if t["trigger"] == "take_profit"]
        assert len(tps) == 1
        # Reason format from the percent branch
        assert "threshold +15%" in tps[0]["reason"]

    def test_price_not_yet_reached_no_fire(self):
        """current $370 < AI target $379, and 3.8% < 15% profile.
        Neither path should fire."""
        from portfolio_manager import check_stop_loss_take_profit
        pos = self._pos(current_price=370.0, tp_price=379.36)
        triggered = check_stop_loss_take_profit(
            [pos], stop_loss_pct=0.05, take_profit_pct=0.15,
        )
        assert len([t for t in triggered if t["trigger"] == "take_profit"]) == 0

    def test_conviction_override_still_works_with_price_target(self):
        """The runaway-winner override (use_conviction_tp_override)
        must still skip TP firing — even when the AI's price target
        was hit. Otherwise the override is contradicted."""
        from portfolio_manager import check_stop_loss_take_profit
        pos = self._pos(current_price=396.44, tp_price=379.36)
        triggered = check_stop_loss_take_profit(
            [pos], stop_loss_pct=0.05, take_profit_pct=0.15,
            conviction_tp_skip=lambda sym, pct: True,
        )
        assert len([t for t in triggered if t["trigger"] == "take_profit"]) == 0


# ---------------------------------------------------------------------------
# journal.get_virtual_positions — propagates per-trade prices
# ---------------------------------------------------------------------------

class TestGetVirtualPositionsPropagatesPrices:
    def _make_db_with_unh_buy(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from journal import init_db
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "stop_loss, take_profit, status, signal_type) "
            "VALUES ('2026-04-22T15:33:19', 'UNH', 'buy', 11, 356.37, "
            "341.05, 379.36, 'open', 'BUY')"
        )
        conn.commit()
        conn.close()
        return path

    def test_open_buy_tp_sl_propagate_to_position(self):
        from journal import get_virtual_positions
        path = self._make_db_with_unh_buy()
        try:
            positions = get_virtual_positions(
                db_path=path,
                price_fetcher=lambda sym, side=None: 396.44,
            )
            assert len(positions) == 1
            p = positions[0]
            # Position class shim supports .get()
            assert p.get("take_profit_price") == 379.36
            assert p.get("stop_loss_price") == 341.05
        finally:
            os.unlink(path)

    def test_no_tp_sl_set_returns_none(self):
        """Positions whose entry row didn't carry tp/sl prices end
        up with None (legacy entries pre-2026-05-12). Polling falls
        back to profile percent."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from journal import init_db
        init_db(path)
        try:
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, price, "
                "status, signal_type) "
                "VALUES ('2026-04-01T15:00:00', 'AAPL', 'buy', 5, 180.0, "
                "'open', 'BUY')"
            )
            conn.commit()
            conn.close()
            from journal import get_virtual_positions
            positions = get_virtual_positions(
                db_path=path,
                price_fetcher=lambda sym, side=None: 185.0,
            )
            assert len(positions) == 1
            assert positions[0].get("take_profit_price") is None
            assert positions[0].get("stop_loss_price") is None
        finally:
            os.unlink(path)
