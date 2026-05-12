"""Phase 4 of the instrument-class pipeline refactor (2026-05-11).

Phase 4 routes proposals through per-pipeline specialist sets.
Each pipeline owns its applicable specialist list via tagging:
  - pattern_recognizer       → ("stock",)
  - earnings_analyst         → ("stock", "option")
  - sentiment_narrative      → ("stock", "option")
  - risk_assessor            → ("stock", "option")
  - adversarial_reviewer     → ("stock", "option")
  - option_spread_risk       → ("option",)  [NEW]

Closes audit findings #5 (multileg bypasses specialist veto) and
#6 (stock specialists shouldn't see option proposals).

This file pins:
1. CLASS INVARIANT: every specialist module carries a non-empty
   APPLIES_TO_PIPELINES tag — catches future regressions where
   a new specialist is added but its routing tag is forgotten.
2. ROUTING CORRECTNESS: stock pipeline sees pattern_recognizer
   and NOT option_spread_risk; option pipeline sees
   option_spread_risk and NOT pattern_recognizer. Cross-pipeline
   specialists appear in both.
3. ENSEMBLE INTEGRATION: pipeline.route_to_specialists() composes
   `applicable_specialists(self.name)` with
   `ensemble.run_ensemble(specialists_override=...)`. Tests patch
   ensemble to avoid AI calls.
4. VETO PROPAGATION: when ensemble reports a vetoed proposal, the
   pipeline correctly classifies it into SpecialistVerdict.vetoed
   (not .approved).
5. EMPTY-PROPOSAL SHORT-CIRCUIT: zero proposals → no ensemble call.
6. ENSEMBLE BACK-COMPAT: callers without specialists_override get
   the legacy discover_specialists()-filtered list (pre-refactor
   behavior preserved for the un-migrated legacy paths).
"""
from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines import AIResult, SpecialistVerdict
from pipelines.stock import StockPipeline
from pipelines.option import OptionPipeline
from pipelines import specialist_router
from specialists import discover_specialists, SPECIALIST_MODULES


# ---------------------------------------------------------------------------
# Class invariant — every specialist module carries the routing tag
# ---------------------------------------------------------------------------

class TestSpecialistTaggingClassInvariant:
    """Pin the CLASS of tagging behavior, not each instance. If a
    new specialist lands in `SPECIALIST_MODULES` without an
    APPLIES_TO_PIPELINES tag, this catches it — preventing the
    "new specialist silently routes to ALL pipelines" failure mode
    that broke audit finding #6."""

    @pytest.mark.parametrize("module_path", SPECIALIST_MODULES)
    def test_every_specialist_declares_routing_tag(self, module_path):
        mod = importlib.import_module(module_path)
        assert hasattr(mod, "APPLIES_TO_PIPELINES"), (
            f"{module_path} is registered in SPECIALIST_MODULES but "
            f"does NOT declare APPLIES_TO_PIPELINES. Every specialist "
            f"must declare which pipelines it applies to — see "
            f"pipelines/specialist_router.py."
        )
        tag = mod.APPLIES_TO_PIPELINES
        assert isinstance(tag, tuple) and len(tag) >= 1, (
            f"{module_path}.APPLIES_TO_PIPELINES must be a non-empty "
            f"tuple — got {tag!r}"
        )
        # Each entry must be a known pipeline name. Catches typos
        # like ("stocks",) or ("opt",) that would silently route
        # the specialist to nothing.
        known_pipelines = {"stock", "option"}
        for entry in tag:
            assert entry in known_pipelines, (
                f"{module_path}.APPLIES_TO_PIPELINES contains "
                f"unknown pipeline name {entry!r}. Known: "
                f"{sorted(known_pipelines)}"
            )


# ---------------------------------------------------------------------------
# Router — the pure filter function
# ---------------------------------------------------------------------------

