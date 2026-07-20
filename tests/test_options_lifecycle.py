"""Tests for options_lifecycle.sweep_expired_options.

2026-07-20 — REWRITTEN for the broker-truth expiry contract (the
opex kill-switch incident). Outcomes now come from the broker's
OPASN/OPXRC/OPEXP activity stream, never from bar-close estimates:
  - OTM + contract gone from broker → closed worthless (no shares
    move, so the estimate is safe)
  - broker OPASN/OPXRC → assigned/exercised + equity leg sized from
    the activity, idempotent, stock entries flipped coherently
  - ITM (or unknown) with NO broker activity → DEFERRED, then
    needs_review after the grace window. NEVER a fabricated leg.
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


def _seed_stock_long(db_path, symbol="AAPL", qty=100, price=140.0,
                     order_id="stk-1"):
    from journal import log_trade
    return log_trade(symbol=symbol, side="buy", qty=qty, price=price,
                     order_id=order_id, signal_type="AI_ANALYSIS",
                     status="open", fill_price=price, db_path=db_path)


def _activity(a_type, occ, qty=1, aid="act-1"):
    a = MagicMock()
    a.activity_type = a_type
    a.symbol = occ
    a.qty = qty
    a.id = aid
    return a


def _api(positions=(), activities=()):
    """Broker mock: list_positions + get_activities both stubbed so
    no test leaks a MagicMock iterable into the sweep. get_activities
    filters by the requested type, modeling the per-type fetch the
    real Alpaca API requires (comma-joined type strings are rejected;
    see test_timestop_cover_journaling_2026_07_20)."""
    api = MagicMock()
    api.list_positions.return_value = list(positions)
    acts = list(activities)
    api.get_activities.side_effect = (
        lambda activity_types=None, after=None:
        [a for a in acts if a.activity_type == activity_types])
    return api


def _stock_virtual(db_path):
    from journal import get_virtual_positions
    return {p["symbol"]: p["qty"]
            for p in get_virtual_positions(db_path=db_path,
                                           price_fetcher=lambda s: 100.0)
            if not p.get("occ_symbol")}


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


class TestOccRightParsing:
    """The 2026-07-20 bug: right was read at occ[12], which is only
    the right letter for OSI space-padded 6-char roots. Journal rows
    carry BOTH forms (format_occ_symbol emits padded; submit paths
    snap to Alpaca's compact form)."""

    def test_padded_and_compact_forms_agree(self):
        from options_lifecycle import _occ_right
        padded = {"occ_symbol": "GOOG  260717C00180000"}
        compact = {"occ_symbol": "GOOG260717C00180000"}
        assert _occ_right(padded) == "C"
        assert _occ_right(compact) == "C"
        assert _occ_right(
            {"occ_symbol": "CVX260717P00150000"}) == "P"

    def test_falls_back_to_option_strategy(self):
        from options_lifecycle import _occ_right
        assert _occ_right({"occ_symbol": "",
                           "option_strategy": "covered_call"}) == "C"
        assert _occ_right({"occ_symbol": None,
                           "option_strategy": "cash_secured_put"}) == "P"


class TestSweepExpiredOptions:
    def test_long_call_expired_worthless_realizes_loss(self, tmp_db):
        """Long call, paid $2.50 × 100 × 1 = $250, expires worthless."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="buy", qty=1, decision_price=2.50,
                          option_strategy="long_call", expiry="2020-01-15",
                          strike=150.0)
        api = _api()
        # Underlying close BELOW the 150 strike → call OTM at expiry
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=140.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["expired_found"] == 1
        assert result["closed_worthless"] == 1
        assert result["assigned"] == 0

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
        api = _api()
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=150.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["closed_worthless"] == 1

        from journal import _get_conn
        conn = _get_conn(tmp_db)
        row = conn.execute("SELECT pnl FROM trades WHERE id=1").fetchone()
        assert row[0] == pytest.approx(150.0)

    def test_broker_still_holds_needs_review_after_grace(self, tmp_db):
        """Broker still shows the contract, no close, no activities,
        far past the grace window → needs_review (never guessed)."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="sell",
                          option_strategy="cash_secured_put",
                          occ_symbol="AAPL  990115P00140000",
                          expiry="2020-01-15", strike=140.0)
        broker_pos = MagicMock()
        broker_pos.symbol = "AAPL  990115P00140000"
        broker_pos.qty = "1"
        broker_pos.avg_entry_price = "1.00"
        broker_pos.market_value = "0"
        api = _api(positions=[broker_pos])
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=None):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["needs_review"] == 1
        assert result["closed_worthless"] == 0
        assert result["equity_legs_logged"] == 0

        from journal import _get_conn
        conn = _get_conn(tmp_db)
        row = conn.execute(
            "SELECT status, pnl FROM trades WHERE id=1"
        ).fetchone()
        assert row[0] == "needs_review"
        assert row[1] is None

    def test_multiple_contracts_scales_pnl(self, tmp_db):
        """3 contracts of $2.00 long put → -$600 worthless when OTM."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="buy", qty=3, decision_price=2.00,
                          option_strategy="long_put",
                          occ_symbol="AAPL  200115P00150000",
                          expiry="2020-01-15", strike=150.0)
        api = _api()
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
        api = _api()
        result = sweep_expired_options(api, db_path=tmp_db,
                                          today=date(2026, 5, 1))
        assert result["expired_found"] == 0
        assert result["closed_worthless"] == 0
        assert result["errors"] == 0
        api.list_positions.assert_not_called()

    def test_broker_failure_defers_instead_of_guessing(self, tmp_db):
        """list_positions AND get_activities raising must not crash
        the sweep — and must NOT close anything on guesswork. Past
        the grace window the row goes to needs_review."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, expiry="2020-01-15", strike=150.0)
        api = MagicMock()
        api.list_positions.side_effect = Exception("alpaca down")
        api.get_activities.side_effect = Exception("alpaca down")
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=None):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2026, 5, 1))
        assert result["expired_found"] == 1
        assert result["errors"] == 0
        assert result["needs_review"] == 1
        assert result["closed_worthless"] == 0
        assert result["equity_legs_logged"] == 0


