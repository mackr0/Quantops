"""Phase 2b — option tuner WRITES (2026-05-12).

OptionPipeline.tune() now produces actual ParameterAdjustments
to the three option-Greeks budget params:
  - max_net_options_delta_pct
  - max_theta_burn_dollars_per_day
  - max_short_vega_dollars

Adjustment math:
  - win rate ≥ 60% over ≥ 20 samples → loosen 5% (×1.05, capped)
  - win rate ≤ 40% over ≥ 20 samples → tighten 5% (×0.95, floored)
  - else: no change (don't tune on noise)

This file pins:
- TUNE OUTPUT: high win rate produces loosen-direction changes;
  low win rate produces tighten-direction; neutral band is no-op.
- SAMPLE GUARD: < MIN_SAMPLES skips even a high win rate.
- BOUNDS CLIPPING: at the ceiling, loosen is a no-op (no entry
  in changes); at the floor, tighten is a no-op.
- WRITER: apply_parameter_adjustments calls update_trading_profile
  with the right kwargs and records to tuning_history.
- HISTORY: every adjustment lands in the tuning_history table
  with old/new/rationale.
- DISPATCHER: run_pipeline_tuning iterates pipelines; calls
  tune() and apply for each.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines.option import OptionPipeline
from pipelines import Metrics
from pipelines.tuning_writer import (
    apply_parameter_adjustments,
    _ensure_tuning_history_table,
    run_pipeline_tuning,
)


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


def _seed_option_outcomes(db_path, n=25, win_rate=0.7):
    """Insert n resolved option predictions with the target win rate."""
    ts = (datetime.utcnow() - timedelta(days=10)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        for i in range(n):
            outcome = "win" if i < int(n * win_rate) else "loss"
            conn.execute(
                """INSERT INTO ai_predictions
                   (timestamp, symbol, predicted_signal, confidence,
                    reasoning, price_at_prediction, status, actual_outcome,
                    actual_return_pct, pipeline_kind)
                   VALUES (?, ?, 'MULTILEG_OPEN', 70, 'r', 0.50,
                           'resolved', ?, 5.0, 'option')""",
                (ts, f"SYM{i}", outcome),
            )
        conn.commit()
    finally:
        conn.close()


def _ctx(db_path, **overrides):
    base = {
        "db_path": db_path,
        "max_net_options_delta_pct": 0.05,
        "max_theta_burn_dollars_per_day": 50.0,
        "max_short_vega_dollars": 500.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# OptionPipeline.tune() — adjustment math
# ---------------------------------------------------------------------------

class TestOptionTuneAdjustmentMath:
    def test_high_win_rate_loosens(self, db_path):
        _seed_option_outcomes(db_path, n=25, win_rate=0.72)
        adj = OptionPipeline().tune(_ctx(db_path), Metrics(pipeline_name="option"))
        # Loosen: each new value > current
        assert adj.changes["max_net_options_delta_pct"] > 0.05
        assert adj.changes["max_theta_burn_dollars_per_day"] > 50.0
        assert adj.changes["max_short_vega_dollars"] > 500.0
        # 5% loosen: 0.05 * 1.05 = 0.0525
        assert adj.changes["max_net_options_delta_pct"] == pytest.approx(0.0525)

    def test_low_win_rate_tightens(self, db_path):
        _seed_option_outcomes(db_path, n=25, win_rate=0.30)
        adj = OptionPipeline().tune(_ctx(db_path), Metrics(pipeline_name="option"))
        # Tighten: each new value < current
        assert adj.changes["max_net_options_delta_pct"] < 0.05
        assert adj.changes["max_theta_burn_dollars_per_day"] < 50.0
        assert adj.changes["max_short_vega_dollars"] < 500.0
        # 5% tighten: 0.05 * 0.95 = 0.0475
        assert adj.changes["max_net_options_delta_pct"] == pytest.approx(0.0475)

    def test_neutral_band_no_change(self, db_path):
        _seed_option_outcomes(db_path, n=25, win_rate=0.50)
        adj = OptionPipeline().tune(_ctx(db_path), Metrics(pipeline_name="option"))
        assert adj.changes == {}

    def test_insufficient_samples_no_change(self, db_path):
        # 10 samples — below MIN_SAMPLES=20
        _seed_option_outcomes(db_path, n=10, win_rate=0.70)
        adj = OptionPipeline().tune(_ctx(db_path), Metrics(pipeline_name="option"))
        assert adj.changes == {}
        assert "insufficient samples" in adj.rationale

    def test_no_db_returns_empty(self):
        adj = OptionPipeline().tune(_ctx(None), Metrics(pipeline_name="option"))
        assert adj.changes == {}


class TestBoundsClipping:
    def test_loosen_at_ceiling_is_noop(self, db_path):
        """At ceiling (max_net_options_delta_pct=0.10), loosen
        attempts produce no delta — entry not in changes."""
        _seed_option_outcomes(db_path, n=25, win_rate=0.70)
        ctx = _ctx(db_path, max_net_options_delta_pct=0.10)
        adj = OptionPipeline().tune(ctx, Metrics(pipeline_name="option"))
        # delta_pct already at ceiling — clipped to ceiling, equals
        # current, so no entry recorded
        assert "max_net_options_delta_pct" not in adj.changes
        # Other params still adjusted (they're below their ceilings)
        assert "max_theta_burn_dollars_per_day" in adj.changes

    def test_tighten_at_floor_is_noop(self, db_path):
        _seed_option_outcomes(db_path, n=25, win_rate=0.30)
        ctx = _ctx(db_path, max_net_options_delta_pct=0.02)
        adj = OptionPipeline().tune(ctx, Metrics(pipeline_name="option"))
        assert "max_net_options_delta_pct" not in adj.changes


# ---------------------------------------------------------------------------
# Writer — apply_parameter_adjustments
# ---------------------------------------------------------------------------

class TestApplyParameterAdjustments:
    def test_writes_to_trading_profile(self, db_path):
        from pipelines import ParameterAdjustments
        adj = ParameterAdjustments(
            pipeline_name="option",
            changes={"max_net_options_delta_pct": 0.06},
            rationale="test loosen",
        )
        captured = {}

        def fake_update(profile_id, **kwargs):
            captured["profile_id"] = profile_id
            captured["kwargs"] = kwargs

        with patch("models.update_trading_profile",
                    side_effect=fake_update):
            n = apply_parameter_adjustments(
                profile_id=42, db_path=db_path,
                adjustments=adj, ctx=_ctx(db_path),
            )

        assert n == 1
        assert captured["profile_id"] == 42
        assert captured["kwargs"]["max_net_options_delta_pct"] == 0.06

    def test_records_to_tuning_history(self, db_path):
        from pipelines import ParameterAdjustments
        adj = ParameterAdjustments(
            pipeline_name="option",
            changes={"max_net_options_delta_pct": 0.06},
            rationale="loosened on 70% win",
        )
        ctx = _ctx(db_path, max_net_options_delta_pct=0.05)
        with patch("models.update_trading_profile"):
            apply_parameter_adjustments(
                profile_id=42, db_path=db_path,
                adjustments=adj, ctx=ctx,
            )

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT pipeline_name, param_name, old_value, "
            "new_value, rationale FROM tuning_history"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "option"
        assert rows[0][1] == "max_net_options_delta_pct"
        assert rows[0][2] == pytest.approx(0.05)
        assert rows[0][3] == pytest.approx(0.06)
        assert "loosened" in rows[0][4]

    def test_empty_changes_skips_write(self, db_path):
        from pipelines import ParameterAdjustments
        adj = ParameterAdjustments(
            pipeline_name="option", changes={}, rationale="noop",
        )
        called = {"count": 0}

        def fake_update(profile_id, **kwargs):
            called["count"] += 1

        with patch("models.update_trading_profile",
                    side_effect=fake_update):
            n = apply_parameter_adjustments(
                profile_id=42, db_path=db_path, adjustments=adj,
            )
        assert n == 0
        assert called["count"] == 0

    def test_no_profile_id_returns_zero(self):
        from pipelines import ParameterAdjustments
        adj = ParameterAdjustments(
            pipeline_name="option",
            changes={"max_net_options_delta_pct": 0.06},
        )
        n = apply_parameter_adjustments(
            profile_id=0, db_path="x", adjustments=adj,
        )
        assert n == 0


# ---------------------------------------------------------------------------
# Dispatcher — run_pipeline_tuning iterates pipelines
# ---------------------------------------------------------------------------

class TestRunPipelineTuning:
    def test_iterates_all_pipelines(self, db_path):
        _seed_option_outcomes(db_path, n=25, win_rate=0.72)
        ctx = SimpleNamespace(
            db_path=db_path, profile_id=42,
            max_net_options_delta_pct=0.05,
            max_theta_burn_dollars_per_day=50.0,
            max_short_vega_dollars=500.0,
        )
        with patch("models.update_trading_profile"):
            results = run_pipeline_tuning(ctx)
        assert "option" in results
        assert "stock" in results
        # Option pipeline produced changes (loosened on 72% wins)
        assert results["option"] > 0
        # Stock pipeline currently doesn't write (no schema yet)
        assert results["stock"] == 0

    def test_no_profile_id_skips_writes(self, db_path):
        _seed_option_outcomes(db_path, n=25, win_rate=0.72)
        ctx = SimpleNamespace(db_path=db_path)  # no profile_id
        results = run_pipeline_tuning(ctx)
        # All pipelines reported zero writes
        assert all(v == 0 for v in results.values())


# ---------------------------------------------------------------------------
# Schema migration — columns exist + allowed_cols accepts them
# ---------------------------------------------------------------------------

class TestSchemaMigration:
    def test_allowed_cols_includes_new_params(self):
        """Without this allowlist entry, update_trading_profile
        silently drops the kwargs (the 2026-04-28 disabled_specialists
        bug class)."""
        import inspect
        import models
        src = inspect.getsource(models.update_trading_profile)
        assert "max_net_options_delta_pct" in src
        assert "max_theta_burn_dollars_per_day" in src
        assert "max_short_vega_dollars" in src

    def test_migration_list_includes_new_columns(self):
        """The _migrations list in init_user_db must include the
        three Greek-budget columns so the ALTER TABLE fires."""
        import inspect
        import models
        src = inspect.getsource(models.init_user_db)
        assert "max_net_options_delta_pct" in src
        assert "max_theta_burn_dollars_per_day" in src
        assert "max_short_vega_dollars" in src
