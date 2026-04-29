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
            protective_stop_order_id TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db


def _make_ctx(stop_loss_pct=0.05, short_stop_loss_pct=0.08):
    ctx = MagicMock()
    ctx.stop_loss_pct = stop_loss_pct
    ctx.short_stop_loss_pct = short_stop_loss_pct
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
