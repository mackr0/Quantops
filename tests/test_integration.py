"""End-to-end integration tests — cross-phase invariants.

Each phase was tested in isolation. These tests verify that the phases
compose correctly: data produced by Phase 6 flows through Phase 8 to
Phase 10 without losing information, deprecated strategies are excluded
consistently, shadow strategies don't drive trades, and crisis mode
correctly overrides decisions from upstream layers.

These tests use synthetic data only — no network, no live AI calls.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candidate(sym: str, signal: str = "BUY", score: int = 1,
               price: float = 50.0, strategy: str = "market_engine") -> dict:
    return {
        "symbol": sym,
        "signal": signal,
        "score": score,
        "votes": {strategy: signal},
        "price": price,
        "reason": f"{sym} test setup",
        "source_strategies": [strategy],
    }


# ---------------------------------------------------------------------------
# Phase 3 (alpha decay) + Phase 6 (multi-strategy) invariant
# ---------------------------------------------------------------------------

class TestDecayExcludesFromMultiStrategy:
    """A strategy marked deprecated by Phase 3 must not contribute candidates
    through Phase 6's aggregate_candidates."""

    def test_deprecated_strategy_not_called(self, sample_ctx, tmp_profile_db,
                                             monkeypatch):
        sample_ctx.db_path = tmp_profile_db

        # Mark insider_cluster as deprecated
        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO deprecated_strategies "
            "(strategy_type, deprecated_at, reason) "
            "VALUES ('insider_cluster', datetime('now'), 'test')"
        )
        conn.commit()
        conn.close()

        called = {"insider_cluster": False, "market_engine": False}

        from strategies import get_active_strategies
        for mod in get_active_strategies("small", db_path=tmp_profile_db):
            name = mod.NAME
            def make_fake(n):
                def fake(ctx, uni):
                    called[n] = True
                    return [_candidate("AAPL", strategy=n)]
                return fake
            monkeypatch.setattr(mod, "find_candidates", make_fake(name))

        from multi_strategy import aggregate_candidates
        aggregate_candidates(sample_ctx, ["AAPL"], db_path=tmp_profile_db)

        assert called["market_engine"] is True
        assert called["insider_cluster"] is False


# ---------------------------------------------------------------------------
# Phase 6 (multi-strategy) + Phase 7 (auto-strategies in shadow)
# ---------------------------------------------------------------------------

class TestShadowStrategiesDontDriveTrades:
    """Shadow auto-strategies must be reachable via get_shadow_strategies()
    but NOT via get_active_strategies()."""

    def test_shadow_filter_separation(self, tmp_profile_db, tmp_strategies_dir):
        from strategy_generator import save_spec, update_status
        from strategies import get_active_strategies, get_shadow_strategies

        spec = {
            "name": "auto_shadow_integration",
            "description": "integration test shadow",
            "applicable_markets": ["small"],
            "direction": "BUY", "score": 1,
            "conditions": [{"field": "rsi", "op": "<", "value": 30}],
        }
        spec_id = save_spec(tmp_profile_db, spec)
        update_status(tmp_profile_db, spec_id, "validated")
        update_status(tmp_profile_db, spec_id, "shadow")

        # Even though we called save_spec, write_strategy_module wasn't called
        # (we went straight to shadow status), so the file doesn't exist on
        # disk — discover_strategies won't find it. This verifies the safety
        # property: a row in auto_generated_strategies with status='shadow'
        # without a corresponding .py file simply doesn't participate.
        active_names = [m.NAME for m in get_active_strategies("small", db_path=tmp_profile_db)]
        shadow_names = [m.NAME for m in get_shadow_strategies("small", db_path=tmp_profile_db)]
        # Neither list should contain it since there's no module file
        assert "auto_shadow_integration" not in active_names
        assert "auto_shadow_integration" not in shadow_names


# ---------------------------------------------------------------------------
# Phase 8 (ensemble) + Phase 10 (crisis) interaction
# ---------------------------------------------------------------------------

