"""Tests for activities_capture (#168, 2026-05-17).

Capture non-trade Alpaca account activities (dividends, option
expiration / assignment / exercise) into the per-profile journal
so broker_cash and broker_value parity audits don't false-flag
on legitimate broker events.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_profile_db(tmp_path, pid):
    """Profile DB with the production trades schema (enough columns
    that log_trade can write)."""
    db = tmp_path / f"quantopsai_profile_{pid}.db"
    with sqlite3.connect(db) as conn:
        # Match the production trades schema closely enough for
        # log_trade's INSERT to succeed.
        conn.executescript("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL,
                order_id TEXT,
                signal_type TEXT,
                strategy TEXT,
                reason TEXT,
                ai_reasoning TEXT,
                ai_confidence REAL,
                stop_loss REAL,
                take_profit REAL,
                status TEXT DEFAULT 'open',
                pnl REAL,
                decision_price REAL,
                fill_price REAL,
                slippage_pct REAL,
                occ_symbol TEXT,
                option_strategy TEXT,
                expiry TEXT,
                strike REAL,
                predicted_slippage_bps REAL,
                adv_at_decision REAL
            );
        """)
    return str(db)


def _ctx(profile_id, db_path, api):
    return SimpleNamespace(
        profile_id=profile_id,
        db_path=db_path,
        api=api,
        display_name=f"P{profile_id}",
    )


def _div_activity(aid, symbol, amount, date="2026-05-17"):
    return SimpleNamespace(
        id=aid, activity_type="DIV", symbol=symbol,
        net_amount=amount, amount=amount, date=date,
    )


def _opasn_activity(aid, occ, qty, price, date="2026-05-17"):
    return SimpleNamespace(
        id=aid, activity_type="OPASN", symbol=occ,
        qty=qty, price=price, date=date,
    )


def _opexp_activity(aid, occ, qty, price=0.0):
    return SimpleNamespace(
        id=aid, activity_type="OPEXP", symbol=occ,
        qty=qty, price=price, date="2026-05-17",
    )


# ─────────────────────────────────────────────────────────────────────
# Dividend capture
# ─────────────────────────────────────────────────────────────────────

class TestDividendCapture:
    def test_dividend_writes_journal_row(self, tmp_path):
        from activities_capture import capture_activities_for_profile
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        api.get_activities.return_value = [
            _div_activity("act-div-1", "AAPL", 50.0),
        ]
        ctx = _ctx(1, db, api)
        summary = capture_activities_for_profile(ctx)
        assert summary["DIV"] == 1
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT symbol, side, qty, price, signal_type, order_id "
                "FROM trades WHERE order_id = ?", ("act-div-1",)
            ).fetchone()
        assert row is not None
        assert row[0] == "AAPL"
        assert row[1] == "dividend"
        assert row[2] == 1.0
        assert row[3] == 50.0
        assert row[4] == "DIVIDEND"

    def test_dividend_idempotent(self, tmp_path):
        """Re-capture must not double-insert the same activity."""
        from activities_capture import capture_activities_for_profile
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        api.get_activities.return_value = [
            _div_activity("act-div-2", "MSFT", 25.0),
        ]
        ctx = _ctx(1, db, api)
        capture_activities_for_profile(ctx)
        # Second call: same activity returned again
        capture_activities_for_profile(ctx)
        with sqlite3.connect(db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE order_id = ?",
                ("act-div-2",),
            ).fetchone()[0]
        assert count == 1


class TestDividendCreditsVirtualCash:
    """End-to-end: a captured dividend must flow into get_virtual_account_info."""

    def test_dividend_credits_cash(self, tmp_path):
        from activities_capture import capture_activities_for_profile
        from journal import get_virtual_account_info
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        api.get_activities.return_value = [
            _div_activity("act-div-3", "AAPL", 100.0),
        ]
        ctx = _ctx(1, db, api)
        capture_activities_for_profile(ctx)
        info = get_virtual_account_info(
            db_path=db, initial_capital=100_000.0,
        )
        # initial 100k + $100 dividend = $100,100 cash, no positions
        assert info["cash"] == 100_100.0
        assert info["portfolio_value"] == 0.0
        assert info["equity"] == 100_100.0


