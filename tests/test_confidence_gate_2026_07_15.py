"""Confidence-gate scale repair + live-gate wiring (2026-07-15).

What was broken (whole chain, found by the robot-view health check):
  1. ai_confidence_threshold was seeded as FRACTIONS (0.60/0.65/0.55)
     against a 0-100 integer gate — the filter passed everything.
  2. The only comparison site (ai_review) had ZERO callers — even a
     correctly-scaled threshold gated nothing.
  3. The tuner's ±25% clamp, anchored to the fraction, turned "raise
     to 70" into 0.75, and _cast_param_value's int() TRUNCATED it to 0
     — six prod profiles held threshold 0 while tuning_history claimed
     0.75/0.8125 (ledger/config divergence).
  4. param_references froze the fraction as the day-1 anchor, making
     zero an absorbing state.

Pinned here: one scale (0-100 int) everywhere; one canonical value
dual-written; the LIVE gate at run_trade_cycle STEP 4.85 (entries
only, exits never); fraction writes refused at the model layer;
fraction reads normalized loudly at ctx build; voided ledger rows
inert to cooldown/effectiveness/expiry; the pipeline tuner writer
aligned with the funnel's guardrails.
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# Scale contract — seeds, schema, gate all speak 0-100 int
# ---------------------------------------------------------------------------

class TestScaleContract:
    def test_manifest_seeds_are_gate_scale_integers(self):
        """Every ai_confidence_threshold in the experiment manifest must
        be an int on the 0-100 gate scale (inside PARAM_BOUNDS). The
        fraction seeds disabled the filter for the whole cohort."""
        from create_experiment_profiles import PROFILES
        from param_bounds import PARAM_BOUNDS
        lo, hi = PARAM_BOUNDS["ai_confidence_threshold"]
        seeded = 0
        for prof in PROFILES:
            if "ai_confidence_threshold" not in prof:
                continue
            seeded += 1
            v = prof["ai_confidence_threshold"]
            assert isinstance(v, int), (
                f"{prof['name']}: seed {v!r} is not an int — the gate "
                f"compares 0-100 integer confidence")
            assert lo <= v <= hi, (
                f"{prof['name']}: seed {v} outside PARAM_BOUNDS "
                f"({lo}, {hi})")
        assert seeded >= 10, "expected all 10 AI profiles to carry a seed"

    def test_update_trading_profile_refuses_fractions(self, monkeypatch):
        """The model-layer writer is the choke point every path crosses
        — a fraction here is always a caller scale bug and must raise,
        not be stored."""
        import models
        with pytest.raises(ValueError):
            models.update_trading_profile(999999,
                                          ai_confidence_threshold=0.6)

    def test_update_trading_profile_rounds_numeric_values(self, monkeypatch):
        """Legit numeric writes are coerced to int(round()) so the
        INTEGER column never stores a REAL again."""
        import models
        captured = {}

        class _FakeConn:
            def execute(self, sql, values):
                captured["values"] = values
            def commit(self):
                pass
            def close(self):
                pass
        monkeypatch.setattr(models, "_get_conn", lambda *a, **k: _FakeConn())
        models.update_trading_profile(1, ai_confidence_threshold=65.4)
        assert captured["values"][0] == 65
        assert isinstance(captured["values"][0], int)

    def test_ctx_normalization_screams_on_fractions(self, caplog):
        """Defense in depth at the read side: a fraction-scale stored
        value (scale corruption) logs ERROR and falls back to the
        schema default 25 — it must never silently gate at 0.6."""
        import logging
        from models import _normalize_confidence_threshold
        with caplog.at_level(logging.ERROR):
            v = _normalize_confidence_threshold(0.6, 210)
        assert v == 25
        assert any("fraction-scaled" in r.message for r in caplog.records)
        # sane values pass through as ints, no noise
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            assert _normalize_confidence_threshold(60, 210) == 60
            assert _normalize_confidence_threshold(65.0, 210) == 65
        assert not caplog.records


# ---------------------------------------------------------------------------
# The live gate — STEP 4.85 source pins + behavior replica
# ---------------------------------------------------------------------------

class TestLiveGateSource:
    def test_dead_ai_review_is_gone(self):
        """ai_review was the only consumer of the threshold and had no
        callers — the parameter gated NOTHING. It must stay deleted so
        there is exactly ONE gate site."""
        import trade_pipeline
        src = inspect.getsource(trade_pipeline)
        assert "def ai_review(" not in src, (
            "ai_review is back — the confidence gate must have exactly "
            "one site (run_trade_cycle STEP 4.85)")

    def test_gate_exists_in_live_path(self):
        import trade_pipeline
        src = inspect.getsource(trade_pipeline)
        assert "STEP 4.85: Confidence gate" in src
        assert '"CONFIDENCE_GATE"' in src, (
            "gated entries must be recorded as trade drops so the AI "
            "Brain shows the specific reason")
        assert "CONFIDENCE_BLOCKED" in src, (
            "gated entries must surface in the cycle details")

    def test_gate_uses_the_override_resolve_chain(self):
        """The tuner writes per-symbol/regime/TOD overrides — the gate
        must consult resolve_for_current_regime or those override
        writers are dead knobs again. Tested against the REAL extracted
        function."""
        import trade_pipeline
        src = inspect.getsource(trade_pipeline._apply_confidence_gate)
        assert "resolve_for_current_regime" in src
        assert 'default=ctx.ai_confidence_threshold' in src
        assert "run_trade_cycle" not in src  # it IS the gate, not a copy
        # and run_trade_cycle actually calls it on the live path
        cycle_src = inspect.getsource(trade_pipeline.run_trade_cycle)
        assert "_apply_confidence_gate(ai_trades, ctx, cycle_id" in cycle_src

    def test_gate_scope_is_the_single_entry_action_constant(self):
        """ONE constant governs 'this action opens risk' for BOTH the
        confidence gate and the intraday-risk gate — two hand-kept
        copies drifted before (hack-hunt #6)."""
        import trade_pipeline
        assert trade_pipeline.NEW_ENTRY_ACTIONS == frozenset(
            {"BUY", "SHORT", "OPTIONS", "MULTILEG_OPEN", "PAIR_TRADE"})
        gate_src = inspect.getsource(trade_pipeline._apply_confidence_gate)
        assert "NEW_ENTRY_ACTIONS" in gate_src
        cycle_src = inspect.getsource(trade_pipeline.run_trade_cycle)
        assert "new_entry_actions = NEW_ENTRY_ACTIONS" in cycle_src, (
            "the intraday-risk gate must share the constant")
        # no second inline literal of the set anywhere in the module
        module_src = inspect.getsource(trade_pipeline)
        assert module_src.count(
            '"BUY", "SHORT", "OPTIONS"') == 1, (
            "a second hand-maintained entry-action literal appeared")


class TestLiveGateBehavior:
    """Behavioral tests against the REAL _apply_confidence_gate (the
    gate was extracted from run_trade_cycle precisely so no replica is
    needed; the wiring pin above proves the live path calls it)."""

    def _gate(self, trades, threshold, monkeypatch=None):
        from unittest.mock import MagicMock
        import trade_pipeline
        ctx = MagicMock()
        ctx.ai_confidence_threshold = threshold
        ctx.db_path = None  # no drop-recording DB in the unit test
        details = []
        # resolve chain falls through to the ctx global when the
        # override modules aren't seeded — exercised as-is.
        kept = trade_pipeline._apply_confidence_gate(
            trades, ctx, cycle_id=None, details=details)
        blocked = [t for t in trades if t not in kept]
        return kept, blocked, details

    def test_low_confidence_entry_blocked(self):
        kept, blocked, details = self._gate(
            [{"symbol": "A", "action": "BUY", "confidence": 55},
             {"symbol": "B", "action": "BUY", "confidence": 72}], 60)
        assert [t["symbol"] for t in kept] == ["B"]
        assert [t["symbol"] for t in blocked] == ["A"]
        assert details and details[0]["action"] == "CONFIDENCE_BLOCKED"
        assert "below this profile's minimum" in details[0]["reason"]

    def test_exits_never_gated(self):
        kept, blocked, _ = self._gate(
            [{"symbol": "A", "action": "SELL", "confidence": 5},
             {"symbol": "B", "action": "COVER", "confidence": 0},
             {"symbol": "C", "action": "MULTILEG_CLOSE", "confidence": 1}],
            90)
        assert len(kept) == 3 and not blocked

    def test_option_entries_gated_like_stock_entries(self):
        kept, blocked, _ = self._gate(
            [{"symbol": "A", "action": "MULTILEG_OPEN", "confidence": 58},
             {"symbol": "B", "action": "OPTIONS", "confidence": 70}], 60)
        assert [t["symbol"] for t in blocked] == ["A"]
        assert [t["symbol"] for t in kept] == ["B"]

    def test_missing_confidence_reads_as_zero(self):
        kept, blocked, _ = self._gate(
            [{"symbol": "A", "action": "BUY"}], 25)
        assert blocked and not kept

    def test_boundary_is_inclusive_pass(self):
        kept, blocked, _ = self._gate(
            [{"symbol": "A", "action": "BUY", "confidence": 60}], 60)
        assert kept and not blocked

    def test_blocked_entry_records_a_confidence_gate_drop(
            self, monkeypatch, tmp_path):
        """The AI Brain surface: a gated entry writes a
        CONFIDENCE_GATE trade-drop row with the gated confidence."""
        from unittest.mock import MagicMock
        import journal
        import trade_pipeline
        drops = []
        monkeypatch.setattr(journal, "record_trade_drop",
                            lambda **kw: drops.append(kw))
        ctx = MagicMock()
        ctx.ai_confidence_threshold = 60
        ctx.db_path = str(tmp_path / "p.db")
        trade_pipeline._apply_confidence_gate(
            [{"symbol": "FCEL", "action": "BUY", "confidence": 41,
              "reasoning": "momentum"}],
            ctx, cycle_id=7, details=[])
        assert drops and drops[0]["drop_code"] == "CONFIDENCE_GATE"
        assert drops[0]["ai_confidence"] == 41
        assert drops[0]["cycle_id"] == 7

    def test_resolve_chain_failure_is_loud_and_falls_back(
            self, monkeypatch, caplog):
        """Hack-hunt #7: a resolve failure silently disabling the
        tuner's overrides is the exact dead-knob class this fix pack
        repaired — it must WARN, then gate at the profile global."""
        import logging
        from unittest.mock import MagicMock
        import regime_overrides
        import trade_pipeline
        monkeypatch.setattr(
            regime_overrides, "resolve_for_current_regime",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        ctx = MagicMock()
        ctx.ai_confidence_threshold = 60
        ctx.db_path = None
        with caplog.at_level(logging.WARNING):
            kept = trade_pipeline._apply_confidence_gate(
                [{"symbol": "A", "action": "BUY", "confidence": 55}],
                ctx, cycle_id=None, details=[])
        assert kept == []  # gated at the global 60
        assert any("override resolve failed" in r.message
                   for r in caplog.records)


# ---------------------------------------------------------------------------
# Voided ledger rows are inert everywhere
# ---------------------------------------------------------------------------

class TestVoidedRowsAreInert:
    def _history(self):
        from datetime import datetime, timedelta
        recent = (datetime.utcnow() - timedelta(hours=2)).isoformat()
        return [
            {"timestamp": recent, "parameter_name": "ai_confidence_threshold",
             "outcome_after": "voided_scale_repair", "old_value": "0.6",
             "new_value": "0.75"},
        ]

    def test_voided_rows_do_not_hold_the_cooldown(self, monkeypatch):
        import self_tuning
        monkeypatch.setattr(self_tuning, "_get_tuning_history",
                            lambda *a, **k: self._history())
        assert self_tuning._get_recent_adjustment(
            1, "ai_confidence_threshold") is None, (
            "a voided row held the 3-day cooldown — the tuner can't "
            "re-tune the repaired param")

    def test_voided_rows_do_not_shadow_effectiveness(self, monkeypatch):
        import self_tuning
        from datetime import datetime, timedelta
        hist = self._history() + [{
            "timestamp": (datetime.utcnow() - timedelta(days=9)).isoformat(),
            "parameter_name": "ai_confidence_threshold",
            "outcome_after": "worsened", "old_value": "50",
            "new_value": "60"}]
        monkeypatch.setattr(self_tuning, "_get_tuning_history",
                            lambda *a, **k: hist)
        assert self_tuning._was_adjustment_effective(
            1, "ai_confidence_threshold") == "worsened", (
            "the voided row shadowed the genuine 'worsened' verdict")

    def test_auto_expiry_skips_voided_rows(self):
        from tuning_auto_expiry import (_is_eligible_for_revert,
                                        _categorize)
        # Any gate_tighten-category type exercises the filter; a voided
        # row must be refused BEFORE the TTL/sample checks so its
        # dead-scale old_value can never be replayed into the config.
        row = {"adjustment_type": "position_size_reduce",
               "outcome_after": "voided_scale_repair"}
        assert _categorize(row["adjustment_type"]) == "gate_tighten", (
            "fixture drifted — pick an adjustment_type that categorizes "
            "as gate_tighten so the voided filter is actually reached")
        eligible, reason = _is_eligible_for_revert(
            row, samples_since=999, ttl_days=0, min_samples=0)
        assert not eligible
        assert "voided" in reason

    def test_expirable_query_excludes_voided(self):
        import inspect as _i
        import models
        src = _i.getsource(models.get_expirable_tightenings)
        assert "NOT LIKE 'voided%'" in src


# ---------------------------------------------------------------------------
# The pipeline tuner writer is aligned with the funnel
# ---------------------------------------------------------------------------

class TestPipelineTunerWriterAligned:
    class _Adj:
        pipeline_name = "option"
        rationale = "test"
        def __init__(self, changes):
            self.changes = changes

    def test_operator_only_params_refused(self, monkeypatch):
        """The 4th writer used to bypass the universe-floors firewall
        entirely — a pipeline tuner could have walked min_adv down."""
        import pipelines.tuning_writer as tw
        import models
        from unittest.mock import MagicMock
        utp = MagicMock()
        monkeypatch.setattr(models, "update_trading_profile", utp)
        n = tw.apply_parameter_adjustments(
            1, "x.db", self._Adj({"min_adv": 1000}), ctx=None)
        assert n == 0
        utp.assert_not_called()

    def test_write_before_ledger_and_one_canonical_value(self, monkeypatch):
        """Profile write FIRST; ledger row records str() of the exact
        value written (cast + bounds-clamped) — never the raw proposal.
        A failed profile write leaves ZERO ledger rows."""
        import pipelines.tuning_writer as tw
        import models
        from unittest.mock import MagicMock
        calls = []
        utp = MagicMock(side_effect=lambda *a, **k: calls.append("write"))
        ltc = MagicMock(side_effect=lambda *a, **k: calls.append("ledger"))
        monkeypatch.setattr(models, "update_trading_profile", utp)
        monkeypatch.setattr(models, "log_tuning_change", ltc)
        n = tw.apply_parameter_adjustments(
            1, "x.db",
            self._Adj({"ai_confidence_threshold": "70.6"}), ctx=None)
        assert n == 1
        assert calls and calls[0] == "write", (
            "ledger must never precede the profile write")
        _, utp_kwargs = utp.call_args
        assert utp_kwargs["ai_confidence_threshold"] == 71  # round, bounds ok
        _, ltc_kwargs = ltc.call_args
        assert ltc_kwargs["new_value"] == "71"

    def test_failed_write_leaves_no_ledger_row(self, monkeypatch):
        import pipelines.tuning_writer as tw
        import models
        from unittest.mock import MagicMock
        utp = MagicMock(side_effect=RuntimeError("db locked"))
        ltc = MagicMock()
        monkeypatch.setattr(models, "update_trading_profile", utp)
        monkeypatch.setattr(models, "log_tuning_change", ltc)
        n = tw.apply_parameter_adjustments(
            1, "x.db",
            self._Adj({"ai_confidence_threshold": 70}), ctx=None)
        assert n == 0
        ltc.assert_not_called()


# ---------------------------------------------------------------------------
# param_bounds hygiene
# ---------------------------------------------------------------------------

def test_param_bounds_has_no_duplicate_keys():
    """A duplicate dict key silently wins with the LAST value —
    enable_short_selling's float duplicate defeated clamp()'s int
    preservation until 2026-07-15. Pin: no literal PARAM_BOUNDS key
    appears twice."""
    import ast
    import param_bounds
    src = inspect.getsource(param_bounds)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for tgt in targets:
            if getattr(tgt, "id", "") == "PARAM_BOUNDS" and \
                    isinstance(node.value, ast.Dict):
                keys = [k.value for k in node.value.keys
                        if isinstance(k, ast.Constant)]
                dupes = {k for k in keys if keys.count(k) > 1}
                assert not dupes, (
                    f"duplicate PARAM_BOUNDS keys: {dupes}")
                return
    pytest.fail("PARAM_BOUNDS dict literal not found")


# ---------------------------------------------------------------------------
# Adversarial-review round (2026-07-15) pins
# ---------------------------------------------------------------------------

class TestReviewRoundGateChain:
    def test_expirable_query_excludes_informational_rows(self):
        """Review #1: the scale-repair corrective row (outcome 'n/a')
        must never become an auto-expiry candidate — the expiry
        optimizer would 'unwind the tightening' 0→60 back down forever."""
        import inspect as _i
        import models
        src = _i.getsource(models.get_expirable_tightenings)
        assert "!= 'n/a'" in src

    def test_auto_expiry_revert_skips_informational_rows(self):
        from tuning_auto_expiry import _is_eligible_for_revert
        row = {"adjustment_type": "position_size_reduce",
               "outcome_after": "n/a"}
        eligible, reason = _is_eligible_for_revert(
            row, samples_since=999, ttl_days=0, min_samples=0)
        assert not eligible and "informational" in reason

    def test_migration_stamps_corrective_rows_terminal(self):
        """Review #1: the corrective INSERT must carry expired_at +
        reviewed_at + 'n/a' in one statement — three independent
        consumers key on three different columns."""
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "migrate_confidence_scale_2026_07_15.py")
                   ).read()
        i = src.find("operator_scale_repair', ")
        insert_region = src[src.rfind("INSERT INTO tuning_history", 0, i):i + 400]
        assert "expired_at" in insert_region
        assert "reviewed_at" in insert_region
        assert "'n/a'" in insert_region

    def test_migration_voids_all_fraction_era_rows_not_just_pending(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "migrate_confidence_scale_2026_07_15.py")
                   ).read()
        assert "NOT LIKE 'voided%'" in src, (
            "the void criterion must cover already-reviewed rows too — "
            "an 'unchanged' fraction-era row is still a replay hazard")

    def test_rank_candidates_keeps_held_exits_without_shorts(self):
        """Review #15: SELL/STRONG_SELL on a HELD name is an EXIT and
        must reach the AI shortlist even when shorts are disabled —
        the else-branch used to discard the entire short_eligible list,
        silencing AI exits on the three no-shorts profiles."""
        from trade_pipeline import _rank_candidates
        signals = [
            {"symbol": "HELDX", "signal": "STRONG_SELL", "score": -3},
            {"symbol": "NEWY", "signal": "BUY", "score": 3},
        ]
        out = _rank_candidates(signals, held_symbols={"HELDX"},
                               enable_shorts=False,
                               deprecated_strategies=set())
        syms = [s["symbol"] for s in out]
        assert "HELDX" in syms, "held-name exit dropped on a no-shorts book"
        assert syms[0] == "HELDX", "exits must lead — never crowded out"
        assert "NEWY" in syms
        # non-held SELL is a SHORT entry — still correctly dropped
        out2 = _rank_candidates(
            [{"symbol": "RANDO", "signal": "SELL", "score": -2}],
            held_symbols=set(), enable_shorts=False,
            deprecated_strategies=set())
        assert out2 == []

    def test_earnings_prefilter_exempts_held_names(self):
        import inspect as _i
        import trade_pipeline
        src = _i.getsource(trade_pipeline)
        assert ("symbol in earnings_blocklist and symbol not in "
                "held_symbols") in src, (
            "review #18: the earnings gate must exempt held names like "
            "its recent-exit and learned-HTB siblings — exits matter "
            "MOST in the pre-earnings window")