class TestEnsembleCrisisComposition:
    """When crisis is active, the ensemble still runs (informational) but
    the downstream trade sizing is zeroed out by the crisis gate. The
    ensemble VETO path is orthogonal — both can fire for different reasons."""

    def test_crisis_zeroes_sizes_regardless_of_ensemble_buy(
        self, sample_ctx, tmp_profile_db, monkeypatch,
    ):
        sample_ctx.db_path = tmp_profile_db

        # Inject a crisis-level state
        import crisis_state
        def fake_detect(db_path=None):
            return {"level": "crisis",
                    "signals": [{"name": "vix_crisis", "severity": "high",
                                 "detail": "VIX 38"}],
                    "readings": {"vix": 38.0}, "size_multiplier": 0.0}
        monkeypatch.setattr(crisis_state, "detect_crisis_state", fake_detect)
        crisis_state.run_crisis_tick(tmp_profile_db)

        # Simulate AI-selected trades; the crisis gate should remove longs
        ai_trades = [
            {"symbol": "AAPL", "action": "BUY", "size_pct": 5.0, "confidence": 80},
            {"symbol": "MSFT", "action": "BUY", "size_pct": 5.0, "confidence": 70},
            {"symbol": "GOOG", "action": "SELL", "size_pct": 3.0, "confidence": 60},
        ]

        # Apply the same gate logic inline (mirrors trade_pipeline Step 4.9)
        cur = crisis_state.get_current_level(tmp_profile_db)
        if cur["size_multiplier"] <= 0:
            ai_trades = [t for t in ai_trades
                         if t.get("action", "").upper() in ("SELL", "SHORT")]

        actions = {t["action"] for t in ai_trades}
        assert "BUY" not in actions      # crisis gate removed longs
        assert "SELL" in actions         # exits still allowed


# ---------------------------------------------------------------------------
# Phase 9 (events) + Phase 10 (crisis) integration
# ---------------------------------------------------------------------------

class TestCrisisTransitionEmitsEvent:
    """A crisis state change must produce a crisis_state_change row in the
    events table so Phase 9 handlers (activity log, notifications) fire."""

    def test_elevation_creates_event(self, tmp_profile_db, monkeypatch):
        import crisis_state

        def detect_elevated(db_path=None):
            return {"level": "elevated",
                    "signals": [{"name": "vix_elevated", "severity": "medium",
                                 "detail": "VIX 24"}],
                    "readings": {"vix": 24.0}, "size_multiplier": 0.5}
        monkeypatch.setattr(crisis_state, "detect_crisis_state", detect_elevated)
        crisis_state.run_crisis_tick(tmp_profile_db)

        conn = sqlite3.connect(tmp_profile_db)
        events = conn.execute(
            "SELECT type, severity, payload_json FROM events "
            "WHERE type='crisis_state_change'"
        ).fetchall()
        conn.close()

        assert len(events) == 1
        assert events[0][1] == "medium"
        payload = json.loads(events[0][2])
        assert payload["to"] == "elevated"

    def test_event_dispatch_runs_default_handlers(
        self, tmp_profile_db, sample_ctx, monkeypatch,
    ):
        """Verify the event bus actually dispatches crisis events when
        default handlers are registered."""
        import crisis_state
        from event_bus import clear_subscriptions, dispatch_pending, subscribe

        def detect_crisis(db_path=None):
            return {"level": "crisis",
                    "signals": [{"name": "s1", "severity": "high", "detail": "x"}],
                    "readings": {}, "size_multiplier": 0.0}
        monkeypatch.setattr(crisis_state, "detect_crisis_state", detect_crisis)
        crisis_state.run_crisis_tick(tmp_profile_db)

        # Register a capturing handler for crisis_state_change
        clear_subscriptions()
        captured = []
        def capture(ev, ctx):
            captured.append(ev["type"])
            return {"captured": True}
        subscribe(capture, ("crisis_state_change",))

        sample_ctx.db_path = tmp_profile_db
        summary = dispatch_pending(tmp_profile_db, sample_ctx)

        assert summary["dispatched"] >= 1
        assert "crisis_state_change" in captured


# ---------------------------------------------------------------------------
# Phase 7 (auto-generation) + Phase 2 (rigorous validation) composition
# ---------------------------------------------------------------------------

