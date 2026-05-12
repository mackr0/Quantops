"""TODO #7 (2026-05-11) — single-leg option exit logic.

Today's `portfolio_manager.check_stop_loss_take_profit` skips ALL
option positions (safe for multileg legs which are protected by
structural max loss; UNSAFE for single-leg longs which can lose
100% of premium with no automated exit). This module adds three
exit triggers for single-leg long options.

This file pins:
- PREMIUM STOP: -50% premium drop → close.
- PREMIUM TAKE-PROFIT: +100% premium gain → close.
- DTE EXIT: ≤7 days to expiry → close (avoid gamma blowup).
- MULTILEG SKIP: positions whose entry trade was signal_type='MULTILEG'
  are NEVER independently closed (would orphan partner legs).
- SHORT-LEG SKIP: short single-leg positions skipped this commit
  (different economics — theta is good for shorts).
- THRESHOLD CORRECTNESS: -45% drop does NOT trigger; -50% does.
- DTE BOUNDARY: DTE=8 does NOT trigger; DTE=7 does.
- PAYLOAD SHAPE: submit_option_close builds correct Alpaca raw POST
  payload with position_intent='sell_to_close'.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from options_exits import (
    check_single_leg_option_exits, submit_option_close,
    PREMIUM_STOP_LOSS_PCT, PREMIUM_TAKE_PROFIT_PCT,
    DTE_EXIT_THRESHOLD_DAYS,
    SHORT_PREMIUM_TAKE_PROFIT_PCT, SHORT_PREMIUM_STOP_LOSS_PCT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from journal import init_db
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _occ(underlying="AAPL", expiry_days=32, strike=150.0, right="C"):
    """Build a 21-char OCC symbol with the given expiry."""
    today = date.today()
    expiry = today + timedelta(days=expiry_days)
    yymmdd = expiry.strftime("%y%m%d")
    strike_str = f"{int(round(strike * 1000)):08d}"
    root = underlying.ljust(6)
    return f"{root}{yymmdd}{right}{strike_str}"


def _option_position(occ_symbol, qty=1, entry=2.40, current=2.40):
    """Synthetic single-leg option position dict."""
    return {
        "symbol": occ_symbol,
        "occ_symbol": occ_symbol,
        "qty": qty,
        "avg_entry_price": entry,
        "current_price": current,
    }


def _stock_position(symbol="AAPL", qty=10):
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": 150.0,
        "current_price": 152.0,
    }


def _log_open_trade(db_path, occ_symbol, signal_type, qty=1):
    """Insert an open entry trade into the trades table so the
    exits module can look up its signal_type."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO trades
               (timestamp, symbol, side, qty, price, fill_price,
                order_id, signal_type, status, occ_symbol)
               VALUES (datetime('now'), 'AAPL', 'buy', ?, 2.40, 2.40,
                       'ord-1', ?, 'open', ?)""",
            (qty, signal_type, occ_symbol),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Premium-stop trigger
# ---------------------------------------------------------------------------

class TestPremiumStop:
    def test_50pct_drop_triggers(self, db_path):
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "OPTIONS")
        pos = _option_position(occ, entry=2.40, current=1.20)
        signals = check_single_leg_option_exits([pos], db_path)
        assert len(signals) == 1
        assert signals[0]["trigger"] == "premium_stop"
        assert signals[0]["side_to_close"] == "sell"
        assert signals[0]["qty"] == 1

    def test_45pct_drop_does_not_trigger(self, db_path):
        """Just inside the threshold — must NOT fire."""
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "OPTIONS")
        # 2.40 → 1.32 = -45%
        pos = _option_position(occ, entry=2.40, current=1.32)
        signals = check_single_leg_option_exits([pos], db_path)
        assert signals == []

    def test_premium_pct_change_recorded(self, db_path):
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "OPTIONS")
        pos = _option_position(occ, entry=2.40, current=1.20)
        signals = check_single_leg_option_exits([pos], db_path)
        assert signals[0]["premium_pct_change"] == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# Premium take-profit trigger
# ---------------------------------------------------------------------------

class TestPremiumTakeProfit:
    def test_100pct_gain_triggers(self, db_path):
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "OPTIONS")
        pos = _option_position(occ, entry=2.40, current=4.80)
        signals = check_single_leg_option_exits([pos], db_path)
        assert len(signals) == 1
        assert signals[0]["trigger"] == "premium_take_profit"

    def test_90pct_gain_does_not_trigger(self, db_path):
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "OPTIONS")
        # 2.40 → 4.56 = +90%
        pos = _option_position(occ, entry=2.40, current=4.56)
        signals = check_single_leg_option_exits([pos], db_path)
        assert signals == []


# ---------------------------------------------------------------------------
# DTE exit trigger
# ---------------------------------------------------------------------------

class TestDTEExit:
    def test_7_days_to_expiry_triggers(self, db_path):
        occ = _occ(expiry_days=7)
        _log_open_trade(db_path, occ, "OPTIONS")
        pos = _option_position(occ, entry=2.40, current=2.40)
        signals = check_single_leg_option_exits([pos], db_path)
        assert len(signals) == 1
        assert signals[0]["trigger"] == "dte_exit"

    def test_8_days_to_expiry_does_not_trigger(self, db_path):
        occ = _occ(expiry_days=8)
        _log_open_trade(db_path, occ, "OPTIONS")
        pos = _option_position(occ, entry=2.40, current=2.40)
        signals = check_single_leg_option_exits([pos], db_path)
        assert signals == []

    def test_dte_fires_even_on_neutral_premium(self, db_path):
        """Time-stop fires regardless of premium movement — the
        gamma risk near expiry justifies closing even at a flat P&L."""
        occ = _occ(expiry_days=5)
        _log_open_trade(db_path, occ, "OPTIONS")
        pos = _option_position(occ, entry=2.40, current=2.30)
        signals = check_single_leg_option_exits([pos], db_path)
        assert len(signals) == 1
        assert signals[0]["trigger"] == "dte_exit"


# ---------------------------------------------------------------------------
# MULTILEG SKIP — the most important safety invariant
# ---------------------------------------------------------------------------

class TestMultilegSkip:
    """Multileg legs MUST NOT be independently closed — would orphan
    the partner leg, exposing it to undefined risk. The check is by
    looking up the entry trade's signal_type."""

    def test_multileg_leg_not_triggered_on_premium_drop(self, db_path):
        occ = _occ(expiry_days=32)
        # Entry was MULTILEG — this leg is part of a spread
        _log_open_trade(db_path, occ, "MULTILEG")
        pos = _option_position(occ, entry=2.40, current=0.20)  # -91%
        signals = check_single_leg_option_exits([pos], db_path)
        assert signals == [], (
            "Multileg leg with -91% premium drop must NOT trigger "
            "an independent close (would orphan the partner leg)"
        )

    def test_multileg_leg_not_triggered_on_dte(self, db_path):
        occ = _occ(expiry_days=3)
        _log_open_trade(db_path, occ, "MULTILEG")
        pos = _option_position(occ, entry=2.40, current=2.40)
        signals = check_single_leg_option_exits([pos], db_path)
        assert signals == []

    def test_single_leg_option_does_trigger(self, db_path):
        """Sanity: with signal_type='OPTIONS' (single-leg), the
        same -91% drop DOES trigger."""
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "OPTIONS")
        pos = _option_position(occ, entry=2.40, current=0.20)
        signals = check_single_leg_option_exits([pos], db_path)
        assert len(signals) == 1
        assert signals[0]["trigger"] == "premium_stop"


