"""Per-specialist confidence calibration via Platt scaling.

History: 2026-04-27 methodology audit. Wave 3 / Fix #9 of
METHODOLOGY_FIX_PLAN.md.

Each specialist (earnings_analyst, pattern_recognizer,
sentiment_narrative, risk_assessor) returns a verdict + confidence
0-100. Before this fix, those raw confidences were never validated
against actual outcomes — when earnings_analyst said BUY 78% it
might have actually been right 50% of the time. The ensemble
synthesizer then weighted contributions by raw confidence, which
let an over-confident specialist dominate.

This module fits a logistic regression per specialist mapping raw
confidence to empirical P(correct), then applies the fit at
ensemble time. After enough resolved predictions, an over-confident
specialist's contribution gets attenuated and an under-confident
specialist's contribution gets amplified — automatically.

The contract:
    record_outcomes_for_prediction(db, prediction_id, specialists)
        called when a prediction is logged. `specialists` is the
        list of per-symbol verdicts from the ensemble's
        `per_symbol[sym]["specialists"]`.

    update_outcomes_on_resolve(db, prediction_id, was_correct)
        called when a prediction resolves. Updates the rows logged
        above with the binary outcome. Called from ai_tracker.

    fit_calibrator(db, specialist_name) -> sklearn estimator | None
        trains a logistic regression on the (raw_confidence,
        was_correct) pairs for one specialist. Returns None if
        insufficient resolved data.

    apply_calibration(raw_confidence, calibrator) -> float
        single-value transform. Returns calibrated_confidence in 0-100.

    get_calibrator(db, specialist_name) -> calibrator | None
        cached loader. Calibrators are persisted as per-specialist
        pkl files alongside the journal db.
"""

from __future__ import annotations

import logging
import os
import pickle
import sqlite3
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_LOCK = threading.Lock()
_schema_initialized: set = set()
_calibrator_cache: Dict[str, Any] = {}  # (db_path, specialist) -> model
_calibrator_cache_lock = threading.Lock()

MIN_SAMPLES_TO_FIT = 30   # below this, no calibration applied
RESOLUTION_LOOKBACK_DAYS = 90   # how far back to fit on


def init_calibration_db(db_path: str) -> None:
    """Create the specialist_outcomes table if it doesn't exist.
    Idempotent."""
    if not db_path:
        return
    with _SCHEMA_LOCK:
        if db_path in _schema_initialized:
            return
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS specialist_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediction_id INTEGER NOT NULL,
                    specialist_name TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    raw_confidence INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                    was_correct INTEGER,
                    resolved_at TEXT,
                    UNIQUE(prediction_id, specialist_name)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_specialist_outcomes_name_resolved "
                "ON specialist_outcomes(specialist_name, resolved_at)"
            )
            conn.commit()
            conn.close()
            _schema_initialized.add(db_path)
        except Exception as exc:
            logger.warning("Failed to init specialist_outcomes table: %s", exc)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record_outcomes_for_prediction(
    db_path: str,
    prediction_id: int,
    specialists: List[Dict[str, Any]],
) -> None:
    """Log the per-specialist verdicts attached to one prediction.

    `specialists` is the list shaped like
    `ensemble.per_symbol[sym]["specialists"]`:
        [{"specialist": name, "verdict": "BUY"|"SELL"|"HOLD"|"VETO"|"ABSTAIN",
          "confidence": int, "reasoning": str}, ...]
    """
    if not db_path or not prediction_id or not specialists:
        return
    init_calibration_db(db_path)
    try:
        conn = sqlite3.connect(db_path)
        for s in specialists:
            name = s.get("specialist")
            verdict = s.get("verdict")
            raw_conf = s.get("confidence")
            if not name or not verdict or raw_conf is None:
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO specialist_outcomes "
                    "(prediction_id, specialist_name, verdict, raw_confidence) "
                    "VALUES (?, ?, ?, ?)",
                    (prediction_id, name, verdict, int(raw_conf)),
                )
            except Exception:
                continue
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Failed to record specialist outcomes: %s", exc)


def update_outcomes_on_resolve(
    db_path: str,
    prediction_id: int,
    was_correct: bool,
) -> None:
    """Backfill the was_correct column for every specialist_outcomes
    row tied to a prediction that just resolved."""
    if not db_path or not prediction_id:
        return
    init_calibration_db(db_path)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE specialist_outcomes "
            "SET was_correct = ?, resolved_at = datetime('now') "
            "WHERE prediction_id = ? AND was_correct IS NULL",
            (1 if was_correct else 0, prediction_id),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Failed to update specialist outcomes for "
                       "prediction %d: %s", prediction_id, exc)


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------