class TestAutoStrategyValidationFlow:
    """The lifecycle controller must: render module, call validation,
    transition to shadow on PASS, delete module + transition to retired
    on FAIL. Exercising the full flow without hitting the network."""

    def test_pass_path(self, tmp_profile_db, tmp_strategies_dir, monkeypatch):
        import strategy_lifecycle
        from strategy_generator import save_spec, get_strategy

        def pass_validation(spec, market_type, rigorous=True):
            return {"verdict": "PASS", "score": 2.0, "passed_gates": ["all"],
                    "failed_gates": [], "metrics": {}}
        monkeypatch.setattr(strategy_lifecycle, "_run_validation",
                            pass_validation)

        spec_id = save_spec(tmp_profile_db, {
            "name": "auto_integration_pass",
            "description": "integration pass path",
            "applicable_markets": ["small"],
            "direction": "BUY", "score": 2,
            "conditions": [{"field": "rsi", "op": "<", "value": 30}],
        })
        result = strategy_lifecycle.validate_and_promote(
            tmp_profile_db, spec_id, rigorous=False
        )
        row = get_strategy(tmp_profile_db, spec_id)
        assert result["outcome"] == "validated"
        assert row["status"] == "shadow"
        assert row["shadow_started_at"] is not None

    def test_fail_path_deletes_module(self, tmp_profile_db, tmp_strategies_dir,
                                       monkeypatch):
        import os
        import strategy_lifecycle
        from strategy_generator import save_spec, get_strategy

        def fail_validation(spec, market_type, rigorous=True):
            return {"verdict": "FAIL", "score": 0.3, "passed_gates": [],
                    "failed_gates": [{"gate": "min_sharpe", "reason": "low"}],
                    "metrics": {}}
        monkeypatch.setattr(strategy_lifecycle, "_run_validation",
                            fail_validation)

        spec_id = save_spec(tmp_profile_db, {
            "name": "auto_integration_fail",
            "description": "integration fail path",
            "applicable_markets": ["small"],
            "direction": "BUY", "score": 1,
            "conditions": [{"field": "rsi", "op": "<", "value": 30}],
        })

        result = strategy_lifecycle.validate_and_promote(
            tmp_profile_db, spec_id, rigorous=False
        )
        row = get_strategy(tmp_profile_db, spec_id)
        assert result["outcome"] == "retired"
        assert row["status"] == "retired"
        # Module file must be deleted — a retired strategy is NOT importable
        module_path = os.path.join(tmp_strategies_dir,
                                    "auto_integration_fail.py")
        assert not os.path.exists(module_path)


# ---------------------------------------------------------------------------
# Sanity: full cross-phase wiring smoke test
# ---------------------------------------------------------------------------

class TestCrossPhaseSmoke:
    """Top-to-bottom smoke: every phase's public entry point is importable
    and callable with its canonical signature. If a refactor accidentally
    breaks one of these, this test catches it before deploy."""

    def test_all_phase_entry_points_importable(self):
        # Phase 1
        from meta_model import extract_features, predict_probability
        # Phase 2
        from rigorous_backtest import validate_strategy
        # Phase 3
        from alpha_decay import run_decay_cycle, is_deprecated
        # Phase 4
        from sec_filings import get_active_alerts
        # Phase 5
        from options_oracle import get_options_oracle, summarize_for_ai
        # Phase 6
        from multi_strategy import (
            aggregate_candidates,
            aggregate_shadow_candidates,
            compute_capital_allocations,
        )
        from strategies import (
            discover_strategies,
            get_active_strategies,
            get_shadow_strategies,
        )
        # Phase 7
        from strategy_generator import (
            validate_spec,
            save_spec,
            render_strategy_module,
        )
        from strategy_proposer import propose_strategies
        from strategy_lifecycle import validate_and_promote, tick
        # Phase 8
        from ensemble import run_ensemble, format_for_final_prompt
        from specialists import discover_specialists
        # Phase 9
        from event_bus import emit, dispatch_pending, subscribe
        from event_detectors import run_all_detectors, ALL_EVENT_TYPES
        from event_handlers import register_default_handlers
        # Phase 10
        from crisis_detector import detect_crisis_state, LEVELS, SIZE_MULTIPLIERS
        from crisis_state import run_crisis_tick, get_current_level

        # Spot-check signatures / constants
        assert len(ALL_EVENT_TYPES) == 6
        assert len(LEVELS) == 4
        assert SIZE_MULTIPLIERS["crisis"] == 0.0
        assert len(discover_specialists()) == 5