# ---------------------------------------------------------------------------
# SHORT-LEG SKIP — defensive (different economics)
# ---------------------------------------------------------------------------

class TestShortLegExits:
    """2026-05-12: short single-leg option exits added.

    Short premium economics: the position is OPENED by collecting
    a credit (premium received). It WINS as the premium decays
    (theta) and we keep more of the credit. It LOSES when the
    premium expands against us (price moved into our short strike).

    Asymmetric thresholds:
      - Take profit at -50% premium drop (lock in 50% of credit)
      - Stop loss at +100% premium expansion (cut at 1× credit
        risk)
    """

    def test_short_premium_decay_triggers_take_profit(self, db_path):
        """Premium dropped 50% — short side wins (theta works)."""
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "OPTIONS")
        # Short call at $2.40 entry, premium decayed to $1.20
        pos = _option_position(occ, qty=-1, entry=2.40, current=1.20)
        signals = check_single_leg_option_exits([pos], db_path)
        assert len(signals) == 1
        assert signals[0]["trigger"] == "short_premium_take_profit"
        # side_to_close: short → buy-to-close
        assert signals[0]["side_to_close"] == "buy"
        assert signals[0]["qty"] == 1

    def test_short_premium_45pct_drop_does_not_trigger(self, db_path):
        """Just inside the threshold — must NOT fire."""
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "OPTIONS")
        # 2.40 → 1.32 = -45%
        pos = _option_position(occ, qty=-1, entry=2.40, current=1.32)
        signals = check_single_leg_option_exits([pos], db_path)
        assert signals == []

    def test_short_premium_expansion_triggers_stop(self, db_path):
        """Premium doubled against us — short side stops out."""
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "OPTIONS")
        # Short call at $2.40 entry, ran to $4.80 (+100%)
        pos = _option_position(occ, qty=-1, entry=2.40, current=4.80)
        signals = check_single_leg_option_exits([pos], db_path)
        assert len(signals) == 1
        assert signals[0]["trigger"] == "short_premium_stop"
        assert signals[0]["side_to_close"] == "buy"

    def test_short_premium_90pct_expansion_does_not_trigger(self, db_path):
        """Just inside the stop threshold — must NOT fire."""
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "OPTIONS")
        # 2.40 → 4.56 = +90%
        pos = _option_position(occ, qty=-1, entry=2.40, current=4.56)
        signals = check_single_leg_option_exits([pos], db_path)
        assert signals == []

    def test_short_dte_exit_uses_buy_to_close(self, db_path):
        """Short positions near expiry also close via DTE exit
        (gamma blowup risk applies regardless of side). side_to_close
        must be buy (closing a short)."""
        occ = _occ(expiry_days=5)
        _log_open_trade(db_path, occ, "OPTIONS")
        pos = _option_position(occ, qty=-1, entry=2.40, current=2.40)
        signals = check_single_leg_option_exits([pos], db_path)
        assert len(signals) == 1
        assert signals[0]["trigger"] == "dte_exit"
        assert signals[0]["side_to_close"] == "buy"

    def test_short_multileg_leg_still_skipped(self, db_path):
        """The multileg-skip safety still applies to shorts —
        independently closing one leg of a spread orphans its
        partner regardless of leg direction."""
        occ = _occ(expiry_days=32)
        _log_open_trade(db_path, occ, "MULTILEG")
        # Short leg of a spread — even with -91% premium drop
        # (massive theta win), do NOT close independently
        pos = _option_position(occ, qty=-1, entry=2.40, current=0.20)
        signals = check_single_leg_option_exits([pos], db_path)
        assert signals == [], (
            "Short multileg leg with -91% drop must NOT trigger "
            "independent close (would orphan partner)"
        )


