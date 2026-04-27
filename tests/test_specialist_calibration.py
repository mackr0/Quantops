"""Guardrail: specialist confidence must be calibrated against
empirical accuracy.

History: 2026-04-27 methodology audit. Wave 3 / Fix #9 of
METHODOLOGY_FIX_PLAN.md. Each specialist (earnings_analyst,
pattern_recognizer, sentiment_narrative, risk_assessor) returns a
verdict + raw confidence 0-100. Before this fix, those raw
confidences were never validated against actual outcomes — an
over-confident specialist could dominate the ensemble even if its
historical hit rate didn't justify the weight.

The fix: per-specialist Platt-scaling layer. Logistic regression
maps raw_confidence → empirical P(correct), fitted from each
specialist's recorded (raw_confidence, was_correct) pairs.
`_synthesize` applies the calibration before computing each
specialist's contribution to the ensemble verdict.

These tests prove:

1. The calibration module exposes the contract API.
2. Recording specialist outcomes for a prediction round-trips.
3. Resolution backfills was_correct.
4. fit_calibrator returns None when there's not enough data.
5. fit_calibrator returns a usable model with enough data.
6. **The behavioral test:** an over-confident specialist (always
   says 90, only right 50% of the time) gets calibrated DOWN to
   ~50. An under-confident one (always says 30, right 80% of the
   time) gets calibrated UP toward ~80.
7. apply_calibration returns the raw value when calibrator is None
   (graceful degradation).
8. _synthesize source-references the calibration apply path.
"""

from __future__ import annotations

import inspect
import os
import random
import sqlite3
import tempfile

import pytest

import ensemble
import specialist_calibration as sc


# ---------------------------------------------------------------------------
# Source-level guardrails
# ---------------------------------------------------------------------------

def test_module_exposes_contract_api():
    for name in ("init_calibration_db",
                 "record_outcomes_for_prediction",
                 "update_outcomes_on_resolve",
                 "fit_calibrator",
                 "save_calibrator",
                 "get_calibrator",
                 "apply_calibration",
                 "refit_all"):
        assert hasattr(sc, name), (
            f"REGRESSION: specialist_calibration module is missing "
            f"`{name}`. The contract is documented in the module "
            f"docstring; removing any of these breaks the integration."
        )


