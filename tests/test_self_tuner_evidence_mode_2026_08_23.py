"""Self-tuner cut to evidence-backed levers (docs/25 step 3).

Experiment 1's tuner made 429 changes with no measurable improvement,
mostly on knobs that do not bind (`max_total_positions`) or that
reverse themselves by auto-expiry. Phase 1 runs the tuner in
"evidence" mode: only the optimizers on EVIDENCE_BACKED_OPTIMIZERS run,
and `max_total_positions` is refused at the single apply choke point
no matter which path proposes it.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import self_tuning as st  # noqa: E402


class TestAllowlist:
    def test_every_allowlisted_optimizer_exists(self):
        for name in st.EVIDENCE_BACKED_OPTIMIZERS:
            assert callable(getattr(st, name, None)), name

    def test_prompt_layout_is_not_allowlisted(self):
        """Arms must see identical prompts — a per-profile prompt-layout
        tuner would confound the model comparison."""
        assert "_optimize_prompt_layout" not in st.EVIDENCE_BACKED_OPTIMIZERS

    def test_non_binding_and_self_reversing_levers_are_out(self):
        for name in ("_optimize_max_total_positions", "_optimize_atr_multiplier_tp",
                     "_optimize_drawdown_thresholds", "_optimize_symbol_overrides",
                     "_optimize_fast_lane_retirement", "_optimize_regime_overrides"):
            assert name not in st.EVIDENCE_BACKED_OPTIMIZERS, name

    def test_default_mode_is_evidence(self, monkeypatch):
        monkeypatch.delenv("SELF_TUNER_MODE", raising=False)
        assert st._tuner_mode() == "evidence"


class TestDispatch:
    def _record_all(self, monkeypatch):
        called = []

        def _fake_for(name):
            def _fake(*a, **k):
                called.append(name)
                return None
            _fake.__name__ = name   # the dispatcher filters by __name__
            return _fake

        for name in dir(st):
            if name.startswith("_optimize_") and callable(getattr(st, name)):
                monkeypatch.setattr(st, name, _fake_for(name))
        return called

    def test_evidence_mode_runs_only_allowlisted(self, monkeypatch):
        monkeypatch.setenv("SELF_TUNER_MODE", "evidence")
        called = self._record_all(monkeypatch)
        st._apply_upward_optimizations(None, object(), 1, 1, 50.0, 100)
        assert called and set(called) <= set(st.EVIDENCE_BACKED_OPTIMIZERS)
        assert "_optimize_signal_weights" in called

    def test_full_mode_runs_everything_registered(self, monkeypatch):
        monkeypatch.setenv("SELF_TUNER_MODE", "full")
        called = self._record_all(monkeypatch)
        st._apply_upward_optimizations(None, object(), 1, 1, 50.0, 100)
        assert "_optimize_max_total_positions" in called
        assert len(called) > len(st.EVIDENCE_BACKED_OPTIMIZERS)


class TestFirewall:
    def test_max_total_positions_is_refused_at_the_choke_point(self, monkeypatch, caplog):
        import models
        monkeypatch.setattr(models, "update_trading_profile",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("must not write")))
        value, clamped, suffix = st._apply_param_change(
            1, 1, "concentration_reduce", "max_total_positions", 999, 749,
            "Concentration risk")
        assert value == 999 and clamped is False
        assert "refused" in suffix
        assert "non-binding" in caplog.text

    def test_other_params_still_pass(self, monkeypatch):
        import models
        writes = []
        monkeypatch.setattr(models, "update_trading_profile",
                            lambda pid, **kw: writes.append(kw))
        monkeypatch.setattr(models, "log_tuning_change", lambda *a, **k: None)
        monkeypatch.setattr(models, "get_param_reference", lambda *a, **k: None)
        monkeypatch.setattr(models, "record_param_reference_if_absent",
                            lambda *a, **k: None)
        # 1.0 → 0.8 is inside the ±25% per-cycle cap, so it lands as-is.
        st._apply_param_change(1, 1, "signal_weight_down",
                               "weight:vwap_position", 1.0, 0.8, "evidence")
        assert len(writes) == 1
        assert float(writes[0]["weight:vwap_position"]) == 0.8
