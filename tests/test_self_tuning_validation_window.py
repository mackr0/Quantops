"""Guardrail: self_tuning's confidence-threshold adjustment must be
validated against a held-out window of recent predictions.

History: 2026-04-27 methodology audit. Wave 2 / Fix #5 of
METHODOLOGY_FIX_PLAN.md. The original `get_auto_adjustments` queried
ALL resolved predictions to compute the band win rate, then raised
the threshold based on that same data. Classic in-sample
optimization — no hold-out window for validating the change.

The fix splits resolved predictions into:
- Adjustment window: resolved older than 14 days
- Validation window: resolved within the last 14 days

A threshold-raise is only recommended if BOTH the adjustment window
shows the band underperforms AND the validation window confirms the
proposed change would have helped (or at least not hurt).

These tests prove:

1. The validation-window cutoff exists in the source.
2. When validation data agrees with the adjustment, the change is
   recommended.
3. When validation data DISAGREES (proposed change would have hurt
   recent performance), the change is rejected even though the
   adjustment-window stats look bad.
4. When there isn't enough validation data, no adjustment is made.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import self_tuning


# ---------------------------------------------------------------------------
# Source-level guardrails
# ---------------------------------------------------------------------------

def test_get_auto_adjustments_uses_validation_window_cutoff():
    """The source must reference a validation-window cutoff. Without
    it, the adjustment is back to in-sample optimization."""
    src = inspect.getsource(self_tuning.get_auto_adjustments)
    has_window = (
        "VALIDATION_WINDOW_DAYS" in src or "validation_cutoff" in src
    )
    assert has_window, (
        "REGRESSION: get_auto_adjustments no longer references a "
        "validation window cutoff. Without train/validate split, "
        "parameter adjustments overfit to in-sample data — see "
        "METHODOLOGY_FIX_PLAN.md Wave 2 / Fix #5."
    )


def test_get_auto_adjustments_queries_resolved_at():
    """The validation-window split is keyed on resolved_at. Without
    that column reference in the SQL, the split is inert."""
    src = inspect.getsource(self_tuning.get_auto_adjustments)
    assert "resolved_at" in src, (
        "REGRESSION: get_auto_adjustments no longer keys its "
        "validation window on the resolved_at column. The split is "
        "inert without it."
    )


# ---------------------------------------------------------------------------
# Behavioral: seed predictions across both windows and verify the
# validation gate honors / rejects as expected
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_db():
    """Build a journal-shaped DB with the columns get_auto_adjustments
    actually reads."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            predicted_signal TEXT NOT NULL,
            confidence INTEGER,
            price_at_prediction REAL,
            status TEXT DEFAULT 'pending',
            actual_outcome TEXT,
            actual_return_pct REAL,
            resolved_at TEXT
        )
    """)
    # tuning_history table is referenced by helpers; create a minimal one
    conn.execute("""
        CREATE TABLE tuning_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            adjusted_at TEXT,
            parameter TEXT,
            old_value TEXT,
            new_value TEXT,
            reason TEXT
        )
    """)
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


def _insert_prediction(db, *, signal, confidence, outcome,
                       resolved_days_ago, symbol="TEST"):
    conn = sqlite3.connect(db)
    resolved_at = (datetime.utcnow() - timedelta(days=resolved_days_ago)).isoformat()
    timestamp = (datetime.utcnow() - timedelta(days=resolved_days_ago + 5)).isoformat()
    conn.execute(
        "INSERT INTO ai_predictions "
        "(timestamp, symbol, predicted_signal, confidence, price_at_prediction, "
        " status, actual_outcome, resolved_at) "
        "VALUES (?, ?, ?, ?, 100.0, 'resolved', ?, ?)",
        (timestamp, symbol, signal, confidence, outcome, resolved_at),
    )
    conn.commit()
    conn.close()


def _ctx_for_db(db_path):
    """Minimal ctx-like object that get_auto_adjustments uses for
    db_path + profile_id."""
    return SimpleNamespace(db_path=db_path, profile_id=None)


def test_validation_confirms_adjustment_recommends_change(seeded_db):
    """In adjustment window (>14d ago): low-confidence predictions
    have a 20% win rate (bad). In validation window (<14d): the
    proposed threshold-raise would also help (kept cohort wins more
    than the full cohort). Recommendation should fire."""
    # Adjustment window: 25 low-conf losses, 5 low-conf wins → 16% wr
    for _ in range(25):
        _insert_prediction(seeded_db, signal="BUY", confidence=50,
                            outcome="loss", resolved_days_ago=20)
    for _ in range(5):
        _insert_prediction(seeded_db, signal="BUY", confidence=50,
                            outcome="win", resolved_days_ago=20)
    # Adjustment window also needs some high-conf data so total>20
    for _ in range(20):
        _insert_prediction(seeded_db, signal="BUY", confidence=80,
                            outcome="win", resolved_days_ago=20)

    # Validation window: high-conf strong (8W/2L=80%), low-conf weak
    # (2W/8L=20%). Raising threshold would IMPROVE validation perf.
    for _ in range(8):
        _insert_prediction(seeded_db, signal="BUY", confidence=80,
                            outcome="win", resolved_days_ago=5)
    for _ in range(2):
        _insert_prediction(seeded_db, signal="BUY", confidence=80,
                            outcome="loss", resolved_days_ago=5)
    for _ in range(2):
        _insert_prediction(seeded_db, signal="BUY", confidence=50,
                            outcome="win", resolved_days_ago=5)
    for _ in range(8):
        _insert_prediction(seeded_db, signal="BUY", confidence=50,
                            outcome="loss", resolved_days_ago=5)

    result = self_tuning.get_auto_adjustments(_ctx_for_db(seeded_db))
    assert result.get("confidence_threshold") in (60, 70), (
        f"Validation confirmed adjustment but no threshold change "
        f"recommended. result={result}"
    )


def test_validation_rejects_adjustment_when_recent_data_disagrees(seeded_db):
    """Adjustment window says raise threshold (low-conf has bad win
    rate). But the validation window shows that high-confidence
    predictions have done WORSE than low-confidence recently — so the
    raise would HURT. The gate must reject."""
    # Adjustment window: low-conf losses dominate (justifies a raise)
    for _ in range(25):
        _insert_prediction(seeded_db, signal="BUY", confidence=50,
                            outcome="loss", resolved_days_ago=20)
    for _ in range(5):
        _insert_prediction(seeded_db, signal="BUY", confidence=50,
                            outcome="win", resolved_days_ago=20)
    for _ in range(15):
        _insert_prediction(seeded_db, signal="BUY", confidence=80,
                            outcome="win", resolved_days_ago=20)

    # Validation window: regime flipped — high-conf is now WORSE.
    # 2W/8L on conf>=70 (20%), 6W/4L on conf<70 (60%).
    # Raising threshold would keep the worse cohort.
    for _ in range(2):
        _insert_prediction(seeded_db, signal="BUY", confidence=80,
                            outcome="win", resolved_days_ago=5)
    for _ in range(8):
        _insert_prediction(seeded_db, signal="BUY", confidence=80,
                            outcome="loss", resolved_days_ago=5)
    for _ in range(6):
        _insert_prediction(seeded_db, signal="BUY", confidence=50,
                            outcome="win", resolved_days_ago=5)
    for _ in range(4):
        _insert_prediction(seeded_db, signal="BUY", confidence=50,
                            outcome="loss", resolved_days_ago=5)

    result = self_tuning.get_auto_adjustments(_ctx_for_db(seeded_db))
    assert result.get("confidence_threshold") is None, (
        f"Validation should have rejected the threshold raise (recent "
        f"data shows it would hurt) but the change was still "
        f"recommended. result={result}"
    )
    # And the reason should mention the rejection
    reasons = " ".join(result.get("reasons", []))
    assert "validation rejected" in reasons.lower() or "worsen" in reasons.lower(), (
        f"Expected a 'validation rejected' or 'worsen' reason in "
        f"the output. Got reasons: {result.get('reasons')}"
    )


def test_no_adjustment_when_validation_window_too_small(seeded_db):
    """Strong adjustment-window signal but only 2 predictions resolved
    in the last 14 days — too noisy to validate against. The gate
    must defer."""
    # Plenty of adjustment-window data
    for _ in range(25):
        _insert_prediction(seeded_db, signal="BUY", confidence=50,
                            outcome="loss", resolved_days_ago=20)
    for _ in range(5):
        _insert_prediction(seeded_db, signal="BUY", confidence=50,
                            outcome="win", resolved_days_ago=20)
    for _ in range(15):
        _insert_prediction(seeded_db, signal="BUY", confidence=80,
                            outcome="win", resolved_days_ago=20)

    # Validation window: only 2 predictions
    for _ in range(2):
        _insert_prediction(seeded_db, signal="BUY", confidence=80,
                            outcome="win", resolved_days_ago=3)

    result = self_tuning.get_auto_adjustments(_ctx_for_db(seeded_db))
    assert result.get("confidence_threshold") is None, (
        "With only 2 validation-window predictions, the adjustment "
        "should be deferred. The gate must err toward not changing."
    )