# ─────────────────────────────────────────────────────────────────────
# Option assignment / expiration
# ─────────────────────────────────────────────────────────────────────

class TestOptionEventCapture:
    def test_option_assignment_writes_close_row(self, tmp_path):
        from activities_capture import capture_activities_for_profile
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        api.get_activities.return_value = [
            _opasn_activity(
                "act-opasn-1", "AAPL260618C00200000", qty=5, price=2.50,
            ),
        ]
        ctx = _ctx(1, db, api)
        summary = capture_activities_for_profile(ctx)
        assert summary["OPASN"] == 1
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT symbol, side, qty, price, occ_symbol, signal_type "
                "FROM trades WHERE order_id = ?", ("act-opasn-1",)
            ).fetchone()
        # symbol extracted as underlying, occ_symbol preserved
        assert row[0] == "AAPL"
        assert row[1] == "sell"
        assert row[2] == 5.0
        assert row[3] == 2.50
        assert row[4] == "AAPL260618C00200000"
        assert row[5] == "OPASN"

    def test_option_expiration_writes_close_row_at_zero(self, tmp_path):
        from activities_capture import capture_activities_for_profile
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        api.get_activities.return_value = [
            _opexp_activity("act-opexp-1", "AAPL260618C00500000",
                            qty=10, price=0.0),
        ]
        ctx = _ctx(1, db, api)
        summary = capture_activities_for_profile(ctx)
        assert summary["OPEXP"] == 1
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT signal_type, price, qty FROM trades "
                "WHERE order_id = ?", ("act-opexp-1",)
            ).fetchone()
        assert row[0] == "OPEXP"
        assert row[1] == 0.0
        assert row[2] == 10.0


# ─────────────────────────────────────────────────────────────────────
# Error handling / API failures
# ─────────────────────────────────────────────────────────────────────

class TestAlpacaFieldContract:
    """Pin the Alpaca NonTradeActivity field-name contract so the
    speculative `amount` fallback (or any other guess) can't recur.

    Verified 2026-05-17 against Alpaca's docs at
    https://docs.alpaca.markets/docs/account-activities :
      DIV   uses `net_amount` (NOT `amount`)
      OPEXP/OPASN/OPXRC use `symbol` (carrying OCC) + `qty`
      `price` is NOT documented for NonTradeActivity — code must
            tolerate it being absent.
    """

    def test_dividend_requires_net_amount_field(self, tmp_path):
        """DIV without `net_amount` → skip + WARN, do NOT silently
        fall back to a guessed field name. The cash-parity audit
        catches the resulting drift."""
        from activities_capture import capture_activities_for_profile
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        # Activity with `amount` set but NO `net_amount` — earlier
        # speculative fallback would have read this; current code
        # must skip + warn.
        no_net_amount = SimpleNamespace(
            id="div-noamount-1", activity_type="DIV",
            symbol="AAPL", amount=50.0,  # NOT net_amount
            date="2026-05-17",
        )
        api.get_activities.return_value = [no_net_amount]
        ctx = _ctx(1, db, api)
        with patch("activities_capture.logger") as fake_log:
            summary = capture_activities_for_profile(ctx)
        assert summary["DIV"] == 0
        # Must have warned (loud, not silent)
        fake_log.warning.assert_called()

    def test_option_event_missing_symbol_field_warns(self, tmp_path):
        """OPEXP without `symbol` → skip + WARN. Without the OCC
        symbol we can't write a meaningful close row."""
        from activities_capture import capture_activities_for_profile
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        no_symbol = SimpleNamespace(
            id="opexp-nosym-1", activity_type="OPEXP",
            symbol="", qty=5, date="2026-05-17",
        )
        api.get_activities.return_value = [no_symbol]
        ctx = _ctx(1, db, api)
        with patch("activities_capture.logger") as fake_log:
            summary = capture_activities_for_profile(ctx)
        assert summary["OPEXP"] == 0
        fake_log.warning.assert_called()

    def test_option_event_with_no_price_field_uses_zero(self, tmp_path):
        """`price` is NOT a documented NTA field. When absent, code
        must default to 0 — which is the correct close-out price
        for OPEXP (worthless) and harmless for OPASN/OPXRC (cash
        movement comes via the separate FILL activity)."""
        from activities_capture import capture_activities_for_profile
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        # OPASN with NO `price` field at all
        no_price = SimpleNamespace(
            id="opasn-noprice-1", activity_type="OPASN",
            symbol="AAPL260618C00200000", qty=5, date="2026-05-17",
        )
        api.get_activities.return_value = [no_price]
        ctx = _ctx(1, db, api)
        summary = capture_activities_for_profile(ctx)
        assert summary["OPASN"] == 1
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT price, occ_symbol FROM trades "
                "WHERE order_id = ?", ("opasn-noprice-1",)
            ).fetchone()
        assert row[0] == 0.0  # price defaulted to 0
        assert row[1] == "AAPL260618C00200000"