def fit_calibrator(db_path: str, specialist_name: str) -> Optional[Any]:
    """Fit a logistic regression on (raw_confidence, was_correct)
    pairs for one specialist. Returns None if fewer than
    MIN_SAMPLES_TO_FIT resolved samples exist.

    The fit uses Platt scaling: a 1-feature logistic regression
    where the feature is `raw_confidence / 100.0`. Output of the
    fitted model on a new raw confidence is the empirical P(correct).
    """
    if not db_path or not specialist_name:
        return None
    init_calibration_db(db_path)
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT raw_confidence, was_correct "
            "FROM specialist_outcomes "
            "WHERE specialist_name = ? "
            "AND was_correct IS NOT NULL "
            "AND resolved_at >= datetime('now', ? || ' days') "
            "ORDER BY resolved_at ASC",
            (specialist_name, f"-{RESOLUTION_LOOKBACK_DAYS}"),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("fit_calibrator query failed for %s: %s",
                       specialist_name, exc)
        return None

    if len(rows) < MIN_SAMPLES_TO_FIT:
        return None

    y = [int(r[1]) for r in rows]
    if sum(y) == 0 or sum(y) == len(y):
        # Degenerate: all wins or all losses — logistic regression
        # would refuse to fit. Skip rather than crash; the caller
        # falls back to raw confidence.
        return None

    X = [[float(r[0]) / 100.0] for r in rows]

    try:
        from sklearn.linear_model import LogisticRegression
        clf = LogisticRegression(C=1.0, solver="lbfgs", max_iter=200)
        clf.fit(X, y)
        return clf
    except Exception as exc:
        logger.warning("LogisticRegression fit failed for %s: %s",
                       specialist_name, exc)
        return None


# ---------------------------------------------------------------------------
# Persistence + cache
# ---------------------------------------------------------------------------

def _calibrator_path(db_path: str, specialist_name: str) -> str:
    """Per-specialist pkl alongside the journal db."""
    base_dir = os.path.dirname(os.path.abspath(db_path))
    db_stem = os.path.splitext(os.path.basename(db_path))[0]
    safe_name = specialist_name.replace("/", "_").replace(" ", "_")
    return os.path.join(base_dir,
                        f"calibrator_{db_stem}_{safe_name}.pkl")


def save_calibrator(db_path: str, specialist_name: str, calibrator: Any) -> None:
    if calibrator is None:
        return
    try:
        path = _calibrator_path(db_path, specialist_name)
        with open(path, "wb") as fh:
            pickle.dump(calibrator, fh)
        with _calibrator_cache_lock:
            _calibrator_cache[(db_path, specialist_name)] = calibrator
    except Exception as exc:
        logger.warning("Failed to save calibrator for %s: %s",
                       specialist_name, exc)


def get_calibrator(db_path: str, specialist_name: str) -> Optional[Any]:
    """Load (with cache) the persisted calibrator for one specialist."""
    if not db_path or not specialist_name:
        return None
    key = (db_path, specialist_name)
    with _calibrator_cache_lock:
        if key in _calibrator_cache:
            return _calibrator_cache[key]
    path = _calibrator_path(db_path, specialist_name)
    if not os.path.exists(path):
        with _calibrator_cache_lock:
            _calibrator_cache[key] = None
        return None
    try:
        with open(path, "rb") as fh:
            cal = pickle.load(fh)
        with _calibrator_cache_lock:
            _calibrator_cache[key] = cal
        return cal
    except Exception as exc:
        logger.warning("Failed to load calibrator for %s: %s",
                       specialist_name, exc)
        return None


def clear_calibrator_cache() -> None:
    """Drop in-memory cached calibrators. Called after a refit so
    the next ensemble run picks up the new model."""
    with _calibrator_cache_lock:
        _calibrator_cache.clear()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def apply_calibration(raw_confidence: int, calibrator: Any) -> int:
    """Apply a fitted calibrator to a single raw confidence value.

    Returns the calibrated confidence as an int in [0, 100]. When
    `calibrator` is None (no fit yet), returns the raw value
    unchanged so callers can use this as a no-op fallback.
    """
    if calibrator is None:
        return int(raw_confidence)
    try:
        x = [[float(raw_confidence) / 100.0]]
        # predict_proba returns [[P(class=0), P(class=1)]] — we want
        # P(was_correct = 1)
        proba = calibrator.predict_proba(x)[0][1]
        # Scale back to 0-100
        return int(round(max(0.0, min(1.0, proba)) * 100))
    except Exception:
        return int(raw_confidence)


def refit_all(db_path: str, specialist_names: List[str]) -> Dict[str, bool]:
    """Refit and persist calibrators for every named specialist.

    Returns a dict mapping specialist_name -> True/False (fitted/skipped).
    Called from the daily scheduler task.
    """
    results: Dict[str, bool] = {}
    for name in specialist_names:
        cal = fit_calibrator(db_path, name)
        if cal is None:
            results[name] = False
            continue
        save_calibrator(db_path, name, cal)
        results[name] = True
    clear_calibrator_cache()
    return results
