"""Tests for options_lifecycle.sweep_expired_options.

Item 1a follow-up: when option contracts expire, the open trade rows
become stale unless we sweep. These tests cover:
  - Open option trades with expiry < today → marked closed worthless
  - Long worthless: P&L = -premium × 100 × contracts
  - Short worthless: P&L = +premium × 100 × contracts
  - Broker still holding position → flagged "assigned" / status=needs_review
  - Future-expiry rows ignored
  - Non-OPTIONS rows ignored
  - Empty journal → empty summary, no crash
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_db():
    """Spin up a fresh trades-table journal DB for each test."""
    from journal import init_db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _seed_open_option(db_path, **overrides):
    """Insert one open OPTIONS row with sensible defaults."""
    from journal import log_trade
    return log_trade(
        symbol=overrides.get("symbol", "AAPL"),
        side=overrides.get("side", "buy"),
        qty=overrides.get("qty", 1),
        price=overrides.get("price", 2.50),
        order_id=overrides.get("order_id", "test-1"),
        signal_type="OPTIONS",
        strategy=overrides.get("option_strategy", "long_call"),
        decision_price=overrides.get("decision_price", 2.50),
        occ_symbol=overrides.get("occ_symbol", "AAPL  990115C00150000"),
        option_strategy=overrides.get("option_strategy", "long_call"),
        expiry=overrides.get("expiry", "1999-01-15"),  # default = past
        strike=overrides.get("strike", 150.0),
        db_path=db_path,
    )


class TestFindExpiredOpenOptions:
    def test_finds_expired_open_option(self, tmp_db):
        from options_lifecycle import find_expired_open_options
        _seed_open_option(tmp_db, expiry="2020-01-15")
        rows = find_expired_open_options(tmp_db, today=date(2026, 5, 1))
        assert len(rows) == 1
        assert rows[0]["expiry"] == "2020-01-15"

    def test_skips_future_expiry(self, tmp_db):
        from options_lifecycle import find_expired_open_options
        _seed_open_option(tmp_db, expiry="2099-01-15")
        rows = find_expired_open_options(tmp_db, today=date(2026, 5, 1))
        assert rows == []

    def test_skips_non_options_rows(self, tmp_db):
        """Equity SELL rows for closed positions shouldn't show up."""
        from journal import log_trade
        log_trade(symbol="MSFT", side="sell", qty=100, price=400,
                  signal_type="EXIT", db_path=tmp_db)
        from options_lifecycle import find_expired_open_options
        rows = find_expired_open_options(tmp_db, today=date(2026, 5, 1))
        assert rows == []

    def test_skips_already_closed_options(self, tmp_db):
        from journal import _get_conn
        _seed_open_option(tmp_db, expiry="2020-01-15")
        conn = _get_conn(tmp_db)
        conn.execute("UPDATE trades SET status='closed' WHERE id=1")
        conn.commit()
        from options_lifecycle import find_expired_open_options
        rows = find_expired_open_options(tmp_db, today=date(2026, 5, 1))
        assert rows == []