class TestAIKnowsTheBar:
    """Operator design rule: these mechanisms INFORM the AI so it
    trades best — the gate must be a stated rule of the game, not a
    hidden trap that silently discards sub-bar work."""

    def test_bar_block_states_threshold_exits_and_honesty(self):
        from unittest.mock import MagicMock
        from ai_analyst import _confidence_bar_block
        ctx = MagicMock()
        ctx.ai_confidence_threshold = 60
        block = _confidence_bar_block(ctx)
        assert ">= 60" in block
        assert "exits are never gated" in block
        assert "Rate honestly" in block, (
            "the anti-gaming clause is load-bearing: confidence feeds "
            "the tracker's calibration, the tuner's band logic, and "
            "the meta-model's training data")
        assert "quality bar, not a brake" in block

    def test_bar_block_empty_without_ctx_or_bar(self):
        from unittest.mock import MagicMock
        from ai_analyst import _confidence_bar_block
        assert _confidence_bar_block(None) == ""
        ctx = MagicMock()
        ctx.ai_confidence_threshold = 0
        assert _confidence_bar_block(ctx) == ""

    def test_bar_block_woven_into_the_decision_prompt(self):
        import inspect
        import ai_analyst
        src = inspect.getsource(ai_analyst._build_batch_prompt)
        assert "_confidence_bar_block(ctx)" in src
        assert '{confidence_bar_block}' in src
        # self-contained: renders even when the risk-limits enrichment
        # fails (it sits OUTSIDE that try/except)
        idx = src.find("confidence_bar_block = _confidence_bar_block")
        rl_except = src.find('"risk_limits_block build failed')
        assert idx > rl_except > 0