class TestErrorHandling:
    def test_api_failure_returns_empty_summary(self, tmp_path):
        from activities_capture import capture_activities_for_profile
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        api.get_activities.side_effect = OSError("network down")
        ctx = _ctx(1, db, api)
        summary = capture_activities_for_profile(ctx)
        assert summary == {"DIV": 0, "OPEXP": 0, "OPASN": 0, "OPXRC": 0}

    def test_activity_without_id_skipped(self, tmp_path):
        from activities_capture import capture_activities_for_profile
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        # No id field — log_trade can't dedupe so we refuse to write
        bad = SimpleNamespace(
            activity_type="DIV", symbol="AAPL", net_amount=10.0,
            amount=10.0, date="2026-05-17",
        )
        # Add an `id` attribute so SimpleNamespace doesn't raise on getattr;
        # leave it empty so the capture skips.
        bad.id = ""
        api.get_activities.return_value = [bad]
        ctx = _ctx(1, db, api)
        summary = capture_activities_for_profile(ctx)
        assert summary["DIV"] == 0

    def test_unhandled_activity_type_ignored(self, tmp_path):
        """Alpaca returns activity_types we don't handle (e.g. JNLC) —
        ignore silently rather than raising."""
        from activities_capture import capture_activities_for_profile
        db = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        api.get_activities.return_value = [
            SimpleNamespace(id="jnlc-1", activity_type="JNLC",
                            symbol="", net_amount=0, amount=0,
                            date="2026-05-17"),
        ]
        ctx = _ctx(1, db, api)
        summary = capture_activities_for_profile(ctx)
        assert all(v == 0 for v in summary.values())


# ─────────────────────────────────────────────────────────────────────
# Multi-profile wrapper
# ─────────────────────────────────────────────────────────────────────

class TestBatchCapture:
    def test_capture_for_all_profiles_handles_load_failure(self, tmp_path):
        from activities_capture import capture_activities_for_all_profiles
        db1 = _make_profile_db(tmp_path, 1)
        api = MagicMock()
        api.get_activities.return_value = [
            _div_activity("act-div-batch-1", "AAPL", 30.0),
        ]
        ctx1 = _ctx(1, db1, api)

        def _build(pid):
            if pid == 1:
                return ctx1
            raise ValueError(f"no profile {pid}")

        with patch(
            "models.build_user_context_from_profile", side_effect=_build,
        ):
            result = capture_activities_for_all_profiles([1, 2])
        assert 1 in result
        assert 2 not in result
        assert result[1]["DIV"] == 1
