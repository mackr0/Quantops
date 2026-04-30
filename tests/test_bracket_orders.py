"""Tests for bracket_orders.py — broker-managed protective stops.

Stage 1 of INTRADAY_STOPS_PLAN.md. The polling-based exit logic
gave fills at the next-cycle current price, which is typically far
past the stop level (AMD: -7.91% on a -5% threshold). These tests
pin the broker-side fix.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# stop_price_for_entry
# ---------------------------------------------------------------------------

def test_long_stop_price_below_entry():
    from bracket_orders import stop_price_for_entry
    sp = stop_price_for_entry(entry_price=100.0, stop_loss_pct=0.05, is_short=False)
    assert sp == pytest.approx(95.0, abs=0.001)


def test_short_stop_price_above_entry():
    from bracket_orders import stop_price_for_entry
    sp = stop_price_for_entry(entry_price=100.0, stop_loss_pct=0.05, is_short=True)
    assert sp == pytest.approx(105.0, abs=0.001)


def test_invalid_inputs_return_none():
    from bracket_orders import stop_price_for_entry
    assert stop_price_for_entry(0, 0.05, False) is None
    assert stop_price_for_entry(100, None, False) is None
    assert stop_price_for_entry(100, 0, False) is None
    assert stop_price_for_entry(100, -0.05, False) is None


# ---------------------------------------------------------------------------
# submit_protective_stop
# ---------------------------------------------------------------------------

def test_submit_calls_alpaca_with_stop_order():
    """The Alpaca submit_order call must use type='stop' with stop_price
    specified — type='market' would defeat the whole purpose."""
    from bracket_orders import submit_protective_stop
    api = MagicMock()
    api.submit_order.return_value = MagicMock(id="abc-123")
    order_id = submit_protective_stop(
        api, "AAPL", qty=100, side="sell", stop_price=95.50,
    )
    assert order_id == "abc-123"
    args = api.submit_order.call_args
    assert args.kwargs["symbol"] == "AAPL"
    assert args.kwargs["qty"] == 100
    assert args.kwargs["side"] == "sell"
    assert args.kwargs["type"] == "stop"
    assert args.kwargs["stop_price"] == 95.50
    # GTC so the stop survives until filled or position closed
    assert args.kwargs["time_in_force"] == "gtc"


def test_submit_returns_none_on_invalid_inputs():
    from bracket_orders import submit_protective_stop
    api = MagicMock()
    assert submit_protective_stop(api, "", 100, "sell", 95.0) is None
    assert submit_protective_stop(api, "AAPL", 0, "sell", 95.0) is None
    assert submit_protective_stop(api, "AAPL", 100, "sell", 0) is None
    assert submit_protective_stop(api, "AAPL", 100, "fly", 95.0) is None
    api.submit_order.assert_not_called()


def test_submit_returns_none_on_broker_error():
    """Failure must not kill the caller — polling fallback exists."""
    from bracket_orders import submit_protective_stop
    api = MagicMock()
    api.submit_order.side_effect = Exception("broker rejected")
    order_id = submit_protective_stop(api, "AAPL", 100, "sell", 95.0)
    assert order_id is None


# ---------------------------------------------------------------------------
# cancel_protective_stop
# ---------------------------------------------------------------------------

def test_cancel_no_order_id_is_noop():
    from bracket_orders import cancel_protective_stop
    api = MagicMock()
    assert cancel_protective_stop(api, None) is True
    assert cancel_protective_stop(api, "") is True
    api.cancel_order.assert_not_called()


def test_cancel_already_filled_treated_as_success():
    """If the broker says the order is already filled, the goal is
    reached anyway — don't error."""
    from bracket_orders import cancel_protective_stop
    api = MagicMock()
    api.cancel_order.side_effect = Exception("order already filled")
    assert cancel_protective_stop(api, "abc-123") is True


def test_cancel_returns_false_on_unknown_error():
    from bracket_orders import cancel_protective_stop
    api = MagicMock()
    api.cancel_order.side_effect = Exception("network unreachable")
    assert cancel_protective_stop(api, "abc-123") is False


