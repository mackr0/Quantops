"""TODO #5b (2026-05-11): specialist_veto rejection_code +
broker-rejection exclusion from win-rate.

Two improvements completing TODO #5:

1. CLASSIFICATION: `journal.classify_broker_rejection_message` now
   recognizes "specialist veto: <reason>" messages as the
   structurally distinct `specialist_veto` code. Phase 4b's option
   specialist vetoes (and any future specialist vetoes) get a
   distinct badge on the AI Brain panel — operators can tell
   "system blocked this" from "broker rejected this."

2. WIN-RATE EXCLUSION: tuning/{stock,option}.py:current_win_rate
   now EXCLUDE predictions with a matching broker_rejection row
   (same symbol + signal, within ±5 min). Without this, vetoed
   trades count in win rate as if they actually traded — letting
   the AI "be right" or "wrong" about positions that never opened.

This file pins:
- specialist_veto code recognized for "specialist veto: <reason>"
  messages.
- specialist_veto code distinct from "other" (operators can
  discriminate).
- humanize("specialist_veto") yields "Specialist Veto" (the badge
  label).
- Win-rate excludes a stock prediction matching a stock rejection.
- Win-rate excludes an option prediction matching a
  specialist_veto rejection.
- A prediction with NO matching rejection is INCLUDED.
- A rejection >5min from the prediction does NOT exclude.
- A prediction with a different signal does NOT match (e.g.,
  rejection on signal=BUY shouldn't exclude a HOLD prediction).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from journal import (
    classify_broker_rejection_message, record_broker_rejection,
    init_db,
)
from tuning import stock as stock_tuning
from tuning import option as option_tuning


# ---------------------------------------------------------------------------
# Classification — "specialist veto" → specialist_veto code
# ---------------------------------------------------------------------------

class TestSpecialistVetoClassification:
    def test_specialist_veto_message_classified_as_specialist_veto(self):
        msg = "specialist veto: max loss exceeds budget"
        assert classify_broker_rejection_message(msg) == "specialist_veto"

    def test_specialist_veto_case_insensitive(self):
        msg = "SPECIALIST VETO: gamma blowup near expiry"
        assert classify_broker_rejection_message(msg) == "specialist_veto"

    def test_specialist_veto_distinct_from_other(self):
        """Catches the regression where someone removes the pattern
        and the code falls back to 'other'."""
        msg = "specialist veto: iv crush exposure"
        assert classify_broker_rejection_message(msg) != "other"

    def test_unrelated_messages_not_classified_as_specialist_veto(self):
        assert classify_broker_rejection_message(
            "wash trade detected"
        ) == "wash_trade"
        assert classify_broker_rejection_message(
            "insufficient buying power"
        ) == "insufficient_buying_power"


class TestHumanizedDisplay:
    def test_specialist_veto_humanizes_correctly(self):
        from display_names import humanize
        assert humanize("specialist_veto") == "Specialist Veto"


# ---------------------------------------------------------------------------
# Win-rate exclusion — broker-rejected predictions don't count
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _insert_resolved_prediction(db_path, *, symbol, signal,
                                  outcome, ts, pipeline_kind=None):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO ai_predictions
               (timestamp, symbol, predicted_signal, confidence,
                reasoning, price_at_prediction, status, actual_outcome,
                actual_return_pct, pipeline_kind)
               VALUES (?, ?, ?, 70, 'r', 100.0, 'resolved', ?, 2.0, ?)""",
            (ts, symbol, signal, outcome, pipeline_kind),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_rejection(db_path, *, symbol, signal, ts,
                       broker_message="specialist veto: bad max loss"):
    """Insert a broker_rejection row with explicit timestamp (not
    using record_broker_rejection helper because it stamps
    datetime('now') and we need to test the time-window match)."""
    from journal import classify_broker_rejection_message
    code = classify_broker_rejection_message(broker_message)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO broker_rejections
               (timestamp, symbol, action, signal_type,
                ai_confidence, ai_reasoning, rejection_code,
                broker_message)
               VALUES (?, ?, ?, ?, 70, 'r', ?, ?)""",
            (ts, symbol, signal, signal, code, broker_message),
        )
        conn.commit()
    finally:
        conn.close()


class TestWinRateExclusionStock:
    def test_rejected_stock_prediction_excluded(self, db_path):
        ts = datetime.utcnow().isoformat()
        _insert_resolved_prediction(
            db_path, symbol="AAPL", signal="BUY", outcome="loss", ts=ts,
            pipeline_kind="stock",
        )
        _insert_rejection(
            db_path, symbol="AAPL", signal="BUY", ts=ts,
            broker_message="cannot open a long buy while a short sell order",
        )
        wr, n = stock_tuning.current_win_rate(db_path)
        assert n == 0, (
            "Rejected stock prediction must NOT count in win rate"
        )

    def test_unrejected_stock_prediction_included(self, db_path):
        ts = datetime.utcnow().isoformat()
        _insert_resolved_prediction(
            db_path, symbol="MSFT", signal="BUY", outcome="win", ts=ts,
            pipeline_kind="stock",
        )
        wr, n = stock_tuning.current_win_rate(db_path)
        assert n == 1
        assert wr == 100.0

    def test_rejection_outside_5min_window_does_not_exclude(self, db_path):
        ts = datetime.utcnow().isoformat()
        far_ts = (datetime.utcnow()
                   + timedelta(minutes=10)).isoformat()
        _insert_resolved_prediction(
            db_path, symbol="AAPL", signal="BUY", outcome="win", ts=ts,
            pipeline_kind="stock",
        )
        _insert_rejection(
            db_path, symbol="AAPL", signal="BUY", ts=far_ts,
            broker_message="wash trade",
        )
        wr, n = stock_tuning.current_win_rate(db_path)
        assert n == 1, (
            "Rejection >5min from prediction is unrelated and must "
            "NOT exclude the prediction"
        )

    def test_rejection_on_different_signal_does_not_match(self, db_path):
        ts = datetime.utcnow().isoformat()
        _insert_resolved_prediction(
            db_path, symbol="AAPL", signal="BUY", outcome="win", ts=ts,
            pipeline_kind="stock",
        )
        # Rejection on the same symbol but a SELL — shouldn't match
        _insert_rejection(
            db_path, symbol="AAPL", signal="SELL", ts=ts,
            broker_message="cannot open a short sell while a long buy order",
        )
        wr, n = stock_tuning.current_win_rate(db_path)
        assert n == 1


class TestWinRateExclusionOption:
    def test_specialist_vetoed_multileg_excluded(self, db_path):
        """The headline Phase 4b case: option_spread_risk vetoes a
        multileg trade. The prediction was recorded but the trade
        never executed. Must not count in option win rate."""
        ts = datetime.utcnow().isoformat()
        _insert_resolved_prediction(
            db_path, symbol="CWAN", signal="MULTILEG_OPEN",
            outcome="loss", ts=ts, pipeline_kind="option",
        )
        _insert_rejection(
            db_path, symbol="CWAN", signal="MULTILEG_OPEN", ts=ts,
            broker_message="specialist veto: max loss exceeds budget",
        )
        wr, n = option_tuning.current_win_rate(db_path)
        assert n == 0, (
            "Specialist-vetoed multileg must NOT count in option "
            "win rate (audit-finding-#5 LIVE side effect)"
        )

    def test_unvetoed_multileg_included(self, db_path):
        ts = datetime.utcnow().isoformat()
        _insert_resolved_prediction(
            db_path, symbol="CWAN", signal="MULTILEG_OPEN",
            outcome="win", ts=ts, pipeline_kind="option",
        )
        wr, n = option_tuning.current_win_rate(db_path)
        assert n == 1
        assert wr == 100.0


class TestCrossPipelineIsolation:
    """The win-rate exclusion must be pipeline-scoped — a stock
    rejection should not affect option win rate even when timestamps
    align. Catches a regression where the JOIN ignores
    pipeline_kind / signal-type filtering."""

    def test_stock_rejection_does_not_exclude_option_prediction(self, db_path):
        """Different signal types — stock rejection of BUY on AAPL
        shouldn't affect a MULTILEG_OPEN prediction on AAPL even at
        the same timestamp (different trade entirely)."""
        ts = datetime.utcnow().isoformat()
        _insert_resolved_prediction(
            db_path, symbol="AAPL", signal="MULTILEG_OPEN",
            outcome="win", ts=ts, pipeline_kind="option",
        )
        _insert_rejection(
            db_path, symbol="AAPL", signal="BUY", ts=ts,
            broker_message="wash trade",
        )
        wr, n = option_tuning.current_win_rate(db_path)
        assert n == 1, (
            "Stock-side rejection on AAPL BUY must not exclude "
            "AAPL MULTILEG_OPEN prediction"
        )
