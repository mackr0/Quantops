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
        # 2026-05-12 — 8 new AI-tunable option params. Defaults
        # match user_context.py / models.py.
        "option_premium_stop_loss_pct": -0.50,
        "option_premium_take_profit_pct": 1.00,
        "option_dte_exit_threshold_days": 7,
        "option_short_premium_take_profit_pct": -0.50,
        "option_short_premium_stop_loss_pct": 1.00,
        "option_spread_iv_rank_veto_threshold": 80.0,
        "option_spread_gamma_dte_veto_threshold": 7,
        "option_spread_credit_ratio_veto_threshold": 0.20,
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

    def test_records_to_canonical_tuning_history(self, db_path):
        """Phase 2b records via the canonical models.log_tuning_change
        helper (writes to main config DB) — NOT a duplicate per-
        profile table. Operators see pipeline-tuner adjustments in
        the same history panel as legacy self-tuner adjustments."""
        from pipelines import ParameterAdjustments
        adj = ParameterAdjustments(
            pipeline_name="option",
            changes={"max_net_options_delta_pct": 0.06},
            rationale="loosened on 70% win",
        )
        ctx = _ctx(db_path, max_net_options_delta_pct=0.05, user_id=1)
        captured = []

        def fake_log(profile_id, user_id, adjustment_type,
                       parameter_name, old_value, new_value, reason,
                       **kwargs):
            captured.append({
                "profile_id": profile_id, "user_id": user_id,
                "adjustment_type": adjustment_type,
                "parameter_name": parameter_name,
                "old_value": old_value, "new_value": new_value,
                "reason": reason,
            })
            return 1

        with patch("models.update_trading_profile"), \
             patch("models.log_tuning_change", side_effect=fake_log):
            apply_parameter_adjustments(
                profile_id=42, db_path=db_path,
                adjustments=adj, ctx=ctx,
            )

        assert len(captured) == 1
        assert captured[0]["profile_id"] == 42
        assert captured[0]["user_id"] == 1
        assert captured[0]["adjustment_type"] == "pipeline_tuner_option"
        assert captured[0]["parameter_name"] == "max_net_options_delta_pct"
        assert captured[0]["old_value"] == "0.05"
        assert captured[0]["new_value"] == "0.06"
        assert "loosened" in captured[0]["reason"]

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


# ---------------------------------------------------------------------------
# 2026-05-12 — 8 NEW AI-tunable option params (exit + veto thresholds)
# ---------------------------------------------------------------------------

NEW_TUNABLE_PARAMS = [
    "option_premium_stop_loss_pct",
    "option_premium_take_profit_pct",
    "option_dte_exit_threshold_days",
    "option_short_premium_take_profit_pct",
    "option_short_premium_stop_loss_pct",
    "option_spread_iv_rank_veto_threshold",
    "option_spread_gamma_dte_veto_threshold",
    "option_spread_credit_ratio_veto_threshold",
]


class TestNewTunableParamsSchema:
    """The whole premise: the AI should figure out these values from
    outcome data. The schema, allowlist, and loader must all carry
    each one or the tuner silently no-ops."""

    def test_migration_list_includes_all_new_params(self):
        import inspect
        import models
        src = inspect.getsource(models.init_user_db)
        for p in NEW_TUNABLE_PARAMS:
            assert p in src, f"missing migration entry for {p}"

    def test_allowed_cols_includes_all_new_params(self):
        import inspect
        import models
        src = inspect.getsource(models.update_trading_profile)
        for p in NEW_TUNABLE_PARAMS:
            assert p in src, f"missing allowed_cols entry for {p}"

    def test_user_context_dataclass_has_all_new_params(self):
        from user_context import UserContext
        ctx = UserContext(user_id=1, segment="t")
        for p in NEW_TUNABLE_PARAMS:
            assert hasattr(ctx, p), f"UserContext missing field {p}"


