"""Phase 5c of the instrument-class pipeline refactor (2026-05-11).

Phase 5c implements the option-aware resolver. Replaces Phase 5b's
defer-everything safety floor with actual option-economics math:
  - Single-leg (OPTIONS / OPTION_EXERCISE with occ_symbol):
    return_pct = (current_premium - entry) / entry × 100
  - Multileg (MULTILEG_OPEN with option_order_id):
    return_pct from net spread P&L vs entry credit/debit

Win/loss thresholds appropriate to option volatility:
  - Long premium: ±25% return → win/loss
  - Short premium (qty<0): inverted (theta wins)
  - Multileg: +25% win, -50% loss (asymmetric to spread economics)

This file pins:
- HELPER: link_option_prediction_to_trade UPDATEs the most recent
  pending row for (symbol, signal); idempotent; safely no-ops with
  no match.
- HELPER: get_multileg_legs_by_combo_order returns the legs for a
  combo via either order_id match or reason-string match.
- SINGLE-LEG MATH: $1.20 → $2.40 = +100%; entry=0 returns None;
  fetch failure returns None.
- MULTILEG MATH: net spread value computation with signed qty;
  partial leg data returns None (safety: don't compute on
  incomplete data).
- CLASSIFICATION: 30% return on long premium → win; -30% → loss;
  10% → neutral; multileg uses asymmetric thresholds.
- WIRED INTO _resolve_one: option signals route through the
  resolver instead of returning None unconditionally.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines.outcomes import option_resolver
from ai_tracker import _resolve_one


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


def _insert_pending_option_row(db_path, symbol="CWAN",
                                  signal="OPTIONS",
                                  entry_premium=1.20,
                                  occ_symbol=None,
                                  option_order_id=None,
                                  ts_offset_minutes=0):
    """Insert a pending option prediction; return its id."""
    ts = (datetime.utcnow()
          - timedelta(minutes=ts_offset_minutes)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO ai_predictions
               (timestamp, symbol, predicted_signal, confidence,
                reasoning, price_at_prediction, status,
                occ_symbol, option_order_id)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (ts, symbol, signal, 70, "test rationale",
             entry_premium, occ_symbol, option_order_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_multileg_legs(db_path, combo_id, legs):
    """Insert N legs of a multileg combo into trades table.

    legs: list of (occ_symbol, qty, fill_price, side) tuples.
    """
    conn = sqlite3.connect(db_path)
    try:
        for occ, qty, fill_price, side in legs:
            conn.execute(
                """INSERT INTO trades
                   (timestamp, symbol, side, qty, price, fill_price,
                    order_id, signal_type, reason, status,
                    occ_symbol, option_strategy)
                   VALUES (?, 'CWAN', ?, ?, ?, ?, ?, 'MULTILEG', ?,
                           'filled', ?, 'bull_put_spread')""",
                (datetime.utcnow().isoformat(), side, qty,
                 fill_price, fill_price,
                 combo_id, f"bull_put_spread leg (combo={combo_id})",
                 occ),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Single-leg resolver math
# ---------------------------------------------------------------------------

class TestSingleLegResolver:
    def test_premium_doubled_returns_100pct(self):
        prediction = {
            "predicted_signal": "OPTIONS",
            "price_at_prediction": 1.20,
            "occ_symbol": "CWAN  260612C00050000",
        }
        ret = option_resolver.compute_option_return_pct(
            prediction,
            fetch_premium=lambda occ: 2.40,
        )
        assert ret == pytest.approx(100.0, rel=0.01)

    def test_premium_halved_returns_minus_50pct(self):
        prediction = {
            "predicted_signal": "OPTIONS",
            "price_at_prediction": 1.20,
            "occ_symbol": "CWAN  260612C00050000",
        }
        ret = option_resolver.compute_option_return_pct(
            prediction,
            fetch_premium=lambda occ: 0.60,
        )
        assert ret == pytest.approx(-50.0, rel=0.01)

    def test_no_occ_symbol_returns_none(self):
        prediction = {
            "predicted_signal": "OPTIONS",
            "price_at_prediction": 1.20,
            "occ_symbol": None,
        }
        ret = option_resolver.compute_option_return_pct(
            prediction, fetch_premium=lambda occ: 2.40,
        )
        assert ret is None

    def test_fetch_premium_returns_zero_returns_none(self):
        prediction = {
            "predicted_signal": "OPTIONS",
            "price_at_prediction": 1.20,
            "occ_symbol": "CWAN  260612C00050000",
        }
        ret = option_resolver.compute_option_return_pct(
            prediction, fetch_premium=lambda occ: 0.0,
        )
        assert ret is None

    def test_fetch_premium_raises_returns_none(self):
        prediction = {
            "predicted_signal": "OPTIONS",
            "price_at_prediction": 1.20,
            "occ_symbol": "CWAN  260612C00050000",
        }
        def boom(occ):
            raise RuntimeError("network")
        ret = option_resolver.compute_option_return_pct(
            prediction, fetch_premium=boom,
        )
        assert ret is None

    def test_zero_entry_premium_returns_none(self):
        prediction = {
            "predicted_signal": "OPTIONS",
            "price_at_prediction": 0.0,
            "occ_symbol": "CWAN  260612C00050000",
        }
        ret = option_resolver.compute_option_return_pct(
            prediction, fetch_premium=lambda occ: 2.40,
        )
        assert ret is None


# ---------------------------------------------------------------------------
# Multileg resolver math
# ---------------------------------------------------------------------------

class TestMultilegResolver:
    def test_credit_spread_profitable_returns_positive(self):
        """Bull put credit spread: short put @ $1.50, long put @ $1.00.
        Net credit = $0.50/share = $50/contract. 1 contract.

        At a profit point, both puts decay: short @ $0.20,
        long @ $0.10. Net = -0.10/share = -$10/contract (we owe
        $10 to close vs $50 we received → +$40 P&L).

        Entry value (signed qty × price × 100):
          short put: -1 × $1.50 × 100 = -$150
          long put:  +1 × $1.00 × 100 = +$100
          net entry = -$50  (received $50 net)
        Current value:
          short put: -1 × $0.20 × 100 = -$20
          long put:  +1 × $0.10 × 100 = +$10
          net current = -$10
        Return = (current - entry) / |entry| × 100
              = (-10 - -50) / 50 × 100 = +80%
        """
        prediction = {
            "predicted_signal": "MULTILEG_OPEN",
            "price_at_prediction": 0.50,
            "option_order_id": "combo-abc123",
        }
        legs = [
            {"occ_symbol": "AAPL  260612P00150000", "qty": -1.0,
             "price": 1.50, "side": "sell"},
            {"occ_symbol": "AAPL  260612P00145000", "qty": 1.0,
             "price": 1.00, "side": "buy"},
        ]
        current_premiums = {
            "AAPL  260612P00150000": 0.20,
            "AAPL  260612P00145000": 0.10,
        }
        ret = option_resolver.compute_option_return_pct(
            prediction,
            fetch_premium=current_premiums.get,
            get_legs=lambda combo_id: legs,
        )
        assert ret == pytest.approx(80.0, abs=1.0)

    def test_no_combo_id_returns_none(self):
        prediction = {
            "predicted_signal": "MULTILEG_OPEN",
            "price_at_prediction": 0.50,
            "option_order_id": None,
        }
        ret = option_resolver.compute_option_return_pct(
            prediction,
            fetch_premium=lambda occ: 1.0,
            get_legs=lambda combo_id: [],
        )
        assert ret is None

    def test_partial_leg_pricing_returns_none(self):
        """If ANY leg's current premium can't be fetched, return
        None — partial spread valuation is misleading. Better to
        defer than to compute on incomplete data."""
        prediction = {
            "predicted_signal": "MULTILEG_OPEN",
            "price_at_prediction": 0.50,
            "option_order_id": "combo-abc123",
        }
        legs = [
            {"occ_symbol": "AAPL  260612P00150000", "qty": -1.0,
             "price": 1.50, "side": "sell"},
            {"occ_symbol": "AAPL  260612P00145000", "qty": 1.0,
             "price": 1.00, "side": "buy"},
        ]
        # Only one leg has a price
        current_premiums = {"AAPL  260612P00150000": 0.20}
        ret = option_resolver.compute_option_return_pct(
            prediction,
            fetch_premium=current_premiums.get,
            get_legs=lambda combo_id: legs,
        )
        assert ret is None

    def test_no_legs_returns_none(self):
        prediction = {
            "predicted_signal": "MULTILEG_OPEN",
            "price_at_prediction": 0.50,
            "option_order_id": "combo-abc123",
        }
        ret = option_resolver.compute_option_return_pct(
            prediction,
            fetch_premium=lambda occ: 1.0,
            get_legs=lambda combo_id: [],
        )
        assert ret is None


# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------

class TestClassifyOptionOutcome:
    def test_long_premium_30pct_gain_is_win(self):
        outcome, _ = option_resolver.classify_option_outcome(
            30.0, "OPTIONS",
        )
        assert outcome == "win"

    def test_long_premium_minus_30pct_is_loss(self):
        outcome, _ = option_resolver.classify_option_outcome(
            -30.0, "OPTIONS",
        )
        assert outcome == "loss"

    def test_long_premium_10pct_is_neutral(self):
        outcome, _ = option_resolver.classify_option_outcome(
            10.0, "OPTIONS",
        )
        assert outcome == "neutral"

    def test_short_premium_decay_is_win(self):
        """Short premium (qty<0) wins when premium drops (theta)."""
        outcome, _ = option_resolver.classify_option_outcome(
            -30.0, "OPTIONS", signed_qty_hint=-1.0,
        )
        assert outcome == "win"

    def test_short_premium_runs_against_is_loss(self):
        outcome, _ = option_resolver.classify_option_outcome(
            30.0, "OPTIONS", signed_qty_hint=-1.0,
        )
        assert outcome == "loss"

    def test_multileg_30pct_profit_is_win(self):
        outcome, _ = option_resolver.classify_option_outcome(
            30.0, "MULTILEG_OPEN",
        )
        assert outcome == "win"

    def test_multileg_minus_30pct_is_neutral(self):
        """Multileg uses asymmetric thresholds: -50% loss, +25%
        win. -30% is still in the neutral band."""
        outcome, _ = option_resolver.classify_option_outcome(
            -30.0, "MULTILEG_OPEN",
        )
        assert outcome == "neutral"

    def test_multileg_minus_60pct_is_loss(self):
        outcome, _ = option_resolver.classify_option_outcome(
            -60.0, "MULTILEG_OPEN",
        )
        assert outcome == "loss"


# ---------------------------------------------------------------------------
# link_option_prediction_to_trade — the journal helper
# ---------------------------------------------------------------------------

class TestLinkOptionPredictionToTrade:
    def test_links_combo_id_to_recent_pending(self, db_path):
        from journal import link_option_prediction_to_trade
        pid = _insert_pending_option_row(
            db_path, signal="MULTILEG_OPEN",
        )
        ok = link_option_prediction_to_trade(
            db_path, symbol="CWAN", signal="MULTILEG_OPEN",
            option_order_id="combo-xyz789",
        )
        assert ok is True
        row = sqlite3.connect(db_path).execute(
            "SELECT option_order_id FROM ai_predictions WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row[0] == "combo-xyz789"

    def test_links_occ_symbol_for_single_leg(self, db_path):
        from journal import link_option_prediction_to_trade
        pid = _insert_pending_option_row(db_path, signal="OPTIONS")
        ok = link_option_prediction_to_trade(
            db_path, symbol="CWAN", signal="OPTIONS",
            occ_symbol="CWAN  260612C00050000",
        )
        assert ok is True
        row = sqlite3.connect(db_path).execute(
            "SELECT occ_symbol FROM ai_predictions WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row[0] == "CWAN  260612C00050000"

    def test_no_matching_pending_returns_false(self, db_path):
        """No pending row for this (symbol, signal) → safe no-op."""
        from journal import link_option_prediction_to_trade
        ok = link_option_prediction_to_trade(
            db_path, symbol="CWAN", signal="MULTILEG_OPEN",
            option_order_id="combo-xyz789",
        )
        assert ok is False

    def test_old_pending_row_excluded_by_max_age(self, db_path):
        """Pending row inserted >max_age_minutes ago is NOT linked
        — the linkage only matches a fresh prediction (the trade we
        just executed)."""
        from journal import link_option_prediction_to_trade
        # Insert with a 30-minute-old timestamp; default max_age=10
        _insert_pending_option_row(
            db_path, signal="MULTILEG_OPEN", ts_offset_minutes=30,
        )
        ok = link_option_prediction_to_trade(
            db_path, symbol="CWAN", signal="MULTILEG_OPEN",
            option_order_id="combo-xyz789",
        )
        assert ok is False

    def test_no_db_path_returns_false(self):
        from journal import link_option_prediction_to_trade
        assert link_option_prediction_to_trade(
            None, symbol="X", signal="OPTIONS",
            option_order_id="y",
        ) is False


# ---------------------------------------------------------------------------
# get_multileg_legs_by_combo_order
# ---------------------------------------------------------------------------

class TestGetMultilegLegs:
    def test_returns_legs_matched_by_order_id(self, db_path):
        from journal import get_multileg_legs_by_combo_order
        _insert_multileg_legs(db_path, "combo-1", [
            ("AAPL  260612P00150000", -1.0, 1.50, "sell"),
            ("AAPL  260612P00145000", 1.0, 1.00, "buy"),
        ])
        legs = get_multileg_legs_by_combo_order(db_path, "combo-1")
        assert len(legs) == 2
        occs = {l["occ_symbol"] for l in legs}
        assert "AAPL  260612P00150000" in occs
        assert "AAPL  260612P00145000" in occs

    def test_returns_empty_when_no_match(self, db_path):
        from journal import get_multileg_legs_by_combo_order
        legs = get_multileg_legs_by_combo_order(db_path, "no-such-combo")
        assert legs == []

    def test_returns_empty_with_no_db(self):
        from journal import get_multileg_legs_by_combo_order
        assert get_multileg_legs_by_combo_order(None, "x") == []


# ---------------------------------------------------------------------------
# _resolve_one wired path — option signals route through resolver
# ---------------------------------------------------------------------------

class TestResolveOnePhase5cIntegration:
    def test_options_signal_with_occ_resolves_to_win(self):
        """Phase 5c integration: OPTIONS row with occ_symbol +
        favorable premium → resolves to win."""
        prediction = {
            "predicted_signal": "OPTIONS",
            "price_at_prediction": 1.20,
            "prediction_type": None,
            "occ_symbol": "CWAN  260612C00050000",
            "option_order_id": None,
            "timestamp": (datetime.utcnow()
                           - timedelta(days=10)).isoformat(),
        }
        with patch(
            "pipelines.outcomes.option_resolver._resolve_single_leg",
            return_value=50.0,  # +50% premium gain
        ):
            result = _resolve_one(prediction, current_price=0.0)
        assert result is not None
        outcome, ret_pct, _days = result
        assert outcome == "win"
        assert ret_pct == pytest.approx(50.0)

    def test_options_signal_without_metadata_defers(self):
        """Phase 5b safety floor still applies when occ_symbol +
        option_order_id are both NULL — Phase 5c didn't take over,
        the row defers."""
        prediction = {
            "predicted_signal": "OPTIONS",
            "price_at_prediction": 1.20,
            "prediction_type": None,
            "occ_symbol": None,
            "option_order_id": None,
            "timestamp": (datetime.utcnow()
                           - timedelta(days=10)).isoformat(),
        }
        result = _resolve_one(prediction, current_price=0.0)
        assert result is None

    def test_multileg_within_min_hold_window_defers(self):
        """Min-hold window applies to options too — avoid resolving
        on intraday premium noise."""
        prediction = {
            "predicted_signal": "MULTILEG_OPEN",
            "price_at_prediction": 0.50,
            "prediction_type": None,
            "occ_symbol": None,
            "option_order_id": "combo-x",
            "timestamp": (datetime.utcnow()
                           - timedelta(days=2)).isoformat(),
        }
        # Even a strong return wouldn't resolve — within the
        # MIN_HOLD_DAYS_BEFORE_RESOLVE window.
        with patch(
            "pipelines.outcomes.option_resolver.compute_option_return_pct",
            return_value=80.0,
        ):
            result = _resolve_one(prediction, current_price=0.0)
        assert result is None