# ---------------------------------------------------------------------------
# ensure_protective_stops sweep
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    db = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, status TEXT,
            decision_price REAL, fill_price REAL, slippage_pct REAL,
            max_favorable_excursion REAL,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db


def _make_ctx(stop_loss_pct=0.05, short_stop_loss_pct=0.08,
                 take_profit_pct=None, short_take_profit_pct=None,
                 use_trailing_stops=False):
    ctx = MagicMock()
    ctx.stop_loss_pct = stop_loss_pct
    ctx.short_stop_loss_pct = short_stop_loss_pct
    # Explicit None or value — never the default MagicMock attribute
    # access (which would return a Mock that breaks numeric comparisons
    # or accidentally enables features).
    ctx.take_profit_pct = take_profit_pct
    ctx.short_take_profit_pct = short_take_profit_pct
    ctx.use_trailing_stops = use_trailing_stops
    return ctx


def _seed_open_buy(db, symbol, qty, price, order_id="entry-1",
                     existing_stop_id=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, order_id, "
        "status, protective_stop_order_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
        ("2026-04-29", symbol, "buy", qty, price, order_id, existing_stop_id),
    )
    conn.commit()
    conn.close()


def test_sweep_places_stop_on_unprotected_position(tmp_db):
    from bracket_orders import ensure_protective_stops
    _seed_open_buy(tmp_db, "AAPL", 100, 150.0)
    api = MagicMock()
    api.submit_order.return_value = MagicMock(id="stop-xyz")
    positions = [{"symbol": "AAPL", "qty": 100, "avg_entry_price": 150.0}]

    ensure_protective_stops(api, positions, _make_ctx(stop_loss_pct=0.05), tmp_db)

    # Stop at 150 × 0.95 = 142.50
    args = api.submit_order.call_args
    assert args.kwargs["type"] == "stop"
    assert args.kwargs["stop_price"] == pytest.approx(142.50, abs=0.01)
    # DB row updated with order_id
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT protective_stop_order_id FROM trades WHERE symbol='AAPL'"
    ).fetchone()
    conn.close()
    assert row[0] == "stop-xyz"


def test_sweep_skips_position_with_active_stop(tmp_db):
    """When the trades row has an order_id and the broker says the
    order is still active, no new submit happens."""
    from bracket_orders import ensure_protective_stops
    _seed_open_buy(tmp_db, "AAPL", 100, 150.0, existing_stop_id="stop-1")
    api = MagicMock()
    # Active order — broker reports 'new' status
    api.get_order.return_value = MagicMock(status="new")
    positions = [{"symbol": "AAPL", "qty": 100, "avg_entry_price": 150.0}]

    ensure_protective_stops(api, positions, _make_ctx(), tmp_db)
    api.submit_order.assert_not_called()


def test_sweep_resubmits_when_existing_order_is_stale(tmp_db):
    """Cancelled/filled orders aren't 'active' — sweep must replace them."""
    from bracket_orders import ensure_protective_stops
    _seed_open_buy(tmp_db, "AAPL", 100, 150.0, existing_stop_id="dead-stop")
    api = MagicMock()
    api.get_order.return_value = MagicMock(status="filled")  # stale
    api.submit_order.return_value = MagicMock(id="fresh-stop")
    positions = [{"symbol": "AAPL", "qty": 100, "avg_entry_price": 150.0}]

    ensure_protective_stops(api, positions, _make_ctx(), tmp_db)
    api.submit_order.assert_called_once()
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT protective_stop_order_id FROM trades WHERE symbol='AAPL'"
    ).fetchone()
    conn.close()
    assert row[0] == "fresh-stop"


def test_sweep_handles_short_positions_with_buy_to_close(tmp_db):
    """For a short, the protective stop is a BUY stop ABOVE entry."""
    from bracket_orders import ensure_protective_stops
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "order_id, status) VALUES (?, ?, 'short', ?, ?, ?, 'open')",
        ("2026-04-29", "TSLA", 50, 200.0, "short-entry"),
    )
    conn.commit()
    conn.close()

    api = MagicMock()
    api.submit_order.return_value = MagicMock(id="cover-stop")
    positions = [{"symbol": "TSLA", "qty": -50, "avg_entry_price": 200.0}]

    ensure_protective_stops(api, positions, _make_ctx(short_stop_loss_pct=0.08),
                              tmp_db)
    args = api.submit_order.call_args
    assert args.kwargs["side"] == "buy"
    assert args.kwargs["stop_price"] == pytest.approx(216.00, abs=0.01)


