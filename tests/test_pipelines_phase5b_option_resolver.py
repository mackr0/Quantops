"""Phase 5b of the instrument-class pipeline refactor (2026-05-11).

Phase 5b is the SAFETY FLOOR for the option resolver:

  Today's `_resolve_one` computes return_pct from
    `(current_price - pred_price) / pred_price * 100`
  using whatever price `_get_current_prices_bulk` returns. For
  STOCK rows that's the underlying ticker price — correct. For
  OPTION rows that's the same UNDERLYING price (because
  `_get_current_prices_bulk` doesn't know about OCC), but
  `pred_price` is the OPTION PREMIUM. The math then produces
  nonsense — a $1.20 premium "current price" of $50 (underlying)
  resolves to a +4067% return.

Phase 5b stops the bleeding:
  - For any prediction whose signal is in `_OPTION_SIGNALS`
    (MULTILEG_OPEN, OPTIONS, OPTION_EXERCISE), `_resolve_one`
    returns None — the row stays 'pending'. NO option row gets a
    wrong actual_return_pct or actual_outcome value written.
  - `resolve_pending_predictions` counts deferred option rows so
    operators see the backlog in cycle logs.
  - New schema fields `occ_symbol` and `option_order_id` are added
    via the journal migration. Phase 5c will use them to wire the
    option-aware resolver (single-leg via _fetch_option_premium;
    multileg via trades-table leg lookup).

This file pins:
- DEFER ON OPTION SIGNAL: every option signal in `_OPTION_SIGNALS`
  causes `_resolve_one` to return None regardless of price/days.
- STOCK BEHAVIOR UNCHANGED: BUY/SELL/SHORT/COVER continue to
  resolve normally with the existing directional logic.
- SCHEMA: occ_symbol + option_order_id columns added by migration
  (idempotent — re-running doesn't error).
- CLASS INVARIANT (parametrized over option signals): every option
  signal type defers, no exceptions.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from ai_tracker import _resolve_one, _OPTION_SIGNALS


def _pred(signal, pred_price=100.0, days_ago=10, pred_type=None):
    """Build a synthetic prediction row with the fields _resolve_one reads."""
    ts = (datetime.utcnow() - timedelta(days=days_ago)).isoformat()
    return {
        "predicted_signal": signal,
        "price_at_prediction": pred_price,
        "prediction_type": pred_type,
        "timestamp": ts,
    }


# ---------------------------------------------------------------------------
# DEFER ON OPTION SIGNAL — class invariant
# ---------------------------------------------------------------------------

class TestOptionSignalsAlwaysDefer:
    """Phase 5b safety floor: any option signal causes resolver to
    return None. Catches regressions where someone re-enables
    option resolution before Phase 5c lands the right computation."""

    @pytest.mark.parametrize("option_signal", sorted(_OPTION_SIGNALS))
    def test_option_signal_returns_none(self, option_signal):
        """Class invariant — every option signal type defers."""
        # Even with a "current price" that would normally win/lose,
        # option rows must not resolve.
        prediction = _pred(option_signal, pred_price=100.0)
        result = _resolve_one(prediction, current_price=110.0)
        assert result is None, (
            f"{option_signal} resolved to {result} — Phase 5b says "
            f"option signals must defer until Phase 5c lands the "
            f"option-aware resolver"
        )

    @pytest.mark.parametrize("option_signal", sorted(_OPTION_SIGNALS))
    def test_option_signal_defers_even_after_timeout(self, option_signal):
        """Even past TIMEOUT_DAYS, option rows must still defer
        (the timeout path computes a wrong return_pct from the
        underlying — also disallowed)."""
        prediction = _pred(option_signal, pred_price=100.0, days_ago=30)
        result = _resolve_one(prediction, current_price=110.0)
        assert result is None

    @pytest.mark.parametrize("option_signal", sorted(_OPTION_SIGNALS))
    def test_option_signal_with_directional_pred_type_still_defers(
        self, option_signal
    ):
        """Even if a buggy upstream sets prediction_type='directional_long'
        on an option row, the signal-based check at the top of
        _resolve_one still defers. The signal is the authority."""
        prediction = _pred(option_signal, pred_price=100.0,
                            pred_type="directional_long")
        assert _resolve_one(prediction, current_price=110.0) is None


# ---------------------------------------------------------------------------
# STOCK BEHAVIOR UNCHANGED — regression check
# ---------------------------------------------------------------------------

class TestStockBehaviorUnchangedByPhase5b:
    """Stock signals (BUY/SELL/SHORT) continue to resolve normally
    with the existing directional logic. Phase 5b's option-defer
    only filters option signals; stocks pass through."""

    def test_buy_with_2pct_gain_resolves_win(self):
        # 5 trading days >= MIN_HOLD_DAYS_BEFORE_RESOLVE
        prediction = _pred("BUY", pred_price=100.0, days_ago=10,
                            pred_type="directional_long")
        result = _resolve_one(prediction, current_price=102.5)
        assert result is not None
        outcome, return_pct, _days = result
        assert outcome == "win"
        assert return_pct == pytest.approx(2.5, rel=0.01)

    def test_buy_with_2pct_loss_resolves_loss(self):
        prediction = _pred("BUY", pred_price=100.0, days_ago=10,
                            pred_type="directional_long")
        result = _resolve_one(prediction, current_price=97.5)
        assert result is not None
        outcome, _ret, _days = result
        assert outcome == "loss"

    def test_short_with_drop_resolves_win(self):
        prediction = _pred("SHORT", pred_price=100.0, days_ago=10,
                            pred_type="directional_short")
        result = _resolve_one(prediction, current_price=97.5)
        assert result is not None
        outcome, _ret, _days = result
        assert outcome == "win"

    def test_buy_within_hold_window_defers(self):
        """Pre-existing min-hold gate — stock predictions inside
        MIN_HOLD_DAYS_BEFORE_RESOLVE defer for noise reduction."""
        prediction = _pred("BUY", pred_price=100.0, days_ago=2,
                            pred_type="directional_long")
        assert _resolve_one(prediction, current_price=102.5) is None


# ---------------------------------------------------------------------------
# SCHEMA — new columns added by migration
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path_with_journal_init():
    """Build a fresh DB by running the production journal init —
    same path the Flask app uses."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from journal import init_db
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


