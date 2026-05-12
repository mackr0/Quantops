"""Phase 0 of the instrument-class pipeline refactor (2026-05-11).

Pins the contract for the new `Pipeline` ABC and the concrete
StockPipeline / OptionPipeline shells. Phase 0 introduces the
abstraction WITHOUT moving any business logic — these tests
verify the abstraction is in place and consumable.

See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md` for the full
architectural plan and exit criteria for each phase.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines import (Pipeline, Candidate, AIResult, SpecialistVerdict,
                        ExecutionResult, Outcome, Metrics,
                        ParameterAdjustments)
from pipelines.stock import StockPipeline
from pipelines.option import OptionPipeline
from pipelines.registry import (ALL_PIPELINES, get_pipelines_for_profile)


# ---------------------------------------------------------------------------
# Class-level conformance: every concrete pipeline implements every
# abstract method
# ---------------------------------------------------------------------------

class TestPipelineABCConformance:
    """Each concrete pipeline class must satisfy the ABC contract.
    Adding a new pipeline (CryptoPipeline, FXPipeline, etc.) must
    pass these without modification."""

    @pytest.mark.parametrize("cls", [StockPipeline, OptionPipeline])
    def test_concrete_pipelines_are_instantiable(self, cls):
        """If any `@abstractmethod` is missing, instantiation raises."""
        instance = cls()
        assert isinstance(instance, Pipeline)

    @pytest.mark.parametrize("cls", [StockPipeline, OptionPipeline])
    def test_each_pipeline_has_a_name(self, cls):
        """The `name` attribute is the pipeline's identity in logs,
        the registry, and per-pipeline metric storage."""
        assert cls.name and isinstance(cls.name, str)

    def test_pipeline_names_are_unique(self):
        """No two registered pipelines share a name (they'd collide
        in metric storage and log output)."""
        names = [p.name for p in ALL_PIPELINES]
        assert len(names) == len(set(names)), (
            f"Duplicate pipeline names in registry: {names}"
        )


# ---------------------------------------------------------------------------
# applies_to — the only method Phase 0 implements concretely
# ---------------------------------------------------------------------------

class TestAppliesTo:
    def test_stock_applies_by_default(self):
        ctx = SimpleNamespace()
        assert StockPipeline().applies_to(ctx) is True

    def test_option_applies_by_default(self):
        ctx = SimpleNamespace()
        assert OptionPipeline().applies_to(ctx) is True

    def test_stock_can_be_disabled_per_profile(self):
        """Future use: a crypto-only profile sets disable_stock=True."""
        ctx = SimpleNamespace(disable_stock=True)
        assert StockPipeline().applies_to(ctx) is False

    def test_option_can_be_disabled_per_profile(self):
        """Future use: a stock-only profile opts out of options."""
        ctx = SimpleNamespace(disable_options=True)
        assert OptionPipeline().applies_to(ctx) is False


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_default_profile_gets_both_pipelines(self):
        """Every profile today trades both stocks and options."""
        ctx = SimpleNamespace()
        pipelines = get_pipelines_for_profile(ctx)
        names = sorted(p.name for p in pipelines)
        assert names == ["option", "stock"]

    def test_stock_disabled_excludes_stock_pipeline(self):
        ctx = SimpleNamespace(disable_stock=True)
        pipelines = get_pipelines_for_profile(ctx)
        names = [p.name for p in pipelines]
        assert "stock" not in names
        assert "option" in names

    def test_option_disabled_excludes_option_pipeline(self):
        ctx = SimpleNamespace(disable_options=True)
        pipelines = get_pipelines_for_profile(ctx)
        names = [p.name for p in pipelines]
        assert "stock" in names
        assert "option" not in names

    def test_all_pipelines_disabled_returns_empty(self):
        ctx = SimpleNamespace(disable_stock=True, disable_options=True)
        assert get_pipelines_for_profile(ctx) == []


# ---------------------------------------------------------------------------
# Phase 0 NotImplementedError contract — every method that hasn't
# been migrated yet raises clearly with a "which phase wires this"
# message. Catches accidental wiring before the right phase lands.
# ---------------------------------------------------------------------------

class TestPhase0PlaceholdersRaiseClearly:
    @pytest.mark.parametrize("cls", [StockPipeline, OptionPipeline])
    @pytest.mark.parametrize("method,args", [
        ("generate_candidates", (None,)),
        # build_prompt now wired in Phase 3.
        ("decide", (None, "")),
        ("route_to_specialists", (None, AIResult(proposals=[]))),
        ("execute", (None, SpecialistVerdict())),
        ("record_outcome", (None, 1, Outcome(
            prediction_id=1, actual_outcome="win",
            actual_return_pct=0.0, resolved_at="", resolution_price=0.0,
        ))),
        # compute_metrics now wired in Phase 1.
        # tune now wired in Phase 2.
        # Both have their own test files pinning behavior.
    ])
    def test_unimplemented_methods_raise_with_phase_pointer(
        self, cls, method, args,
    ):
        instance = cls()
        with pytest.raises(NotImplementedError) as exc_info:
            getattr(instance, method)(*args)
        msg = str(exc_info.value)
        # Each placeholder names the phase that lands the real
        # implementation — makes "unwired pipeline call" debuggable.
        assert "Phase" in msg, (
            f"{cls.__name__}.{method}() raises NotImplementedError but "
            f"doesn't say which Phase will land it: {msg}"
        )


# ---------------------------------------------------------------------------
# DTOs are constructible with sensible defaults — consumers
# shouldn't have to pass every field
# ---------------------------------------------------------------------------

class TestDTODefaults:
    def test_candidate_minimal(self):
        c = Candidate(symbol="AAPL", score=0.8, signal="BUY",
                       price=150.0)
        assert c.symbol == "AAPL"
        assert c.extra == {}

    def test_ai_result_empty(self):
        r = AIResult(proposals=[])
        assert r.proposals == []
        assert r.reasoning == ""
        assert r.confidence_avg is None

    def test_specialist_verdict_empty(self):
        v = SpecialistVerdict()
        assert v.approved == []
        assert v.vetoed == []
        assert v.veto_log == []

    def test_execution_result_empty(self):
        e = ExecutionResult()
        assert e.submitted == []
        assert e.rejected == []
        assert e.skipped == []
        assert e.errors == []

    def test_metrics_minimal(self):
        m = Metrics(pipeline_name="stock")
        assert m.pipeline_name == "stock"
        assert m.numbers == {}

    def test_parameter_adjustments_empty(self):
        a = ParameterAdjustments(pipeline_name="stock")
        assert a.changes == {}


# ---------------------------------------------------------------------------
# run_cycle composition — verify the lifecycle stitches together
# even though Phase 0 methods raise. The dispatcher uses this; if a
# pipeline doesn't apply, run_cycle short-circuits cleanly without
# raising.
# ---------------------------------------------------------------------------

class TestRunCycleComposition:
    def test_run_cycle_short_circuits_when_pipeline_does_not_apply(self):
        """If applies_to() is False, the rest of the lifecycle is
        skipped — no NotImplementedError should bubble up. This is
        the behavior the scheduler dispatcher relies on."""
        ctx = SimpleNamespace(disable_stock=True)
        result = StockPipeline().run_cycle(ctx)
        assert isinstance(result, ExecutionResult)
        assert result.submitted == []

    def test_run_cycle_short_circuits_with_no_candidates(self):
        """If generate_candidates() returns [] (no candidates this
        cycle), the lifecycle short-circuits at that step — useful
        once Phase 1 wires generate_candidates and the rest are
        still placeholders. Stub it to return [] for this test."""
        class EmptyStockPipeline(StockPipeline):
            def generate_candidates(self, ctx):
                return []
        ctx = SimpleNamespace()
        result = EmptyStockPipeline().run_cycle(ctx)
        assert result.submitted == []