# ---------------------------------------------------------------------------
# Stock positions ignored (only option positions evaluated)
# ---------------------------------------------------------------------------

class TestStockPositionsIgnored:
    def test_stock_positions_not_evaluated(self, db_path):
        positions = [_stock_position()]
        signals = check_single_leg_option_exits(positions, db_path)
        assert signals == []

    def test_mixed_book(self, db_path):
        """Stock + option positions in the same call — only option
        is evaluated."""
        opt_occ = _occ(expiry_days=32)
        _log_open_trade(db_path, opt_occ, "OPTIONS")
        positions = [
            _stock_position(symbol="AAPL"),
            _option_position(opt_occ, entry=2.40, current=1.20),
        ]
        signals = check_single_leg_option_exits(positions, db_path)
        assert len(signals) == 1
        assert signals[0]["occ_symbol"] == opt_occ


# ---------------------------------------------------------------------------
# submit_option_close payload shape
# ---------------------------------------------------------------------------

class TestSubmitOptionClosePayload:
    def test_payload_includes_sell_to_close_intent(self):
        captured = {}

        def fake_post(api, payload):
            captured["payload"] = payload
            mock = MagicMock()
            mock.id = "order-xyz"
            return mock

        with patch(
            "options_multileg._submit_alpaca_order_raw",
            side_effect=fake_post,
        ):
            result = submit_option_close(
                api=None,
                occ_symbol="AAPL  260612C00150000",
                qty=1,
                side_to_close="sell",
            )

        payload = captured["payload"]
        assert payload["position_intent"] == "sell_to_close"
        assert payload["side"] == "sell"
        assert payload["qty"] == 1
        assert payload["type"] == "market"
        # OCC symbol unpadded for Alpaca
        assert payload["symbol"] == "AAPL260612C00150000"
        assert result["status"] == "submitted"
        assert result["order_id"] == "order-xyz"

    def test_payload_uses_limit_when_price_supplied(self):
        captured = {}

        def fake_post(api, payload):
            captured["payload"] = payload
            mock = MagicMock()
            mock.id = "order-xyz"
            return mock

        with patch(
            "options_multileg._submit_alpaca_order_raw",
            side_effect=fake_post,
        ):
            submit_option_close(
                api=None, occ_symbol="AAPL  260612C00150000",
                qty=2, side_to_close="sell", limit_price=2.40,
            )

        assert captured["payload"]["type"] == "limit"
        assert captured["payload"]["limit_price"] == 2.40

    def test_failure_returns_error_dict(self):
        with patch(
            "options_multileg._submit_alpaca_order_raw",
            side_effect=RuntimeError("Alpaca rejected"),
        ):
            result = submit_option_close(
                api=None, occ_symbol="AAPL  260612C00150000",
                qty=1,
            )
        assert result["status"] == "failed"
        assert result["action"] == "ERROR"
        assert "rejected" in result["reason"].lower()


# ---------------------------------------------------------------------------
# THRESHOLD CONSTANTS pinned (catches accidental loosening)
# ---------------------------------------------------------------------------

class TestThresholdConstants:
    def test_premium_stop_loss_is_50pct(self):
        assert PREMIUM_STOP_LOSS_PCT == -0.50

    def test_premium_take_profit_is_100pct(self):
        assert PREMIUM_TAKE_PROFIT_PCT == 1.00

    def test_dte_threshold_is_7_days(self):
        assert DTE_EXIT_THRESHOLD_DAYS == 7

    def test_short_take_profit_is_negative_50pct(self):
        """Short wins on premium decay (negative pct_change)."""
        assert SHORT_PREMIUM_TAKE_PROFIT_PCT == -0.50

    def test_short_stop_loss_is_positive_100pct(self):
        """Short loses on premium expansion."""
        assert SHORT_PREMIUM_STOP_LOSS_PCT == 1.00