class TestRouterFiltersPerPipeline:
    def test_stock_pipeline_includes_pattern_recognizer(self):
        names = specialist_router.applicable_specialist_names("stock")
        assert "pattern_recognizer" in names

    def test_stock_pipeline_excludes_option_spread_risk(self):
        names = specialist_router.applicable_specialist_names("stock")
        assert "option_spread_risk" not in names, (
            "Option-only specialist must NOT fire on stock proposals "
            "(audit finding #6)"
        )

    def test_option_pipeline_includes_option_spread_risk(self):
        names = specialist_router.applicable_specialist_names("option")
        assert "option_spread_risk" in names, (
            "Option pipeline must include its option-specific "
            "specialist (audit finding #5 — multileg bypasses veto)"
        )

    def test_option_pipeline_excludes_pattern_recognizer(self):
        names = specialist_router.applicable_specialist_names("option")
        assert "pattern_recognizer" not in names, (
            "Stock-only chart-pattern specialist must NOT fire on "
            "option proposals — option premiums move on Greeks, "
            "not chart patterns (audit finding #6)"
        )

    @pytest.mark.parametrize("cross_specialist", [
        "earnings_analyst",
        "sentiment_narrative",
        "risk_assessor",
        "adversarial_reviewer",
    ])
    def test_cross_pipeline_specialists_appear_in_both(self, cross_specialist):
        stock_names = specialist_router.applicable_specialist_names("stock")
        option_names = specialist_router.applicable_specialist_names("option")
        assert cross_specialist in stock_names, (
            f"{cross_specialist} must apply to stock pipeline "
            f"(earnings/news/risk are universal)"
        )
        assert cross_specialist in option_names, (
            f"{cross_specialist} must apply to option pipeline "
            f"(earnings/news/risk are universal)"
        )

    def test_unknown_pipeline_returns_empty_list(self):
        """A typo or new instrument class with no specialists yet
        gets an empty list — not a crash."""
        assert specialist_router.applicable_specialists("crypto") == []
        assert specialist_router.applicable_specialists("") == []

    def test_legacy_module_without_tag_defaults_to_stock(self):
        """Back-compat: if a specialist module exists in the registry
        but doesn't declare APPLIES_TO_PIPELINES, it defaults to
        ("stock",) — safe default since the system was stock-only
        before the refactor. Untagged modules must NOT silently fire
        on option proposals."""
        # Build a fake specialist module without the tag.
        fake = SimpleNamespace(
            NAME="legacy_untagged",
            build_prompt=lambda candidates, ctx: "",
            parse_response=lambda raw: [],
        )
        # Test the internal helper directly.
        pipelines = specialist_router._module_pipelines(fake)
        assert pipelines == ("stock",), (
            f"Untagged specialist must default to ('stock',) — got "
            f"{pipelines}"
        )


# ---------------------------------------------------------------------------
# Pipeline.route_to_specialists — composes router + ensemble
# ---------------------------------------------------------------------------

class TestPipelineRoutesThroughEnsemble:
    """Verify the pipeline composes the per-pipeline specialist list
    into `ensemble.run_ensemble(specialists_override=...)`. Patches
    ensemble so no AI calls happen."""

    def test_stock_pipeline_passes_stock_specialists_to_ensemble(self):
        captured = {}

        def fake_ensemble(**kwargs):
            captured["specialists_override"] = kwargs.get("specialists_override")
            return {"per_symbol": {"AAPL": {"vetoed": False}}}

        with patch("ensemble.run_ensemble", side_effect=fake_ensemble):
            ctx = SimpleNamespace(ai_provider="anthropic", ai_model="x",
                                   ai_api_key="y")
            ai_result = AIResult(proposals=[{"symbol": "AAPL",
                                              "verdict": "BUY"}])
            verdict = StockPipeline().route_to_specialists(ctx, ai_result)

        assert "specialists_override" in captured
        passed_names = [s.NAME for s in captured["specialists_override"]]
        assert "pattern_recognizer" in passed_names
        assert "option_spread_risk" not in passed_names
        assert isinstance(verdict, SpecialistVerdict)

    def test_option_pipeline_passes_option_specialists_to_ensemble(self):
        captured = {}

        def fake_ensemble(**kwargs):
            captured["specialists_override"] = kwargs.get("specialists_override")
            return {"per_symbol": {"CWAN": {"vetoed": False}}}

        with patch("ensemble.run_ensemble", side_effect=fake_ensemble):
            ctx = SimpleNamespace(ai_provider="anthropic", ai_model="x",
                                   ai_api_key="y")
            ai_result = AIResult(proposals=[{"symbol": "CWAN",
                                              "signal": "MULTILEG_OPEN"}])
            verdict = OptionPipeline().route_to_specialists(ctx, ai_result)

        passed_names = [s.NAME for s in captured["specialists_override"]]
        assert "option_spread_risk" in passed_names
        assert "pattern_recognizer" not in passed_names
        assert isinstance(verdict, SpecialistVerdict)