class TestNewTunableParamsTuning:
    """OptionPipeline.tune() must adjust each new param with the
    right per-param direction. The crux: most caps go UP when
    loosening, but DTE-based exits and gamma-DTE/credit-ratio vetoes
    go DOWN when loosening. Sign errors here would invert tuning."""

    def test_high_win_rate_loosens_all_8_params(self, db_path):
        _seed_option_outcomes(db_path, n=25, win_rate=0.72)
        # DTE-based params at default 7 with 0.95 multiplier round
        # back to 7 (no-op by design — bounded integer). Bump them
        # so we verify the tuner reaches each param.
        ctx = _ctx(
            db_path,
            option_dte_exit_threshold_days=11,
            option_spread_gamma_dte_veto_threshold=11,
        )
        adj = OptionPipeline().tune(ctx, Metrics(pipeline_name="option"))
        for p in NEW_TUNABLE_PARAMS:
            assert p in adj.changes, f"loosen produced no change for {p}"

    def test_loosen_direction_per_param(self, db_path):
        """Each param has a deliberate direction. This pins it."""
        _seed_option_outcomes(db_path, n=25, win_rate=0.72)
        adj = OptionPipeline().tune(_ctx(db_path), Metrics(pipeline_name="option"))
        # LONG stop = -0.50; loosen = more negative (let positions
        # ride further)
        assert adj.changes["option_premium_stop_loss_pct"] < -0.50
        # LONG TP = 1.00; loosen = higher (let winners run)
        assert adj.changes["option_premium_take_profit_pct"] > 1.00
        # DTE exit = 7; loosen = LOWER (close less aggressively
        # near expiry). 7 * 0.95 = 6.65 → rounds to 7 — too close.
        # Use a higher current to verify the rounding crosses.
        ctx_dte = _ctx(db_path, option_dte_exit_threshold_days=11)
        adj_dte = OptionPipeline().tune(ctx_dte, Metrics(pipeline_name="option"))
        assert adj_dte.changes["option_dte_exit_threshold_days"] < 11
        # IV-rank veto = 80; loosen = HIGHER (veto less often)
        assert adj.changes["option_spread_iv_rank_veto_threshold"] > 80.0
        # Gamma-DTE veto = 7; loosen = LOWER (use the same workaround)
        ctx_gam = _ctx(db_path, option_spread_gamma_dte_veto_threshold=11)
        adj_gam = OptionPipeline().tune(ctx_gam, Metrics(pipeline_name="option"))
        assert adj_gam.changes["option_spread_gamma_dte_veto_threshold"] < 11
        # Credit-ratio veto = 0.20; loosen = LOWER (accept thinner
        # credits)
        assert adj.changes["option_spread_credit_ratio_veto_threshold"] < 0.20

    def test_tighten_direction_per_param(self, db_path):
        _seed_option_outcomes(db_path, n=25, win_rate=0.30)
        adj = OptionPipeline().tune(_ctx(db_path), Metrics(pipeline_name="option"))
        # LONG stop = -0.50; tighten = less negative (exit sooner)
        assert adj.changes["option_premium_stop_loss_pct"] > -0.50
        # LONG TP = 1.00; tighten = lower (lock in sooner)
        assert adj.changes["option_premium_take_profit_pct"] < 1.00
        # IV-rank veto = 80; tighten = LOWER (veto more aggressively)
        assert adj.changes["option_spread_iv_rank_veto_threshold"] < 80.0
        # Credit-ratio veto = 0.20; tighten = HIGHER (demand richer credits)
        assert adj.changes["option_spread_credit_ratio_veto_threshold"] > 0.20

    def test_bounds_clipping_prevents_runaway(self, db_path):
        """Repeated loosening doesn't push past ceiling. Repeated
        tightening doesn't push past floor."""
        _seed_option_outcomes(db_path, n=25, win_rate=0.72)
        # Ceiling check: option_premium_take_profit_pct ceiling = 2.0
        ctx = _ctx(db_path, option_premium_take_profit_pct=2.0)
        adj = OptionPipeline().tune(ctx, Metrics(pipeline_name="option"))
        # At ceiling — clipped to ceiling = current → no change recorded
        assert "option_premium_take_profit_pct" not in adj.changes

    def test_integer_params_stay_integer(self, db_path):
        """option_dte_exit_threshold_days + gamma_dte are INTEGER
        columns. Tuner must round, not write a float."""
        _seed_option_outcomes(db_path, n=25, win_rate=0.30)
        adj = OptionPipeline().tune(
            _ctx(db_path, option_dte_exit_threshold_days=8),
            Metrics(pipeline_name="option"),
        )
        if "option_dte_exit_threshold_days" in adj.changes:
            v = adj.changes["option_dte_exit_threshold_days"]
            assert isinstance(v, int), f"DTE param must be int, got {type(v)}"