# ---------------------------------------------------------------------------
# cancel_for_symbol
# ---------------------------------------------------------------------------

def test_cancel_for_symbol_clears_db(tmp_db):
    """After AI-driven exit, the DB column must be reset to NULL so
    the next sweep doesn't try to verify a cancelled order."""
    from bracket_orders import cancel_for_symbol
    _seed_open_buy(tmp_db, "AAPL", 100, 150.0, existing_stop_id="will-die")
    api = MagicMock()
    cancel_for_symbol(api, tmp_db, "AAPL")

    api.cancel_order.assert_called_with("will-die")
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT protective_stop_order_id FROM trades WHERE symbol='AAPL'"
    ).fetchone()
    conn.close()
    assert row[0] is None


def test_cancel_for_symbol_noop_when_no_active_stop(tmp_db):
    from bracket_orders import cancel_for_symbol
    _seed_open_buy(tmp_db, "AAPL", 100, 150.0)  # no protective_stop_order_id
    api = MagicMock()
    cancel_for_symbol(api, tmp_db, "AAPL")
    api.cancel_order.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: trader.check_exits invokes the sweep
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stage 2: Take-profit limit orders
# ---------------------------------------------------------------------------

def test_long_tp_price_above_entry():
    from bracket_orders import tp_price_for_entry
    tp = tp_price_for_entry(entry_price=100.0, take_profit_pct=0.10, is_short=False)
    assert tp == pytest.approx(110.0, abs=0.001)


def test_short_tp_price_below_entry():
    from bracket_orders import tp_price_for_entry
    tp = tp_price_for_entry(entry_price=100.0, take_profit_pct=0.10, is_short=True)
    assert tp == pytest.approx(90.0, abs=0.001)


def test_submit_take_profit_uses_limit_order_type():
    """TP must use type='limit', not 'stop'. Limit fills only at the
    target price or better — won't slip past on gaps."""
    from bracket_orders import submit_protective_take_profit
    api = MagicMock()
    api.submit_order.return_value = MagicMock(id="tp-123")
    order_id = submit_protective_take_profit(
        api, "AAPL", qty=100, side="sell", limit_price=110.50,
    )
    assert order_id == "tp-123"
    args = api.submit_order.call_args
    assert args.kwargs["type"] == "limit"
    assert args.kwargs["limit_price"] == 110.50
    assert args.kwargs["time_in_force"] == "gtc"


def test_sweep_places_take_profit_alongside_stop_loss(tmp_db):
    """A position with neither stop nor TP gets both placed."""
    from bracket_orders import ensure_protective_stops
    _seed_open_buy(tmp_db, "AAPL", 100, 150.0)
    api = MagicMock()
    # Two distinct order_ids returned — first call is the stop, second the TP.
    api.submit_order.side_effect = [
        MagicMock(id="stop-id"),
        MagicMock(id="tp-id"),
    ]
    ctx = _make_ctx(stop_loss_pct=0.05)
    ctx.take_profit_pct = 0.10
    positions = [{"symbol": "AAPL", "qty": 100, "avg_entry_price": 150.0}]

    ensure_protective_stops(api, positions, ctx, tmp_db)

    assert api.submit_order.call_count == 2
    # Verify order types — stop first, limit second
    calls = api.submit_order.call_args_list
    assert calls[0].kwargs["type"] == "stop"
    assert calls[1].kwargs["type"] == "limit"
    # Stop at 142.50, TP at 165.00
    assert calls[0].kwargs["stop_price"] == pytest.approx(142.50, abs=0.01)
    assert calls[1].kwargs["limit_price"] == pytest.approx(165.00, abs=0.01)
    # DB columns both updated
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT protective_stop_order_id, protective_tp_order_id "
        "FROM trades WHERE symbol='AAPL'"
    ).fetchone()
    conn.close()
    assert row[0] == "stop-id"
    assert row[1] == "tp-id"


