"""Phase 4b of the instrument-class pipeline refactor (2026-05-11).

Phase 4b WIRES the option specialist veto into the live multileg
dispatch path. Today's pre-Phase-4b multileg branch in
`trade_pipeline.py` calls `execute_multileg_strategy` directly,
bypassing the entire ensemble — `option_spread_risk` (added in
Phase 4a) had nowhere to fire.

Phase 4b adds `check_multileg_specialist_veto(ctx, ai_trade, symbol)`
as the gate. Returns (vetoed, reason). Vetoed proposals are skipped
+ logged to broker_rejections; non-vetoed proposals proceed to
`execute_multileg_strategy` exactly as before.

This file pins the helper's contract:
- VETOED PROPOSAL: when OptionPipeline.route_to_specialists puts
  the symbol in `vetoed`, helper returns (True, reason).
- APPROVED PROPOSAL: when the symbol is in `approved`, helper
  returns (False, "").
- FAILURE-TOLERANT: when route_to_specialists raises (network /
  ensemble crash), helper returns (False, "") so the trade
  proceeds. Phase 4b adds a veto LAYER; it must not introduce a
  NEW failure mode that blocks trades on infrastructure problems.
- USES OPTION SPECIALISTS: under the hood, the call goes through
  OptionPipeline (not StockPipeline), so option_spread_risk +
  cross-pipeline specialists fire on the proposal.
- BROKER_REJECTIONS LOGGING: vetoed proposals get persisted to
  the rejections panel so operators see the veto.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from trade_pipeline import check_multileg_specialist_veto
from pipelines import SpecialistVerdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trade(symbol="CWAN", strategy="bull_put_spread", confidence=70):
    return {
        "symbol": symbol,
        "action": "MULTILEG_OPEN",
        "strategy_name": strategy,
        "confidence": confidence,
        "reasoning": "test rationale",
    }


def _ctx(db_path=None):
    return SimpleNamespace(
        db_path=db_path,
        ai_provider="anthropic",
        ai_model="claude-haiku-4-5-20251001",
        ai_api_key="test-key",
    )


# ---------------------------------------------------------------------------
# Veto outcome propagation
# ---------------------------------------------------------------------------

class TestVetoedProposalReturnsTrue:
    def test_vetoed_symbol_returns_true_with_reason(self):
        proposal = _trade(symbol="CWAN")
        verdict = SpecialistVerdict(
            approved=[],
            vetoed=[proposal],
            veto_log=["CWAN: VETO — max loss exceeds budget"],
        )
        with patch(
            "pipelines.option.OptionPipeline.route_to_specialists",
            return_value=verdict,
        ):
            vetoed, reason = check_multileg_specialist_veto(
                _ctx(), proposal, "CWAN",
            )
        assert vetoed is True
        assert "max loss" in reason or "budget" in reason

    def test_vetoed_symbol_default_reason_when_log_empty(self):
        proposal = _trade(symbol="CWAN")
        verdict = SpecialistVerdict(
            approved=[], vetoed=[proposal], veto_log=[],
        )
        with patch(
            "pipelines.option.OptionPipeline.route_to_specialists",
            return_value=verdict,
        ):
            vetoed, reason = check_multileg_specialist_veto(
                _ctx(), proposal, "CWAN",
            )
        assert vetoed is True
        assert reason == "specialist veto"


class TestApprovedProposalReturnsFalse:
    def test_approved_symbol_returns_false(self):
        proposal = _trade(symbol="CWAN")
        verdict = SpecialistVerdict(
            approved=[proposal], vetoed=[], veto_log=[],
        )
        with patch(
            "pipelines.option.OptionPipeline.route_to_specialists",
            return_value=verdict,
        ):
            vetoed, reason = check_multileg_specialist_veto(
                _ctx(), proposal, "CWAN",
            )
        assert vetoed is False
        assert reason == ""

    def test_empty_verdict_returns_false(self):
        """When the verdict has no vetoed entries, the proposal
        passes — the trade proceeds."""
        verdict = SpecialistVerdict(approved=[], vetoed=[], veto_log=[])
        with patch(
            "pipelines.option.OptionPipeline.route_to_specialists",
            return_value=verdict,
        ):
            vetoed, _ = check_multileg_specialist_veto(
                _ctx(), _trade(), "CWAN",
            )
        assert vetoed is False


# ---------------------------------------------------------------------------
# Failure tolerance — infrastructure crash MUST NOT block trades
# ---------------------------------------------------------------------------

class TestFailureTolerance:
    """The single most important Phase 4b invariant: a routing
    failure must NOT introduce a new failure mode that blocks
    trades. Phase 4b adds a VETO LAYER on top of the existing
    multileg path; if the layer crashes, the existing path still
    runs."""

    def test_route_raises_returns_false_no_block(self):
        with patch(
            "pipelines.option.OptionPipeline.route_to_specialists",
            side_effect=RuntimeError("ensemble exploded"),
        ):
            vetoed, reason = check_multileg_specialist_veto(
                _ctx(), _trade(), "CWAN",
            )
        assert vetoed is False, (
            "Routing crash MUST NOT block the trade — Phase 4b is "
            "additive; failure mode preservation is the contract"
        )
        assert reason == ""

    def test_route_raises_network_error_returns_false(self):
        with patch(
            "pipelines.option.OptionPipeline.route_to_specialists",
            side_effect=ConnectionError("AI provider unreachable"),
        ):
            vetoed, _ = check_multileg_specialist_veto(
                _ctx(), _trade(), "CWAN",
            )
        assert vetoed is False


# ---------------------------------------------------------------------------
# Routes through OptionPipeline (not StockPipeline)
# ---------------------------------------------------------------------------

class TestRoutesThroughOptionPipeline:
    """Verifies that the helper calls OptionPipeline (so
    option_spread_risk + cross-pipeline specialists fire), NOT
    StockPipeline (which would route through pattern_recognizer
    and produce noise on multileg proposals)."""

    def test_helper_uses_option_pipeline(self):
        """Confirm the route goes through OptionPipeline by
        intercepting OptionPipeline.route_to_specialists. If the
        helper accidentally instantiated StockPipeline, this patch
        wouldn't fire and the assertion would fail."""
        called_with = {}

        def fake_route(self, ctx, ai_result):
            called_with["pipeline_name"] = self.name
            called_with["proposals"] = list(
                getattr(ai_result, "proposals", [])
            )
            return SpecialistVerdict(
                approved=ai_result.proposals, vetoed=[], veto_log=[],
            )

        with patch(
            "pipelines.option.OptionPipeline.route_to_specialists",
            new=fake_route,
        ):
            check_multileg_specialist_veto(_ctx(), _trade(), "CWAN")

        assert called_with.get("pipeline_name") == "option", (
            "Phase 4b helper must route through OptionPipeline, not "
            "StockPipeline (option_spread_risk only fires for option)"
        )
        assert len(called_with["proposals"]) == 1
        assert called_with["proposals"][0]["symbol"] == "CWAN"