class TestOptionsExitsCtxResolution:
    """options_exits.check_single_leg_option_exits must read
    thresholds from ctx when provided. Without ctx → fall back to
    module defaults (legacy callers / tests)."""

    def test_resolve_thresholds_reads_ctx_when_present(self):
        from options_exits import _resolve_thresholds
        ctx = SimpleNamespace(
            option_premium_stop_loss_pct=-0.60,
            option_premium_take_profit_pct=1.50,
            option_dte_exit_threshold_days=5,
            option_short_premium_take_profit_pct=-0.40,
            option_short_premium_stop_loss_pct=1.20,
        )
        t = _resolve_thresholds(ctx)
        assert t["premium_stop_loss_pct"] == -0.60
        assert t["premium_take_profit_pct"] == 1.50
        assert t["dte_exit_threshold_days"] == 5
        assert t["short_premium_take_profit_pct"] == -0.40
        assert t["short_premium_stop_loss_pct"] == 1.20

    def test_resolve_thresholds_falls_back_when_ctx_none(self):
        from options_exits import (
            _resolve_thresholds, PREMIUM_STOP_LOSS_PCT,
            PREMIUM_TAKE_PROFIT_PCT, DTE_EXIT_THRESHOLD_DAYS,
        )
        t = _resolve_thresholds(None)
        assert t["premium_stop_loss_pct"] == PREMIUM_STOP_LOSS_PCT
        assert t["premium_take_profit_pct"] == PREMIUM_TAKE_PROFIT_PCT
        assert t["dte_exit_threshold_days"] == DTE_EXIT_THRESHOLD_DAYS

    def test_check_uses_per_profile_threshold(self, db_path):
        """Position with -55% premium drop. Default threshold of
        -50% would trigger; a tuned -60% threshold should NOT."""
        from options_exits import check_single_leg_option_exits

        pos = {
            "occ_symbol": "AAPL  250620P00150000",
            "qty": 1,
            "avg_entry_price": 1.00,
            "current_price": 0.45,  # -55% drop
            "is_option": True,
        }

        ctx_default = SimpleNamespace(
            option_premium_stop_loss_pct=-0.50,
        )
        sigs = check_single_leg_option_exits(
            [pos], db_path, ctx=ctx_default,
        )
        assert any(s["trigger"] == "premium_stop" for s in sigs)

        # Same position, looser tuned threshold — should NOT fire
        ctx_looser = SimpleNamespace(
            option_premium_stop_loss_pct=-0.60,
        )
        sigs = check_single_leg_option_exits(
            [pos], db_path, ctx=ctx_looser,
        )
        assert not any(s["trigger"] == "premium_stop" for s in sigs)


class TestOptionSpreadRiskCtxResolution:
    """The veto-threshold values must surface in the prompt text,
    so a tuned threshold actually changes what the LLM is told."""

    def test_prompt_uses_ctx_iv_rank_threshold(self):
        from specialists.option_spread_risk import build_prompt
        ctx = SimpleNamespace(
            option_spread_iv_rank_veto_threshold=65.0,
            option_spread_gamma_dte_veto_threshold=5,
            option_spread_credit_ratio_veto_threshold=0.30,
            max_per_trade_loss=500,
        )
        prompt = build_prompt(
            [{"symbol": "AAPL", "strategy_name": "iron_condor"}],
            ctx,
        )
        assert "iv_rank > 65" in prompt
        assert "DTE < 5" in prompt
        assert "at least 0.30" in prompt

    def test_prompt_falls_back_to_default_thresholds(self):
        from specialists.option_spread_risk import build_prompt
        ctx = SimpleNamespace(max_per_trade_loss=500)  # no veto attrs
        prompt = build_prompt(
            [{"symbol": "AAPL", "strategy_name": "iron_condor"}],
            ctx,
        )
        assert "iv_rank > 80" in prompt
        assert "DTE < 7" in prompt
        assert "at least 0.20" in prompt