def test_sweep_skips_tp_when_conviction_override_is_active(tmp_db):
    """conviction_tp_skip(symbol, pct) → True means 'don't cap this
    runaway winner'. Sweep must NOT place a TP order in that case."""
    from bracket_orders import ensure_protective_stops
    _seed_open_buy(tmp_db, "TSLA", 100, 100.0)
    api = MagicMock()
    api.submit_order.return_value = MagicMock(id="stop-only")
    ctx = _make_ctx(stop_loss_pct=0.05)
    ctx.take_profit_pct = 0.10
    positions = [{
        "symbol": "TSLA", "qty": 100, "avg_entry_price": 100.0,
        "current_price": 130.0,  # +30%, conviction override likely fires
    }]

    skip = lambda sym, pct: True  # always skip — runaway winner
    ensure_protective_stops(api, positions, ctx, tmp_db,
                              conviction_tp_skip=skip)

    # Only one submit (the stop). TP suppressed.
    assert api.submit_order.call_count == 1
    assert api.submit_order.call_args.kwargs["type"] == "stop"


def test_cancel_for_symbol_clears_both_stop_and_tp(tmp_db):
    """AI early-exit must cancel BOTH protective orders. Otherwise a
    leftover TP fires later on a flat position."""
    from bracket_orders import cancel_for_symbol
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "status, protective_stop_order_id, protective_tp_order_id) "
        "VALUES (?, ?, 'buy', ?, ?, 'open', ?, ?)",
        ("2026-04-29", "AAPL", 100, 150.0, "stop-id", "tp-id"),
    )
    conn.commit()
    conn.close()
    api = MagicMock()

    cancel_for_symbol(api, tmp_db, "AAPL")

    # Both broker orders cancelled
    cancelled_ids = [c.args[0] for c in api.cancel_order.call_args_list]
    assert "stop-id" in cancelled_ids
    assert "tp-id" in cancelled_ids
    # Both DB columns cleared
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT protective_stop_order_id, protective_tp_order_id "
        "FROM trades WHERE symbol='AAPL'"
    ).fetchone()
    conn.close()
    assert row[0] is None
    assert row[1] is None


# ---------------------------------------------------------------------------
# Stage 3: Trailing-stop orders
# ---------------------------------------------------------------------------

def test_trail_percent_clamped_to_bounds():
    from bracket_orders import (
        trail_percent_for_entry, TRAIL_PERCENT_MIN, TRAIL_PERCENT_MAX,
    )
    # 1% stop_loss → would be 1.0%, clamped UP to 2.0%
    assert trail_percent_for_entry(0.01) == TRAIL_PERCENT_MIN
    # 5% stop_loss → 5.0% (within bounds)
    assert trail_percent_for_entry(0.05) == 5.0
    # 15% stop_loss → 15.0%, clamped DOWN to 10.0%
    assert trail_percent_for_entry(0.15) == TRAIL_PERCENT_MAX


def test_trail_percent_returns_none_for_invalid_inputs():
    from bracket_orders import trail_percent_for_entry
    assert trail_percent_for_entry(None) is None
    assert trail_percent_for_entry(0) is None
    assert trail_percent_for_entry(-0.05) is None


def test_submit_trailing_uses_trailing_stop_order_type():
    """The fix relies on Alpaca's native trailing_stop type — the
    broker tracks high water and fires when price drops by trail_percent.
    Polling on daily bars never had a chance to catch fast intraday
    reversals (the IBM tiny-win pattern)."""
    from bracket_orders import submit_protective_trailing
    api = MagicMock()
    api.submit_order.return_value = MagicMock(id="trail-xyz")
    order_id = submit_protective_trailing(
        api, "AAPL", qty=100, side="sell", trail_percent=5.0,
    )
    assert order_id == "trail-xyz"
    args = api.submit_order.call_args
    assert args.kwargs["type"] == "trailing_stop"
    assert args.kwargs["trail_percent"] == "5.0"
    assert args.kwargs["time_in_force"] == "gtc"


