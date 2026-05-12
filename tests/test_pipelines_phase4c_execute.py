"""Phase 4c — multileg full pipeline migration (2026-05-12).

OptionPipeline.execute() now owns the multileg + single-leg
broker submission paths. The legacy 80-line elif branch in
trade_pipeline.run_trade_cycle is now a thin caller that builds
a one-element SpecialistVerdict and delegates here.

This file pins:
- VETOED PROPOSAL → SKIPPED list with SPECIALIST_VETOED action;
  broker_rejection persisted; reason carries through.
- APPROVED MULTILEG → submitted via execute_multileg_strategy;
  link_option_prediction_to_trade fires with combo_order_id;
  result lands in submitted list.
- APPROVED SINGLE-LEG → submitted via execute_option_strategy;
  link fires with occ_symbol.
- BAD STRATEGY NAME → ERROR result in errors list.
- BAD EXPIRY → ERROR result.
- ROUTING FAILURE TOLERANCE: when execute_multileg_strategy
  raises, we land in errors with a meaningful message; the
  caller's loop continues.
- BACK-COMPAT: trade_result dict shape produced by Phase 4c
  delegation matches what the legacy elif branch produced
  (same keys, same conventions).
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines import SpecialistVerdict
from pipelines.option import OptionPipeline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ctx():
    return SimpleNamespace(
        db_path=None,   # tests with linkage will set this explicitly
        ai_provider="anthropic",
        ai_model="x", ai_api_key="y",
    )


def _multileg_proposal(symbol="CWAN", strategy="bull_put_spread"):
    today = date.today()
    expiry = today + timedelta(days=32)
    return {
        "symbol": symbol,
        "action": "MULTILEG_OPEN",
        "strategy_name": strategy,
        "strikes": {"long": 45, "short": 50},
        "expiry": expiry.strftime("%Y-%m-%d"),
        "contracts": 1,
        "confidence": 70,
        "reasoning": "test rationale",
    }


def _single_leg_proposal(symbol="MSFT"):
    return {
        "symbol": symbol,
        "action": "OPTIONS",
        "option_strategy": "long_call",
        "strike": 400,
        "expiry": "2026-06-12",
        "contracts": 1,
        "confidence": 65,
        "reasoning": "test rationale",
    }


# ---------------------------------------------------------------------------
# VETOED proposals → SKIPPED + persisted
# ---------------------------------------------------------------------------

class TestVetoedProposalsLandInSkipped:
    def test_vetoed_multileg_persisted_and_skipped(self):
        proposal = _multileg_proposal(symbol="CWAN")
        verdict = SpecialistVerdict(
            approved=[], vetoed=[proposal],
            veto_log=["CWAN: VETO — max loss exceeds budget"],
        )
        recorded = []

        def fake_record(db_path, **kwargs):
            recorded.append(kwargs)

        ctx = SimpleNamespace(db_path="x.db")
        with patch("journal.record_broker_rejection",
                    side_effect=fake_record):
            result = OptionPipeline().execute(ctx, verdict)

        # SPECIALIST_VETOED entry in skipped
        assert len(result.skipped) == 1
        assert result.skipped[0]["action"] == "SPECIALIST_VETOED"
        assert result.skipped[0]["symbol"] == "CWAN"
        assert "max loss" in result.skipped[0]["reason"]
        # Persisted to broker_rejections
        assert len(recorded) == 1
        assert recorded[0]["symbol"] == "CWAN"
        assert recorded[0]["signal_type"] == "MULTILEG_OPEN"
        assert "specialist veto" in recorded[0]["broker_message"]
        assert "max loss" in recorded[0]["broker_message"]
        # No execution attempts
        assert result.submitted == []

    def test_veto_persistence_failure_non_fatal(self):
        proposal = _multileg_proposal()
        verdict = SpecialistVerdict(
            approved=[], vetoed=[proposal],
            veto_log=["CWAN: VETO — bad"],
        )
        ctx = SimpleNamespace(db_path="x.db")
        # record_broker_rejection raises — execute should still
        # produce the SPECIALIST_VETOED skip entry
        with patch("journal.record_broker_rejection",
                    side_effect=RuntimeError("DB locked")):
            result = OptionPipeline().execute(ctx, verdict)
        assert len(result.skipped) == 1
        assert result.skipped[0]["action"] == "SPECIALIST_VETOED"


# ---------------------------------------------------------------------------
# APPROVED MULTILEG → submitted via execute_multileg_strategy
# ---------------------------------------------------------------------------

class TestApprovedMultilegExecution:
    def test_approved_multileg_calls_executor_and_links(self):
        proposal = _multileg_proposal(symbol="CWAN")
        verdict = SpecialistVerdict(approved=[proposal], vetoed=[])
        ctx = SimpleNamespace(db_path="x.db")

        exec_calls = []
        link_calls = []

        def fake_exec(api, spec, **kwargs):
            exec_calls.append({"strategy": spec.name})
            return {
                "action": "MULTILEG_OPEN",
                "combo_order_id": "combo-123",
                "leg_order_ids": ["combo-123"],
            }

        def fake_link(db_path, **kwargs):
            link_calls.append(kwargs)
            return True

        with patch("client.get_api", return_value=MagicMock()), \
             patch("options_multileg.execute_multileg_strategy",
                    side_effect=fake_exec), \
             patch("journal.link_option_prediction_to_trade",
                    side_effect=fake_link):
            result = OptionPipeline().execute(ctx, verdict)

        assert len(exec_calls) == 1
        assert exec_calls[0]["strategy"] == "bull_put_spread"
        # Linkage called with the combo_order_id
        assert len(link_calls) == 1
        assert link_calls[0]["option_order_id"] == "combo-123"
        assert link_calls[0]["signal"] == "MULTILEG_OPEN"
        # Result in submitted
        assert len(result.submitted) == 1
        assert result.submitted[0]["combo_order_id"] == "combo-123"

    def test_unknown_strategy_lands_in_errors(self):
        proposal = _multileg_proposal(strategy="not_a_real_strategy")
        verdict = SpecialistVerdict(approved=[proposal], vetoed=[])
        with patch("client.get_api", return_value=MagicMock()):
            result = OptionPipeline().execute(_ctx(), verdict)
        assert len(result.errors) == 1
        assert "Unknown strategy" in result.errors[0]["reason"]
        assert result.submitted == []

    def test_bad_expiry_lands_in_errors(self):
        proposal = _multileg_proposal()
        proposal["expiry"] = "not-a-date"
        verdict = SpecialistVerdict(approved=[proposal], vetoed=[])
        with patch("client.get_api", return_value=MagicMock()):
            result = OptionPipeline().execute(_ctx(), verdict)
        assert len(result.errors) == 1
        assert "Invalid expiry" in result.errors[0]["reason"]

    def test_executor_exception_lands_in_errors(self):
        proposal = _multileg_proposal()
        verdict = SpecialistVerdict(approved=[proposal], vetoed=[])
        with patch("client.get_api", return_value=MagicMock()), \
             patch("options_multileg.execute_multileg_strategy",
                    side_effect=RuntimeError("broker down")):
            result = OptionPipeline().execute(_ctx(), verdict)
        assert len(result.errors) == 1
        assert "build/submit failed" in result.errors[0]["reason"]


# ---------------------------------------------------------------------------
# APPROVED SINGLE-LEG → submitted via execute_option_strategy
# ---------------------------------------------------------------------------

class TestApprovedSingleLegExecution:
    def test_approved_single_leg_calls_executor_and_links(self):
        proposal = _single_leg_proposal()
        verdict = SpecialistVerdict(approved=[proposal], vetoed=[])
        ctx = SimpleNamespace(db_path="x.db")

        link_calls = []
        def fake_link(db_path, **kwargs):
            link_calls.append(kwargs)

        with patch("client.get_api", return_value=MagicMock()), \
             patch("options_trader.execute_option_strategy",
                    return_value={
                        "action": "OPTIONS",
                        "occ_symbol": "MSFT  260612C00400000",
                    }), \
             patch("journal.link_option_prediction_to_trade",
                    side_effect=fake_link):
            result = OptionPipeline().execute(ctx, verdict)

        assert len(result.submitted) == 1
        assert result.submitted[0]["occ_symbol"] == "MSFT  260612C00400000"
        # Linkage with occ_symbol
        assert len(link_calls) == 1
        assert link_calls[0]["occ_symbol"] == "MSFT  260612C00400000"
        assert link_calls[0]["signal"] == "OPTIONS"


# ---------------------------------------------------------------------------
# BACK-COMPAT — trade_result dict shape matches legacy
# ---------------------------------------------------------------------------

class TestBackCompatTradeResultShape:
    """The legacy elif branch produced a single trade_result dict
    per ai_trade with these keys: action, symbol, reason (on
    error), combo_order_id (on success), etc. Phase 4c's
    delegation must produce a dict compatible with the same
    consumer code (trade_pipeline.py:2148-2153 warning-on-action,
    details.append, etc.)."""

    def test_vetoed_result_has_action_symbol_reason(self):
        proposal = _multileg_proposal(symbol="CWAN")
        verdict = SpecialistVerdict(
            approved=[], vetoed=[proposal],
            veto_log=["CWAN: VETO — bad"],
        )
        with patch("journal.record_broker_rejection"):
            result = OptionPipeline().execute(SimpleNamespace(),
                                                 verdict)
        # First (and only) result entry has the expected keys
        entry = result.skipped[0]
        assert "action" in entry
        assert "symbol" in entry
        assert "reason" in entry
        assert entry["action"] == "SPECIALIST_VETOED"

    def test_executed_result_has_action_symbol(self):
        proposal = _multileg_proposal()
        verdict = SpecialistVerdict(approved=[proposal], vetoed=[])
        with patch("client.get_api", return_value=MagicMock()), \
             patch("options_multileg.execute_multileg_strategy",
                    return_value={
                        "action": "MULTILEG_OPEN",
                        "combo_order_id": "x",
                    }), \
             patch("journal.link_option_prediction_to_trade"):
            result = OptionPipeline().execute(_ctx(), verdict)
        entry = result.submitted[0]
        assert "action" in entry
        assert "symbol" in entry
        assert entry["symbol"] == "CWAN"


# ---------------------------------------------------------------------------
# Empty verdict
# ---------------------------------------------------------------------------

class TestEmptyVerdict:
    def test_empty_verdict_returns_empty_result(self):
        verdict = SpecialistVerdict(approved=[], vetoed=[])
        result = OptionPipeline().execute(_ctx(), verdict)
        assert result.submitted == []
        assert result.skipped == []
        assert result.errors == []