class TestSweepExpiredOptions:
    def test_long_call_expired_worthless_realizes_loss(self, tmp_db):
        """Long call, paid $2.50 × 100 × 1 = $250, expires worthless."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="buy", qty=1, decision_price=2.50,
                          option_strategy="long_call", expiry="2020-01-15",
                          strike=150.0)
        api = MagicMock()
        api.list_positions.return_value = []  # broker no longer holds
        # Underlying close BELOW the 150 strike → call OTM at expiry
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=140.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["expired_found"] == 1
        assert result["closed_worthless"] == 1
        assert result["assigned"] == 0

        # Verify the row was updated
        from journal import _get_conn
        conn = _get_conn(tmp_db)
        row = conn.execute("SELECT status, pnl FROM trades WHERE id=1").fetchone()
        assert row[0] == "closed"
        assert row[1] == pytest.approx(-250.0)

    def test_short_call_expired_worthless_realizes_gain(self, tmp_db):
        """Covered call sold for $1.50, expires OTM (close < strike)
        → keep premium, +$150."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="sell", qty=1, decision_price=1.50,
                          option_strategy="covered_call", expiry="2020-01-15",
                          strike=160.0)
        api = MagicMock()
        api.list_positions.return_value = []
        # Underlying closed BELOW 160 strike → short call OTM
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=150.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["closed_worthless"] == 1

        from journal import _get_conn
        conn = _get_conn(tmp_db)
        row = conn.execute("SELECT pnl FROM trades WHERE id=1").fetchone()
        assert row[0] == pytest.approx(150.0)

    def test_broker_still_holds_position_when_close_unavailable(self, tmp_db):
        """If broker still shows a position AND we can't fetch the
        underlying close, mark needs_review (can't determine ITM/OTM)."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="sell",
                          option_strategy="cash_secured_put",
                          occ_symbol="AAPL  990115P00140000",
                          expiry="2020-01-15", strike=140.0)
        api = MagicMock()
        broker_pos = MagicMock()
        broker_pos.symbol = "AAPL  990115P00140000"
        broker_pos.qty = "1"
        broker_pos.avg_entry_price = "1.00"
        broker_pos.market_value = "0"
        api.list_positions.return_value = [broker_pos]
        # No underlying close available → fallback to broker check
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=None):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["needs_review"] == 1
        assert result["closed_worthless"] == 0

        from journal import _get_conn
        conn = _get_conn(tmp_db)
        row = conn.execute(
            "SELECT status, pnl, reason FROM trades WHERE id=1"
        ).fetchone()
        assert row[0] == "needs_review"
        assert row[1] is None
        assert "assignment likely" in row[2].lower()

    def test_multiple_contracts_scales_pnl(self, tmp_db):
        """3 contracts of $2.00 long put → -$600 worthless when OTM."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="buy", qty=3, decision_price=2.00,
                          option_strategy="long_put", expiry="2020-01-15",
                          strike=150.0)
        api = MagicMock()
        api.list_positions.return_value = []
        # Underlying close ABOVE 150 strike → put OTM
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=160.0):
            sweep_expired_options(api, db_path=tmp_db,
                                     today=date(2026, 5, 1))

        from journal import _get_conn
        conn = _get_conn(tmp_db)
        row = conn.execute("SELECT pnl FROM trades WHERE id=1").fetchone()
        assert row[0] == pytest.approx(-600.0)

    def test_empty_journal_returns_empty_summary(self, tmp_db):
        from options_lifecycle import sweep_expired_options
        api = MagicMock()
        result = sweep_expired_options(api, db_path=tmp_db,
                                          today=date(2026, 5, 1))
        assert result["expired_found"] == 0
        assert result["closed_worthless"] == 0
        assert result["errors"] == 0
        api.list_positions.assert_not_called()

    def test_broker_failure_doesnt_crash_sweep(self, tmp_db):
        """list_positions raising shouldn't take out the sweep — just
        log and continue. The trade stays open for the next pass."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, expiry="2020-01-15", strike=150.0)
        api = MagicMock()
        api.list_positions.side_effect = Exception("alpaca down")
        # No close available either → falls back to conservative
        # worthless treatment with note
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=None):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["expired_found"] == 1
        # Either closed-worthless (best-effort) or error. Don't crash.
        assert result["closed_worthless"] + result["errors"] == 1


class TestAssignmentDetection:
    """Phase C2 — short option ITM at expiry → assigned, with
    synthetic equity leg logged."""

    def test_short_call_itm_at_expiry_assigns_called_away(self, tmp_db):
        """Covered call at strike 150, underlying closes 155 (ITM).
        → called away. Premium realized; synthetic SELL equity leg
        logged for 100 shares at $150."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="sell", qty=1, decision_price=2.00,
                          option_strategy="covered_call",
                          occ_symbol="AAPL  200115C00150000",
                          expiry="2020-01-15", strike=150.0)
        api = MagicMock()
        api.list_positions.return_value = []
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=155.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["assigned"] == 1
        assert result["equity_legs_logged"] == 1

        # Verify option row updated and equity leg created
        from journal import _get_conn
        conn = _get_conn(tmp_db)
        rows = conn.execute(
            "SELECT signal_type, side, qty, price FROM trades ORDER BY id"
        ).fetchall()
        assert len(rows) == 2  # original option + synthetic equity leg
        # Equity leg: SELL 100 shares at strike $150
        equity = rows[1]
        assert equity[0] == "OPTION_EXERCISE"
        assert equity[1] == "sell"
        assert equity[2] == 100
        assert equity[3] == 150.0

    def test_short_put_itm_at_expiry_assigns_buy_shares(self, tmp_db):
        """CSP at strike 140, underlying closes 135 (ITM put).
        → assigned. Synthetic BUY equity leg for 100 shares at $140."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="sell", qty=2, decision_price=1.00,
                          option_strategy="cash_secured_put",
                          occ_symbol="AAPL  200115P00140000",
                          expiry="2020-01-15", strike=140.0)
        api = MagicMock()
        api.list_positions.return_value = []
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=135.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["assigned"] == 1
        assert result["equity_legs_logged"] == 1

        from journal import _get_conn
        conn = _get_conn(tmp_db)
        rows = conn.execute(
            "SELECT side, qty, price FROM trades "
            "WHERE signal_type='OPTION_EXERCISE'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "buy"
        assert rows[0][1] == 200  # 2 contracts × 100
        assert rows[0][2] == 140.0


class TestExerciseDetection:
    """Long option ITM at expiry → exercised, with synthetic equity
    leg logged."""

    def test_long_call_itm_at_expiry_exercised(self, tmp_db):
        """Long call strike $150 paid $2; underlying closes $158.
        Intrinsic = $8. P&L = (8 - 2) * 100 = +$600."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="buy", qty=1, decision_price=2.00,
                          option_strategy="long_call",
                          occ_symbol="AAPL  200115C00150000",
                          expiry="2020-01-15", strike=150.0)
        api = MagicMock()
        api.list_positions.return_value = []
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=158.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["exercised"] == 1
        assert result["equity_legs_logged"] == 1

        from journal import _get_conn
        conn = _get_conn(tmp_db)
        # Original option row gets P&L = (intrinsic - premium) * 100
        opt = conn.execute(
            "SELECT pnl FROM trades WHERE signal_type='OPTIONS'"
        ).fetchone()
        assert opt[0] == pytest.approx(600.0)
        # Synthetic equity leg: BUY 100 at strike 150
        eq = conn.execute(
            "SELECT side, qty, price FROM trades "
            "WHERE signal_type='OPTION_EXERCISE'"
        ).fetchone()
        assert eq[0] == "buy"
        assert eq[1] == 100
        assert eq[2] == 150.0

    def test_long_put_itm_at_expiry_exercised(self, tmp_db):
        """Long put strike $150 paid $3; underlying closes $140.
        Intrinsic = $10. P&L = (10 - 3) * 100 = +$700."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="buy", qty=1, decision_price=3.00,
                          option_strategy="long_put",
                          occ_symbol="AAPL  200115P00150000",
                          expiry="2020-01-15", strike=150.0)
        api = MagicMock()
        api.list_positions.return_value = []
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=140.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["exercised"] == 1
        # Synthetic equity leg: SELL 100 at strike 150
        from journal import _get_conn
        conn = _get_conn(tmp_db)
        eq = conn.execute(
            "SELECT side, qty, price FROM trades "
            "WHERE signal_type='OPTION_EXERCISE'"
        ).fetchone()
        assert eq[0] == "sell"