class TestPhase5bSchemaMigration:
    def _columns(self, db_path, table):
        conn = sqlite3.connect(db_path)
        try:
            return {row[1] for row in conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()}
        finally:
            conn.close()

    def test_occ_symbol_column_added(self, db_path_with_journal_init):
        cols = self._columns(db_path_with_journal_init, "ai_predictions")
        assert "occ_symbol" in cols, (
            "Phase 5b migration should add occ_symbol column to "
            "ai_predictions for the option-aware resolver to use"
        )

    def test_option_order_id_column_added(self, db_path_with_journal_init):
        cols = self._columns(db_path_with_journal_init, "ai_predictions")
        assert "option_order_id" in cols, (
            "Phase 5b migration should add option_order_id column "
            "for multileg trade-leg lookup"
        )

    def test_migration_is_idempotent(self, db_path_with_journal_init):
        """Running the migration a second time must not error
        (it's called on every Flask app startup)."""
        from journal import init_db
        # Re-run init twice — must be a no-op
        init_db(db_path_with_journal_init)
        init_db(db_path_with_journal_init)
        # Column count unchanged
        cols = self._columns(db_path_with_journal_init, "ai_predictions")
        assert "occ_symbol" in cols
        assert "option_order_id" in cols


# ---------------------------------------------------------------------------
# Constants — pin the option-signal set so future signal additions
# can't silently slip through the safety floor
# ---------------------------------------------------------------------------

class TestOptionSignalsConstantPinning:
    def test_option_signals_set_matches_pipeline_inference(self):
        """The option-signal set used by Phase 5b's defer logic
        must match the set used by `pipelines.outcomes.kind_from_signal`
        — they're the single source of truth for which signal
        belongs to the option pipeline.

        Catches regressions where a new option signal type is added
        to one but not the other."""
        from pipelines.outcomes import kind_from_signal
        for signal in _OPTION_SIGNALS:
            assert kind_from_signal(signal) == "option", (
                f"Signal {signal!r} is in _OPTION_SIGNALS but "
                f"kind_from_signal(...) returns "
                f"{kind_from_signal(signal)!r}. The two definitions "
                f"must agree."
            )

    def test_no_known_stock_signal_in_option_set(self):
        """The defer set must NOT contain any stock signal type —
        catching regressions where someone accidentally adds 'BUY'
        or similar to the option set."""
        stock_signals = {"BUY", "STRONG_BUY", "WEAK_BUY",
                          "SELL", "STRONG_SELL", "WEAK_SELL",
                          "SHORT", "COVER"}
        leaks = _OPTION_SIGNALS & stock_signals
        assert leaks == set(), (
            f"Stock signals leaked into _OPTION_SIGNALS: {leaks}. "
            f"This would defer stock predictions and break stock "
            f"tuning."
        )