class TestVetoFlowsToVerdict:
    """When ensemble reports a vetoed symbol, the pipeline must
    classify the proposal into `vetoed`, not `approved`. Critical
    for audit finding #5 (multileg today flows past veto signals)."""

    def test_vetoed_proposal_lands_in_vetoed_list(self):
        def fake_ensemble(**kwargs):
            return {"per_symbol": {
                "TSLA": {"vetoed": True,
                          "veto_reason": "illiquid options chain"},
                "AAPL": {"vetoed": False},
            }}

        with patch("ensemble.run_ensemble", side_effect=fake_ensemble):
            ctx = SimpleNamespace()
            ai_result = AIResult(proposals=[
                {"symbol": "TSLA", "signal": "MULTILEG_OPEN"},
                {"symbol": "AAPL", "signal": "MULTILEG_OPEN"},
            ])
            verdict = OptionPipeline().route_to_specialists(ctx, ai_result)

        approved_syms = [p["symbol"] for p in verdict.approved]
        vetoed_syms = [p["symbol"] for p in verdict.vetoed]
        assert "TSLA" in vetoed_syms
        assert "TSLA" not in approved_syms
        assert "AAPL" in approved_syms
        assert "AAPL" not in vetoed_syms
        # The veto reason flows through to the log
        assert any("illiquid" in entry for entry in verdict.veto_log)


class TestEmptyProposalsShortCircuit:
    """Zero proposals → no ensemble call (no AI cost spent on nothing)."""

    def test_empty_proposals_skip_ensemble(self):
        called = {"count": 0}

        def fake_ensemble(**kwargs):
            called["count"] += 1
            return {"per_symbol": {}}

        with patch("ensemble.run_ensemble", side_effect=fake_ensemble):
            ctx = SimpleNamespace()
            verdict = OptionPipeline().route_to_specialists(
                ctx, AIResult(proposals=[]),
            )

        assert called["count"] == 0, (
            "Pipeline must short-circuit on empty proposals — no AI "
            "call wasted on nothing"
        )
        assert verdict.approved == []
        assert verdict.vetoed == []


# ---------------------------------------------------------------------------
# Ensemble back-compat — un-migrated callers still work
# ---------------------------------------------------------------------------

class TestEnsembleSpecialistsOverrideBackCompat:
    """The new `specialists_override` parameter is opt-in. Callers
    that don't pass it (the legacy `ai_analyst` path, the existing
    multi_scheduler call sites) still get the discover_specialists()
    list — no surprise behavior change for un-migrated code paths."""

    def test_run_ensemble_signature_accepts_specialists_override(self):
        import inspect
        from ensemble import run_ensemble
        sig = inspect.signature(run_ensemble)
        assert "specialists_override" in sig.parameters
        param = sig.parameters["specialists_override"]
        assert param.default is None, (
            "specialists_override must default to None so existing "
            "callers get pre-refactor behavior"
        )


# ---------------------------------------------------------------------------
# Option spread risk specialist — minimal contract pinning
# ---------------------------------------------------------------------------

class TestOptionSpreadRiskSpecialistContract:
    """Pin that the new option-only specialist conforms to the
    specialist module contract (NAME, DESCRIPTION, build_prompt,
    parse_response, HAS_VETO_AUTHORITY, APPLIES_TO_PIPELINES)."""

    def test_specialist_is_discoverable(self):
        names = [s.NAME for s in discover_specialists()]
        assert "option_spread_risk" in names

    def test_specialist_has_veto_authority(self):
        from specialists import option_spread_risk
        assert option_spread_risk.HAS_VETO_AUTHORITY is True, (
            "Option-specific risks (max-loss-exceeds-budget, IV "
            "crush, gamma blowup) are structural — no other "
            "specialist can catch them. Must hold veto authority."
        )

    def test_specialist_tagged_option_only(self):
        from specialists import option_spread_risk
        assert option_spread_risk.APPLIES_TO_PIPELINES == ("option",)

    def test_specialist_build_prompt_mentions_option_concepts(self):
        from specialists import option_spread_risk
        candidates = [{"symbol": "CWAN", "iv_rank": 78, "dte": 32,
                        "spread_max_loss": 230}]
        prompt = option_spread_risk.build_prompt(candidates,
                                                  SimpleNamespace())
        # The prompt should mention the four risk classes by name
        lower = prompt.lower()
        for term in ("max-loss", "iv crush", "gamma", "credit"):
            assert term in lower, (
                f"option_spread_risk prompt missing {term!r} — "
                f"specialist should explicitly check this risk class"
            )

    def test_specialist_in_veto_authorized_or_extensible(self):
        """For Phase 4a, option_spread_risk is in the registry but
        not yet in `ensemble.VETO_AUTHORIZED`. Phase 4b will add it
        when the live cycles wire through. This test pins the
        eventual contract: HAS_VETO_AUTHORITY = True means the
        specialist's VETO verdict will be honored once ensemble is
        wired."""
        from specialists import option_spread_risk
        assert option_spread_risk.HAS_VETO_AUTHORITY is True