class TestBrokerTruthInvariant:
    """The 2026-07-20 opex incident pins: an ITM estimate must never
    fabricate a share movement. Only OPASN/OPXRC broker activities
    may book equity legs."""

    OCC = "GOOG  260717C00180000"
    OCC_COMPACT = "GOOG260717C00180000"

    def _seed_incident(self, db, occ=None):
        """The literal incident shape: 75 shares long, 1 short call
        struck 180, expired Fri 2026-07-17, closed $185 (ITM)."""
        _seed_stock_long(db, symbol="GOOG", qty=75, price=170.0)
        _seed_open_option(db, symbol="GOOG", side="sell", qty=1,
                          decision_price=1.50,
                          option_strategy="covered_call",
                          occ_symbol=occ or self.OCC,
                          expiry="2026-07-17", strike=180.0)

    def test_itm_without_opasn_defers_no_phantom_short(self, tmp_db):
        """Monday after opex, broker flat, NO OPASN activity → the row
        DEFERS. No equity leg, no phantom short, journal unchanged."""
        from options_lifecycle import sweep_expired_options
        self._seed_incident(tmp_db)
        api = _api()  # broker flat, zero activities
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=185.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                           today=date(2026, 7, 20))
        assert result["deferred"] == 1
        assert result["assigned"] == 0
        assert result["equity_legs_logged"] == 0
        # THE pin: virtual book still shows the real 75-share long —
        # no fabricated -25 short (the kill-switch banner numbers).
        assert _stock_virtual(tmp_db) == {"GOOG": 75.0}
        # Row still open for the next pass
        conn = sqlite3.connect(tmp_db)
        status = conn.execute(
            "SELECT status FROM trades WHERE occ_symbol IS NOT NULL"
        ).fetchone()[0]
        assert status == "open"

    def test_itm_without_opasn_needs_review_after_grace(self, tmp_db):
        from options_lifecycle import (sweep_expired_options,
                                       ASSIGNMENT_CONFIRM_GRACE_DAYS)
        self._seed_incident(tmp_db)
        api = _api()
        late = date(2026, 7, 17) + timedelta(
            days=ASSIGNMENT_CONFIRM_GRACE_DAYS + 1)
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=185.0):
            result = sweep_expired_options(api, db_path=tmp_db, today=late)
        assert result["needs_review"] == 1
        assert result["equity_legs_logged"] == 0
        assert _stock_virtual(tmp_db) == {"GOOG": 75.0}

    def test_opasn_books_called_away_leg_and_flips_entries(self, tmp_db):
        """With the broker's OPASN activity the assignment books: SELL
        100 @ strike (activity truth), the fully-consumed 75-share
        entry flips closed, and the residual -25 mirrors the broker's
        own under-covered assignment short."""
        from options_lifecycle import sweep_expired_options
        self._seed_incident(tmp_db)
        api = _api(activities=[_activity("OPASN", self.OCC_COMPACT, 1)])
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=185.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                           today=date(2026, 7, 20))
        assert result["assigned"] == 1
        assert result["equity_legs_logged"] == 1

        conn = sqlite3.connect(tmp_db)
        leg = conn.execute(
            "SELECT side, qty, price, status, order_id FROM trades "
            "WHERE signal_type='OPTION_EXERCISE'").fetchone()
        assert leg[0] == "sell"
        assert leg[1] == 100
        assert leg[2] == 180.0
        assert leg[3] == "closed"
        assert leg[4].startswith("OPX-EQ-")
        # Consumed stock entry flipped closed (reconciler coherence)
        entry = conn.execute(
            "SELECT status FROM trades WHERE side='buy' "
            "AND occ_symbol IS NULL").fetchone()
        assert entry[0] == "closed"
        # Net book matches the broker's post-assignment reality
        assert _stock_virtual(tmp_db) == {"GOOG": -25.0}

    def test_opasn_booking_is_idempotent(self, tmp_db):
        """Re-sweeping must never double-book the equity leg."""
        from options_lifecycle import sweep_expired_options
        self._seed_incident(tmp_db)
        api = _api(activities=[_activity("OPASN", self.OCC_COMPACT, 1)])
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=185.0):
            sweep_expired_options(api, db_path=tmp_db,
                                  today=date(2026, 7, 20))
            # Force the option row back open to simulate a crash
            # between the row UPDATE and a retried pass.
            conn = sqlite3.connect(tmp_db)
            conn.execute("UPDATE trades SET status='open' "
                         "WHERE occ_symbol IS NOT NULL")
            conn.commit()
            conn.close()
            sweep_expired_options(api, db_path=tmp_db,
                                  today=date(2026, 7, 20))
        conn = sqlite3.connect(tmp_db)
        n_legs = conn.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE signal_type='OPTION_EXERCISE'").fetchone()[0]
        assert n_legs == 1

    def test_compact_occ_row_books_assignment_identically(self, tmp_db):
        """The compact-form variant of the incident: pre-fix, occ[12]
        read a strike digit and the equity leg was silently skipped
        (P&L overstated). Both forms must now behave identically."""
        from options_lifecycle import sweep_expired_options
        self._seed_incident(tmp_db, occ=self.OCC_COMPACT)
        api = _api(activities=[_activity("OPASN", self.OCC_COMPACT, 1)])
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=185.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                           today=date(2026, 7, 20))
        assert result["assigned"] == 1
        assert result["equity_legs_logged"] == 1
        assert _stock_virtual(tmp_db) == {"GOOG": -25.0}

    def test_opexp_beats_itm_estimate(self, tmp_db):
        """Broker says OPEXP (expired unassigned) while the bar close
        says ITM — broker truth wins: worthless, no equity leg. (Bar
        closes are estimates; settlement can differ.)"""
        from options_lifecycle import sweep_expired_options
        self._seed_incident(tmp_db)
        api = _api(activities=[_activity("OPEXP", self.OCC_COMPACT, 1)])
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=185.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                           today=date(2026, 7, 20))
        assert result["closed_worthless"] == 1
        assert result["equity_legs_logged"] == 0
        assert _stock_virtual(tmp_db) == {"GOOG": 75.0}