def test_synthesize_applies_calibration_when_db_path_provided():
    src = inspect.getsource(ensemble._synthesize)
    assert "apply_calibration" in src, (
        "REGRESSION: ensemble._synthesize no longer applies the "
        "calibration layer. Without that call, the over-confident-"
        "specialist bug returns and the ensemble weights raw "
        "confidence instead of empirical accuracy."
    )
    assert "get_calibrator" in src, (
        "REGRESSION: ensemble._synthesize no longer loads "
        "calibrators. The fix is inert without this lookup."
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    sc.init_calibration_db(path)
    yield path
    # Best-effort cleanup — also remove any pkls we wrote
    try:
        os.unlink(path)
    except Exception:
        pass
    base_dir = os.path.dirname(os.path.abspath(path))
    db_stem = os.path.splitext(os.path.basename(path))[0]
    for f in os.listdir(base_dir):
        if f.startswith(f"calibrator_{db_stem}_"):
            try:
                os.unlink(os.path.join(base_dir, f))
            except Exception:
                pass
    sc.clear_calibrator_cache()


# ---------------------------------------------------------------------------
# Recording + resolution round-trip
# ---------------------------------------------------------------------------

def test_record_and_update_outcomes_roundtrip(fresh_db):
    specialists = [
        {"specialist": "earnings_analyst", "verdict": "BUY",
         "confidence": 78, "reasoning": ""},
        {"specialist": "risk_assessor", "verdict": "HOLD",
         "confidence": 60, "reasoning": ""},
    ]
    sc.record_outcomes_for_prediction(fresh_db, prediction_id=42,
                                       specialists=specialists)

    conn = sqlite3.connect(fresh_db)
    rows = conn.execute(
        "SELECT specialist_name, verdict, raw_confidence, was_correct "
        "FROM specialist_outcomes WHERE prediction_id = ?",
        (42,),
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    names = {r[0] for r in rows}
    assert names == {"earnings_analyst", "risk_assessor"}
    assert all(r[3] is None for r in rows), (
        "Just-recorded outcomes should have was_correct = NULL"
    )

    # Resolution path
    sc.update_outcomes_on_resolve(fresh_db, prediction_id=42,
                                   was_correct=True)
    conn = sqlite3.connect(fresh_db)
    rows = conn.execute(
        "SELECT was_correct FROM specialist_outcomes WHERE prediction_id = ?",
        (42,),
    ).fetchall()
    conn.close()
    assert all(r[0] == 1 for r in rows), (
        "After update_outcomes_on_resolve(was_correct=True), every "
        "row for the prediction must be flagged correct."
    )


# ---------------------------------------------------------------------------
# Fitting: insufficient data → None
# ---------------------------------------------------------------------------

def test_fit_returns_none_when_below_min_samples(fresh_db):
    # Below MIN_SAMPLES_TO_FIT
    for i in range(5):
        sc.record_outcomes_for_prediction(
            fresh_db, prediction_id=100 + i,
            specialists=[{"specialist": "earnings_analyst",
                          "verdict": "BUY", "confidence": 70}],
        )
        sc.update_outcomes_on_resolve(fresh_db, 100 + i,
                                        was_correct=(i % 2 == 0))
    cal = sc.fit_calibrator(fresh_db, "earnings_analyst")
    assert cal is None, (
        "With only 5 resolved rows, fit_calibrator must return None "
        "and not pretend to have fitted a model."
    )


# ---------------------------------------------------------------------------
# THE BEHAVIORAL TEST — calibration actually corrects the bias
# ---------------------------------------------------------------------------

def test_overconfident_specialist_gets_calibrated_down(fresh_db):
    """Seed 100 outcomes for a specialist that always says BUY with
    confidence 90 but is only right 50% of the time. After fitting,
    apply_calibration(90) should return ~50, not 90."""
    rng = random.Random(42)
    for i in range(100):
        sc.record_outcomes_for_prediction(
            fresh_db, prediction_id=1000 + i,
            specialists=[{"specialist": "overconfident",
                          "verdict": "BUY", "confidence": 90}],
        )
        sc.update_outcomes_on_resolve(
            fresh_db, 1000 + i, was_correct=(rng.random() < 0.5),
        )

    cal = sc.fit_calibrator(fresh_db, "overconfident")
    assert cal is not None, "100 samples (mixed) should fit a model"

    calibrated_90 = sc.apply_calibration(90, cal)
    assert 35 <= calibrated_90 <= 65, (
        f"Over-confident specialist (raw=90, hit rate=50%) should "
        f"calibrate to ~50, got {calibrated_90}. The calibration "
        f"layer is supposed to map raw confidence to empirical "
        f"P(correct). If this fails, the specialist's contribution "
        f"will be inflated 1.5-1.8x in the ensemble."
    )


def test_underconfident_specialist_with_high_accuracy(fresh_db):
    """Specialist that says BUY with confidence 30 but is right 80%
    of the time (varied confidences for fittability)."""
    rng = random.Random(7)
    confs = [25, 30, 35]
    for i in range(120):
        c = rng.choice(confs)
        sc.record_outcomes_for_prediction(
            fresh_db, prediction_id=2000 + i,
            specialists=[{"specialist": "underconfident",
                          "verdict": "BUY", "confidence": c}],
        )
        sc.update_outcomes_on_resolve(
            fresh_db, 2000 + i, was_correct=(rng.random() < 0.80),
        )

    cal = sc.fit_calibrator(fresh_db, "underconfident")
    assert cal is not None

    # The mean accuracy is 80% so a calibrated 30 should be much
    # higher than 30. We accept anything ≥ 60 (reasonably calibrated).
    calibrated_30 = sc.apply_calibration(30, cal)
    assert calibrated_30 >= 60, (
        f"Under-confident specialist (raw=30, hit rate=80%) should "
        f"calibrate UP toward ~80, got {calibrated_30}. The fit must "
        f"map empirical accuracy back into the confidence space."
    )


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------

def test_apply_calibration_passes_through_when_calibrator_is_none():
    """No fit yet → return raw value. Lets ensemble degrade
    gracefully on first deploy or for new specialists."""
    assert sc.apply_calibration(78, None) == 78
    assert sc.apply_calibration(0, None) == 0
    assert sc.apply_calibration(100, None) == 100


def test_get_calibrator_returns_none_for_missing_pkl(fresh_db):
    """No fitted model on disk → cache returns None, application
    falls back to raw confidence."""
    assert sc.get_calibrator(fresh_db, "never_fitted") is None


# ---------------------------------------------------------------------------
# Backfill — parse existing features_json.ensemble_summary
# ---------------------------------------------------------------------------

def test_backfill_parses_ensemble_summary_format(fresh_db):
    """Seed ai_predictions with realistic features_json containing
    an ensemble_summary string in the prod format. Backfill should
    parse the per-specialist verdicts and populate specialist_outcomes
    with was_correct already set from actual_outcome."""
    import json as _json
    # Build the ai_predictions table on fresh_db to simulate a real
    # journal db (the fresh_db fixture only created specialist_outcomes).
    conn = sqlite3.connect(fresh_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            status TEXT,
            actual_outcome TEXT,
            features_json TEXT
        )
    """)
    # Insert 3 resolved predictions with realistic ensemble summaries
    conn.execute(
        "INSERT INTO ai_predictions (status, actual_outcome, features_json) "
        "VALUES (?, ?, ?)",
        ("resolved", "win", _json.dumps({
            "ensemble_summary": "ENSEMBLE: BUY @ 100% — earn=BUY(72), patt=HOLD(45), sent=BUY(78), risk=HOLD(55)",
        })),
    )
    conn.execute(
        "INSERT INTO ai_predictions (status, actual_outcome, features_json) "
        "VALUES (?, ?, ?)",
        ("resolved", "loss", _json.dumps({
            "ensemble_summary": "ENSEMBLE: SELL @ 100% — earn=ABSTAIN(0), patt=SELL(78), sent=SELL(72), risk=SELL(70)",
        })),
    )
    conn.execute(
        "INSERT INTO ai_predictions (status, actual_outcome, features_json) "
        "VALUES (?, ?, ?)",
        ("resolved", "neutral", _json.dumps({  # neutrals must be skipped
            "ensemble_summary": "ENSEMBLE: HOLD @ 50% — earn=HOLD(60), patt=HOLD(55), sent=HOLD(50), risk=HOLD(50)",
        })),
    )
    conn.commit()
    conn.close()

    inserted = sc.backfill_from_resolved_predictions(fresh_db)
    # Pred 1: 4 specialists, but earn=BUY/patt=HOLD/sent=BUY/risk=HOLD all kept = 4
    # Pred 2: earn=ABSTAIN skipped; patt/sent/risk = 3
    # Pred 3: actual_outcome='neutral', whole prediction skipped = 0
    assert inserted == 7, (
        f"Expected 7 backfilled rows (4 from pred1 + 3 from pred2), "
        f"got {inserted}"
    )

    # Verify the rows look right
    conn = sqlite3.connect(fresh_db)
    rows = conn.execute(
        "SELECT specialist_name, verdict, raw_confidence, was_correct "
        "FROM specialist_outcomes ORDER BY prediction_id, specialist_name"
    ).fetchall()
    conn.close()
    # Pred 1 (win) — was_correct=1 for all 4
    pred1_rows = [r for r in rows if r[3] == 1]
    assert len(pred1_rows) == 4
    assert {r[0] for r in pred1_rows} == {
        "earnings_analyst", "pattern_recognizer",
        "sentiment_narrative", "risk_assessor",
    }
    # Pred 2 (loss) — was_correct=0 for all 3
    pred2_rows = [r for r in rows if r[3] == 0]
    assert len(pred2_rows) == 3
    assert "earnings_analyst" not in {r[0] for r in pred2_rows}, (
        "earn=ABSTAIN must be skipped — ABSTAIN means no signal"
    )


def test_backfill_is_idempotent(fresh_db):
    """Re-running backfill on the same data must not duplicate rows."""
    import json as _json
    conn = sqlite3.connect(fresh_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, status TEXT, actual_outcome TEXT,
            features_json TEXT
        )
    """)
    conn.execute(
        "INSERT INTO ai_predictions (status, actual_outcome, features_json) "
        "VALUES (?, ?, ?)",
        ("resolved", "win", _json.dumps({
            "ensemble_summary": "ENSEMBLE: BUY @ 100% — earn=BUY(72), patt=BUY(60), sent=BUY(78), risk=BUY(55)",
        })),
    )
    conn.commit()
    conn.close()

    n1 = sc.backfill_from_resolved_predictions(fresh_db)
    n2 = sc.backfill_from_resolved_predictions(fresh_db)
    assert n1 == 4
    assert n2 == 0, "Second backfill must insert zero new rows (idempotent)"