def test_sweep_places_trailing_stop_when_use_trailing_enabled(tmp_db):
    from bracket_orders import ensure_protective_stops
    _seed_open_buy(tmp_db, "AAPL", 100, 150.0)
    api = MagicMock()
    api.submit_order.side_effect = [
        MagicMock(id="stop-id"),
        MagicMock(id="tp-id"),
        MagicMock(id="trail-id"),
    ]
    ctx = _make_ctx(stop_loss_pct=0.05, take_profit_pct=0.10)
    ctx.take_profit_pct = 0.10
    ctx.use_trailing_stops = True
    positions = [{"symbol": "AAPL", "qty": 100, "avg_entry_price": 150.0}]

    ensure_protective_stops(api, positions, ctx, tmp_db)

    # 3 orders placed: stop, TP, trailing
    assert api.submit_order.call_count == 3
    types = [c.kwargs["type"] for c in api.submit_order.call_args_list]
    assert "stop" in types
    assert "limit" in types
    assert "trailing_stop" in types
    # DB has all three IDs
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT protective_stop_order_id, protective_tp_order_id, "
        "protective_trailing_order_id FROM trades WHERE symbol='AAPL'"
    ).fetchone()
    conn.close()
    assert row[0] == "stop-id"
    assert row[1] == "tp-id"
    assert row[2] == "trail-id"


def test_sweep_skips_trailing_when_use_trailing_disabled(tmp_db):
    from bracket_orders import ensure_protective_stops
    _seed_open_buy(tmp_db, "AAPL", 100, 150.0)
    api = MagicMock()
    api.submit_order.side_effect = [
        MagicMock(id="stop-id"),
        MagicMock(id="tp-id"),
    ]
    ctx = _make_ctx(stop_loss_pct=0.05)
    ctx.take_profit_pct = 0.10
    ctx.use_trailing_stops = False  # explicit
    positions = [{"symbol": "AAPL", "qty": 100, "avg_entry_price": 150.0}]

    ensure_protective_stops(api, positions, ctx, tmp_db)
    # Only 2 calls — stop and TP. No trailing.
    assert api.submit_order.call_count == 2
    types = [c.kwargs["type"] for c in api.submit_order.call_args_list]
    assert "trailing_stop" not in types


def test_cancel_for_symbol_clears_all_three_protective_orders(tmp_db):
    """When AI does an early exit, all three broker orders must be
    cancelled so they don't orphan-fire on a flat position."""
    from bracket_orders import cancel_for_symbol
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "status, protective_stop_order_id, protective_tp_order_id, "
        "protective_trailing_order_id) "
        "VALUES (?, ?, 'buy', ?, ?, 'open', ?, ?, ?)",
        ("2026-04-29", "AAPL", 100, 150.0,
         "stop-id", "tp-id", "trail-id"),
    )
    conn.commit()
    conn.close()
    api = MagicMock()

    cancel_for_symbol(api, tmp_db, "AAPL")

    cancelled = [c.args[0] for c in api.cancel_order.call_args_list]
    assert "stop-id" in cancelled
    assert "tp-id" in cancelled
    assert "trail-id" in cancelled
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT protective_stop_order_id, protective_tp_order_id, "
        "protective_trailing_order_id FROM trades WHERE symbol='AAPL'"
    ).fetchone()
    conn.close()
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None


def test_check_exits_invokes_protective_sweep():
    """Source-level pin: trader.check_exits must call
    ensure_protective_stops. Without it, polling-based stop detection
    runs on a 5-min cycle while real prices move continuously."""
    import inspect
    import trader
    src = inspect.getsource(trader.check_exits)
    assert "ensure_protective_stops" in src, (
        "REGRESSION: trader.check_exits no longer calls "
        "ensure_protective_stops. Stage 1 of INTRADAY_STOPS_PLAN "
        "regressed — fills will revert to next-cycle current price."
    )


def test_check_exits_clears_protective_stop_on_polling_exit():
    """Source-level pin: trader.check_exits must call cancel_for_symbol
    in the exit loop. Without it, the broker stop sits orphaned at
    Alpaca after a polling-driven exit and can fire on a flat
    position next cycle."""
    import inspect
    import trader
    src = inspect.getsource(trader.check_exits)
    assert "cancel_for_symbol" in src, (
        "REGRESSION: trader.check_exits no longer cancels protective "
        "stops on polling-driven exits. Broker stops will orphan "
        "after market sells."
    )
