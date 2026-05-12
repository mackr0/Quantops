"""Pipeline-aware specialist calibrators (2026-05-11).

After Phase 5a's pipeline_kind tag landed, specialist calibrators
should train per-pipeline so a stock specialist's calibration
reflects its accuracy on stock proposals only — uncontaminated by
the pre-Phase-5c wrong option resolutions.

This file pins:
- FIT FILTER: fit_calibrator(pipeline_kind='stock') only consumes
  rows whose ai_predictions.pipeline_kind = 'stock' (or
  signal-fallback for legacy NULLs).
- FILE PATH: _calibrator_path includes pipeline_kind in the
  filename so stock and option calibrators are separate files
  (the stock calibrator can never be loaded for option proposals).
- LOOKUP FALLBACK: get_calibrator(pipeline_kind='option') tries
  most-specific first, falls through to direction-only, then
  legacy unified — fresh option specialists with no pipeline-
  specific fit yet still get *some* calibration.
- RECALIBRATION: recalibrate_all_specialists fits across the
  (direction × pipeline_kind) matrix; idempotent via marker;
  force=True bypasses the marker.
- ENSEMBLE INTEGRATION: run_ensemble accepts pipeline_kind kwarg
  and threads it to get_calibrator.
"""
from __future__ import annotations

import os
import pickle
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from journal import init_db
from specialist_calibration import (
    init_calibration_db, fit_calibrator, save_calibrator,
    get_calibrator, _calibrator_path, clear_calibrator_cache,
)
from pipelines.outcomes import recalibrate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path():
    """Build a fresh DB and seed enough specialist outcomes for
    fitting (≥30 samples per category)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    init_calibration_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass
    # Clean up any pkl files created next to the temp db
    base_dir = os.path.dirname(path)
    db_stem = os.path.splitext(os.path.basename(path))[0]
    for f in os.listdir(base_dir):
        if f.startswith(f"calibrator_{db_stem}_"):
            try:
                os.unlink(os.path.join(base_dir, f))
            except OSError:
                pass


def _seed_predictions_and_outcomes(
    db_path, *, pipeline_kind, signal, n=40, win_rate=0.6,
    specialist_name="risk_assessor",
):
    """Insert n resolved predictions of the given pipeline_kind +
    signal, with corresponding specialist_outcomes rows. Returns
    the prediction ids."""
    ts = (datetime.utcnow() - timedelta(days=10)).isoformat()
    conn = sqlite3.connect(db_path)
    pred_ids = []
    try:
        for i in range(n):
            outcome = "win" if i < int(n * win_rate) else "loss"
            cur = conn.execute(
                """INSERT INTO ai_predictions
                   (timestamp, symbol, predicted_signal, confidence,
                    reasoning, price_at_prediction, status, actual_outcome,
                    actual_return_pct, pipeline_kind)
                   VALUES (?, ?, ?, 70, 'r', 100.0, 'resolved', ?, 2.0, ?)""",
                (ts, f"SYM{i}", signal, outcome, pipeline_kind),
            )
            pred_ids.append(cur.lastrowid)
        conn.commit()
        # Insert specialist_outcomes rows referencing the predictions
        for i, pid in enumerate(pred_ids):
            outcome = "win" if i < int(n * win_rate) else "loss"
            # Vary raw_confidence so the regression has signal
            raw_conf = 50 + (i % 50)
            conn.execute(
                "INSERT INTO specialist_outcomes "
                "(prediction_id, specialist_name, verdict, "
                " raw_confidence, was_correct, resolved_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                (pid, specialist_name, "BUY", raw_conf,
                 1 if outcome == "win" else 0),
            )
        conn.commit()
    finally:
        conn.close()
    return pred_ids


# ---------------------------------------------------------------------------
# FILE PATH — pipeline_kind in filename keeps calibrators separate
# ---------------------------------------------------------------------------

class TestCalibratorPathWithPipelineKind:
    def test_stock_pipeline_path_distinct_from_option(self):
        p_stock = _calibrator_path(
            "/tmp/db.sqlite", "risk_assessor",
            direction="long", pipeline_kind="stock",
        )
        p_option = _calibrator_path(
            "/tmp/db.sqlite", "risk_assessor",
            direction="long", pipeline_kind="option",
        )
        assert p_stock != p_option, (
            "Stock and option calibrators must be different files "
            "— otherwise loading the wrong one is silently possible"
        )
        assert "stock" in p_stock
        assert "option" in p_option

    def test_no_pipeline_kind_yields_legacy_filename(self):
        """Legacy callers that don't pass pipeline_kind get the
        same filename as before (back-compat)."""
        p = _calibrator_path(
            "/tmp/db.sqlite", "risk_assessor", direction="long",
        )
        assert "stock" not in p
        assert "option" not in p


# ---------------------------------------------------------------------------
# FIT FILTER — pipeline_kind='stock' only sees stock rows
# ---------------------------------------------------------------------------

class TestFitCalibratorPipelineFilter:
    def test_stock_fit_excludes_option_outcomes(self, db_path):
        """Stock specialist trained with pipeline_kind='stock' only
        sees stock pipeline outcomes. If option outcomes leak in,
        fitting on the unified set would produce different
        coefficients."""
        # Seed 35 stock + 35 option (different win rates).
        _seed_predictions_and_outcomes(
            db_path, pipeline_kind="stock", signal="BUY",
            n=35, win_rate=0.7,
        )
        _seed_predictions_and_outcomes(
            db_path, pipeline_kind="option", signal="MULTILEG_OPEN",
            n=35, win_rate=0.3,
        )

        cal_stock = fit_calibrator(
            db_path, "risk_assessor", pipeline_kind="stock",
        )
        cal_option = fit_calibrator(
            db_path, "risk_assessor", pipeline_kind="option",
        )
        # Both fits should succeed (each has ≥30 samples)
        assert cal_stock is not None
        assert cal_option is not None
        # Different training data → different coefficients
        c_stock = cal_stock.coef_[0][0]
        c_option = cal_option.coef_[0][0]
        # Sanity: not identical (different win-rate distributions)
        assert c_stock != c_option, (
            "Stock and option calibrators trained on disjoint data "
            "must produce different coefficients"
        )

    def test_unified_fit_sees_both_pipelines(self, db_path):
        """When pipeline_kind=None, fit consumes ALL outcomes
        (back-compat with pre-2026-05-11 unified calibrator)."""
        _seed_predictions_and_outcomes(
            db_path, pipeline_kind="stock", signal="BUY",
            n=20, win_rate=0.7,
        )
        _seed_predictions_and_outcomes(
            db_path, pipeline_kind="option", signal="MULTILEG_OPEN",
            n=20, win_rate=0.3,
        )
        cal = fit_calibrator(db_path, "risk_assessor",
                              pipeline_kind=None)
        # Combined 40 samples ≥ 30 threshold → fit
        assert cal is not None

    def test_insufficient_samples_returns_none(self, db_path):
        _seed_predictions_and_outcomes(
            db_path, pipeline_kind="stock", signal="BUY",
            n=10, win_rate=0.7,
        )
        cal = fit_calibrator(db_path, "risk_assessor",
                              pipeline_kind="stock")
        assert cal is None


# ---------------------------------------------------------------------------
# LOOKUP FALLBACK — most-specific first, then legacy
# ---------------------------------------------------------------------------

class TestGetCalibratorFallback:
    def test_pipeline_specific_loads_when_present(self, db_path):
        # Save a mock object to the option file
        marker = ("OPTION-SPECIFIC", 42)
        path = _calibrator_path(
            db_path, "risk_assessor",
            direction="long", pipeline_kind="option",
        )
        with open(path, "wb") as fh:
            pickle.dump(marker, fh)
        clear_calibrator_cache()
        loaded = get_calibrator(
            db_path, "risk_assessor",
            direction="long", pipeline_kind="option",
        )
        assert loaded == marker

    def test_falls_back_to_unified_when_pipeline_specific_missing(
        self, db_path,
    ):
        """Brand new option specialist with no pipeline-specific
        calibrator → falls through to the legacy unified calibrator
        (better than nothing)."""
        marker = ("LEGACY-UNIFIED", 0)
        path = _calibrator_path(db_path, "risk_assessor")
        with open(path, "wb") as fh:
            pickle.dump(marker, fh)
        clear_calibrator_cache()
        loaded = get_calibrator(
            db_path, "risk_assessor",
            direction="long", pipeline_kind="option",
        )
        assert loaded == marker, (
            "Should fall back to legacy unified calibrator when "
            "(direction, pipeline_kind)-specific isn't fit yet"
        )

    def test_returns_none_when_nothing_fit(self, db_path):
        clear_calibrator_cache()
        assert get_calibrator(
            db_path, "risk_assessor",
            direction="long", pipeline_kind="option",
        ) is None


# ---------------------------------------------------------------------------
# RECALIBRATION SCRIPT
# ---------------------------------------------------------------------------

class TestRecalibrateAllSpecialists:
    def test_runs_once_per_profile(self, db_path):
        # First call: fits whatever it can.
        first = recalibrate.recalibrate_all_specialists(db_path)
        # The marker is now set — second call short-circuits.
        second = recalibrate.recalibrate_all_specialists(db_path)
        assert second["skipped_already_done"] == 1
        assert second["fitted"] == 0

    def test_force_bypasses_marker(self, db_path):
        recalibrate.recalibrate_all_specialists(db_path)
        forced = recalibrate.recalibrate_all_specialists(
            db_path, force=True,
        )
        assert forced["skipped_already_done"] == 0

    def test_no_db_path_returns_zero(self):
        counts = recalibrate.recalibrate_all_specialists("")
        assert counts["fitted"] == 0
        assert counts["skipped_already_done"] == 0

    def test_fits_per_pipeline_when_data_present(self, db_path):
        # Seed enough stock + option outcomes for risk_assessor
        _seed_predictions_and_outcomes(
            db_path, pipeline_kind="stock", signal="BUY",
            n=40, win_rate=0.6, specialist_name="risk_assessor",
        )
        _seed_predictions_and_outcomes(
            db_path, pipeline_kind="option", signal="MULTILEG_OPEN",
            n=40, win_rate=0.4, specialist_name="risk_assessor",
        )
        counts = recalibrate.recalibrate_all_specialists(
            db_path, force=True,
        )
        # At least one stock + one option calibrator fitted for
        # risk_assessor
        assert counts["fitted"] >= 2
        # Files exist
        assert os.path.exists(_calibrator_path(
            db_path, "risk_assessor", pipeline_kind="stock",
        ))
        assert os.path.exists(_calibrator_path(
            db_path, "risk_assessor", pipeline_kind="option",
        ))


# ---------------------------------------------------------------------------
# ENSEMBLE INTEGRATION — pipeline_kind threads through
# ---------------------------------------------------------------------------

class TestEnsemblePipelineKindIntegration:
    def test_run_ensemble_signature_accepts_pipeline_kind(self):
        import inspect
        from ensemble import run_ensemble
        sig = inspect.signature(run_ensemble)
        assert "pipeline_kind" in sig.parameters
        assert sig.parameters["pipeline_kind"].default is None

    def test_synthesize_signature_accepts_pipeline_kind(self):
        import inspect
        from ensemble import _synthesize
        sig = inspect.signature(_synthesize)
        assert "pipeline_kind" in sig.parameters

    def test_pipeline_route_passes_pipeline_kind(self):
        """OptionPipeline.route_to_specialists must pass
        pipeline_kind='option' so the right calibrator loads."""
        from pipelines.option import OptionPipeline
        from pipelines import AIResult
        from types import SimpleNamespace
        captured = {}

        def fake_run_ensemble(**kwargs):
            captured.update(kwargs)
            return {"per_symbol": {"CWAN": {"vetoed": False}}}

        with patch("ensemble.run_ensemble",
                    side_effect=fake_run_ensemble):
            OptionPipeline().route_to_specialists(
                SimpleNamespace(),
                AIResult(proposals=[
                    {"symbol": "CWAN", "signal": "MULTILEG_OPEN"}
                ]),
            )

        assert captured.get("pipeline_kind") == "option"

    def test_stock_pipeline_passes_stock_kind(self):
        from pipelines.stock import StockPipeline
        from pipelines import AIResult
        from types import SimpleNamespace
        captured = {}

        def fake_run_ensemble(**kwargs):
            captured.update(kwargs)
            return {"per_symbol": {"AAPL": {"vetoed": False}}}

        with patch("ensemble.run_ensemble",
                    side_effect=fake_run_ensemble):
            StockPipeline().route_to_specialists(
                SimpleNamespace(),
                AIResult(proposals=[{"symbol": "AAPL", "signal": "BUY"}]),
            )

        assert captured.get("pipeline_kind") == "stock"