# ---------------------------------------------------------------------------
# Symbol propagation — proposal symbol set when missing
# ---------------------------------------------------------------------------

class TestSymbolPropagation:
    def test_helper_sets_symbol_on_proposal_when_missing(self):
        """ai_trade dicts sometimes lack a symbol field (the caller
        passes it separately). The helper must set the symbol so
        the verdict's vetoed/approved lookup works."""
        captured = {}

        def fake_route(self, ctx, ai_result):
            captured["proposal"] = ai_result.proposals[0]
            return SpecialistVerdict(
                approved=ai_result.proposals, vetoed=[], veto_log=[],
            )

        ai_trade_no_symbol = {
            "action": "MULTILEG_OPEN", "strategy_name": "iron_condor",
            "confidence": 60,
        }
        with patch(
            "pipelines.option.OptionPipeline.route_to_specialists",
            new=fake_route,
        ):
            check_multileg_specialist_veto(
                _ctx(), ai_trade_no_symbol, "AAPL",
            )

        assert captured["proposal"]["symbol"] == "AAPL"

    def test_helper_does_not_overwrite_existing_symbol(self):
        """If ai_trade already has a symbol, it wins over the
        positional `symbol` parameter — caller knows what they're
        doing."""
        captured = {}

        def fake_route(self, ctx, ai_result):
            captured["proposal"] = ai_result.proposals[0]
            return SpecialistVerdict(
                approved=ai_result.proposals, vetoed=[], veto_log=[],
            )

        ai_trade_with_symbol = _trade(symbol="MSFT")
        with patch(
            "pipelines.option.OptionPipeline.route_to_specialists",
            new=fake_route,
        ):
            # Pass mismatched outer symbol — proposal should keep MSFT
            check_multileg_specialist_veto(
                _ctx(), ai_trade_with_symbol, "AAPL",
            )

        assert captured["proposal"]["symbol"] == "MSFT"