class TestAssignmentDetection:
    """Short option assignment — broker OPASN activity required."""

    def test_short_put_opasn_books_buy_shares(self, tmp_db):
        """CSP 2× strike 140 assigned → BUY 200 shares at $140 as a
        new open long lot (the broker holds the shares)."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="sell", qty=2, decision_price=1.00,
                          option_strategy="cash_secured_put",
                          occ_symbol="AAPL  200115P00140000",
                          expiry="2020-01-15", strike=140.0)
        api = _api(activities=[
            _activity("OPASN", "AAPL200115P00140000", 2)])
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=135.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2020, 1, 18))
        assert result["assigned"] == 1
        assert result["equity_legs_logged"] == 1

        conn = sqlite3.connect(tmp_db)
        leg = conn.execute(
            "SELECT side, qty, price, status FROM trades "
            "WHERE signal_type='OPTION_EXERCISE'").fetchone()
        assert leg[0] == "buy"
        assert leg[1] == 200  # 2 contracts × 100
        assert leg[2] == 140.0
        assert leg[3] == "open"
        # Premium kept on the option row
        opt = conn.execute(
            "SELECT pnl FROM trades WHERE signal_type='OPTIONS'"
        ).fetchone()
        assert opt[0] == pytest.approx(200.0)  # $1.00 × 100 × 2

    def test_partial_opasn_books_only_assigned_contracts(self, tmp_db):
        """3 short calls, broker assigned only 2 → the leg is 200
        shares, not 300."""
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="sell", qty=3, decision_price=1.00,
                          option_strategy="covered_call",
                          occ_symbol="AAPL  200115C00150000",
                          expiry="2020-01-15", strike=150.0)
        api = _api(activities=[
            _activity("OPASN", "AAPL200115C00150000", 2)])
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=155.0):
            sweep_expired_options(api, db_path=tmp_db,
                                  today=date(2020, 1, 18))
        conn = sqlite3.connect(tmp_db)
        leg = conn.execute(
            "SELECT qty FROM trades WHERE signal_type='OPTION_EXERCISE'"
        ).fetchone()
        assert leg[0] == 200


class TestExerciseDetection:
    """Long option exercise — broker OPXRC activity required. The
    option row books -premium (cost); intrinsic value lives in the
    equity leg's strike basis, realized on the stock side exactly
    once (the pre-fix intrinsic-minus-premium P&L double-counted)."""

    def test_long_call_opxrc_books_buy_at_strike(self, tmp_db):
        from options_lifecycle import sweep_expired_options
        _seed_open_option(tmp_db, side="buy", qty=1, decision_price=2.00,
                          option_strategy="long_call",
                          occ_symbol="AAPL  200115C00150000",
                          expiry="2020-01-15", strike=150.0)
        api = _api(activities=[
            _activity("OPXRC", "AAPL200115C00150000", 1)])
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=158.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2020, 1, 18))
        assert result["exercised"] == 1
        assert result["equity_legs_logged"] == 1

        conn = sqlite3.connect(tmp_db)
        # Option row: premium cost only (no intrinsic double-count)
        opt = conn.execute(
            "SELECT pnl FROM trades WHERE signal_type='OPTIONS'"
        ).fetchone()
        assert opt[0] == pytest.approx(-200.0)
        # Equity leg: BUY 100 at strike 150, an open broker-held lot
        eq = conn.execute(
            "SELECT side, qty, price, status FROM trades "
            "WHERE signal_type='OPTION_EXERCISE'").fetchone()
        assert eq[0] == "buy"
        assert eq[1] == 100
        assert eq[2] == 150.0
        assert eq[3] == "open"

    def test_long_put_opxrc_books_sell_at_strike(self, tmp_db):
        from options_lifecycle import sweep_expired_options
        _seed_stock_long(tmp_db, symbol="AAPL", qty=100, price=155.0)
        _seed_open_option(tmp_db, side="buy", qty=1, decision_price=3.00,
                          option_strategy="long_put",
                          occ_symbol="AAPL  200115P00150000",
                          expiry="2020-01-15", strike=150.0)
        api = _api(activities=[
            _activity("OPXRC", "AAPL200115P00150000", 1)])
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=140.0):
            result = sweep_expired_options(api, db_path=tmp_db,
                                              today=date(2020, 1, 18))
        assert result["exercised"] == 1
        conn = sqlite3.connect(tmp_db)
        eq = conn.execute(
            "SELECT side, qty, price FROM trades "
            "WHERE signal_type='OPTION_EXERCISE'").fetchone()
        assert eq[0] == "sell"
        assert eq[1] == 100
        assert eq[2] == 150.0
        # The 100-share lot was fully consumed → flipped closed
        entry = conn.execute(
            "SELECT status FROM trades WHERE side='buy' "
            "AND occ_symbol IS NULL AND signal_type='AI_ANALYSIS'"
        ).fetchone()
        assert entry[0] == "closed"
        # Book is flat, matching the broker after exercise
        assert _stock_virtual(tmp_db) in ({}, {"AAPL": 0.0})
