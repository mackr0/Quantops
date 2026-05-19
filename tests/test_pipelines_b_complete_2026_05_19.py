"""Pipeline scope-B: every abstract method implemented end-to-end.

2026-05-19. Filled the remaining stubs that the prior phase migrations
skipped:

- `OptionPipeline.generate_candidates` (Phase 1 was supposed to;
  did metrics instead and left this as a stub with stale docstring)
- `OptionPipeline.decide` (Phase 3 was supposed to; did prompts
  instead and left this as a stub)
- `StockPipeline.generate_candidates` (same Phase 1 miss as option)
- `StockPipeline.decide` (same Phase 3 miss as option)
- `StockPipeline.execute` (Phase 4 did multileg only; stock-side
  was deferred to "future cleanup" and never landed)

After this commit, both pipelines are runnable end-to-end via
`.run_cycle(ctx)`. Production still uses the legacy
`trade_pipeline.run_trade_cycle` dispatch — the scheduler cutover
(scope C) is a separate change requiring shadow-mode soak.

Tests pin:
  - generate_candidates() returns the right shape from a shortlist
    (option side requires IV rank via oracle; stock side carries
    technicals through `extra`)
  - decide() calls call_ai with the right provider/key from ctx and
    filters returned proposals to the right action types
  - execute() classifies trader/options_trader results into the
    submitted/rejected/skipped/errors buckets correctly
  - run_cycle() composes all five methods end-to-end without errors
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pipelines import (
    AIResult, Candidate, ExecutionResult, SpecialistVerdict, Metrics,
)
from pipelines.stock import StockPipeline
from pipelines.option import OptionPipeline


# ---------------------------------------------------------------------------
# StockPipeline.generate_candidates
# ---------------------------------------------------------------------------

class TestStockGenerateCandidates:
    def test_empty_shortlist_returns_empty_list(self):
        ctx = SimpleNamespace(shortlist=[])
        assert StockPipeline().generate_candidates(ctx) == []

    def test_no_shortlist_attr_returns_empty(self):
        ctx = SimpleNamespace()
        assert StockPipeline().generate_candidates(ctx) == []

    def test_actionable_signals_become_candidates(self):
        ctx = SimpleNamespace(shortlist=[
            {"symbol": "AAPL", "signal": "BUY", "price": 150.0,
             "score": 0.8, "rsi": 65, "adx": 30},
            {"symbol": "TSLA", "signal": "STRONG_BUY", "price": 200.0,
             "score": 0.9, "rsi": 75},
        ])
        candidates = StockPipeline().generate_candidates(ctx)
        assert len(candidates) == 2
        # Sorted by score descending
        assert candidates[0].symbol == "TSLA"
        assert candidates[1].symbol == "AAPL"
        # Technicals carried in extra
        assert candidates[0].extra.get("rsi") == 75

    def test_option_actions_skipped(self):
        """MULTILEG_OPEN / OPTIONS rows belong to OptionPipeline, not stock."""
        ctx = SimpleNamespace(shortlist=[
            {"symbol": "AAPL", "signal": "BUY", "price": 150.0, "score": 0.5},
            {"symbol": "SPY", "signal": "MULTILEG_OPEN", "price": 500.0,
             "score": 0.9},
        ])
        candidates = StockPipeline().generate_candidates(ctx)
        syms = [c.symbol for c in candidates]
        assert "AAPL" in syms
        assert "SPY" not in syms, (
            "MULTILEG_OPEN rows must not be stock candidates — they "
            "belong to OptionPipeline"
        )

    def test_invalid_rows_skipped(self):
        ctx = SimpleNamespace(shortlist=[
            {"symbol": "AAPL", "signal": "BUY", "price": 150.0},  # OK
            {"signal": "BUY", "price": 150.0},  # no symbol
            {"symbol": "TSLA", "signal": "BUY", "price": 0},  # zero price
            {"symbol": "GOOG", "signal": "BUY", "price": "not-a-number"},
        ])
        candidates = StockPipeline().generate_candidates(ctx)
        assert [c.symbol for c in candidates] == ["AAPL"]

    def test_top_n_cap_respected(self):
        rows = [
            {"symbol": f"S{i}", "signal": "BUY", "price": 100.0,
             "score": float(i)}
            for i in range(50)
        ]
        ctx = SimpleNamespace(shortlist=rows, stock_candidate_top_n=5)
        candidates = StockPipeline().generate_candidates(ctx)
        assert len(candidates) == 5
        # Top 5 by score: 49, 48, 47, 46, 45
        assert [c.symbol for c in candidates] == [
            "S49", "S48", "S47", "S46", "S45",
        ]


# ---------------------------------------------------------------------------
# StockPipeline.decide
# ---------------------------------------------------------------------------

class TestStockDecide:
    def test_calls_call_ai_with_ctx_provider_and_key(self):
        ctx = SimpleNamespace(
            ai_provider="google", ai_model="gemini-2.5-flash-lite",
            ai_api_key="g-key", db_path=None,
        )
        with patch("ai_providers.call_ai") as mock_call_ai:
            mock_call_ai.return_value = (
                '{"trades": [{"action": "BUY", "symbol": "AAPL", "confidence": 80}]}'
            )
            result = StockPipeline().decide(ctx, "test prompt")
        kwargs = mock_call_ai.call_args.kwargs
        assert kwargs["provider"] == "google"
        assert kwargs["api_key"] == "g-key"
        assert kwargs["model"] == "gemini-2.5-flash-lite"
        assert result.proposals[0]["symbol"] == "AAPL"

    def test_filters_to_stock_actions_only(self):
        """If the AI returns mixed stock + option trades, decide() on
        StockPipeline must return only the stock-side ones."""
        ctx = SimpleNamespace(
            ai_provider="google", ai_model="m", ai_api_key="k",
        )
        with patch("ai_providers.call_ai") as mock_call_ai:
            mock_call_ai.return_value = (
                '{"trades": ['
                '{"action": "BUY", "symbol": "AAPL"},'
                '{"action": "MULTILEG_OPEN", "symbol": "SPY"},'
                '{"action": "SELL", "symbol": "TSLA"}'
                ']}'
            )
            result = StockPipeline().decide(ctx, "prompt")
        actions = sorted(p["action"] for p in result.proposals)
        assert actions == ["BUY", "SELL"]

    def test_ai_call_failure_returns_empty_with_reasoning(self):
        ctx = SimpleNamespace(
            ai_provider="google", ai_model="m", ai_api_key="k",
        )
        with patch("ai_providers.call_ai",
                   side_effect=Exception("Gemini down")):
            result = StockPipeline().decide(ctx, "prompt")
        assert result.proposals == []
        assert "gemini down" in result.reasoning.lower()


# ---------------------------------------------------------------------------
# StockPipeline.execute
# ---------------------------------------------------------------------------

class TestStockExecute:
    def test_empty_verdict_returns_empty_result(self):
        result = StockPipeline().execute(SimpleNamespace(), None)
        assert isinstance(result, ExecutionResult)
        assert result.submitted == []

    def test_approved_proposal_calls_execute_trade(self):
        ctx = SimpleNamespace(db_path=None, user_id=1)
        verdict = SpecialistVerdict(
            approved=[{"action": "BUY", "symbol": "AAPL",
                       "price": 150.0, "confidence": 70}],
        )
        with patch("trader.execute_trade") as mock_exec:
            mock_exec.return_value = {
                "action": "BUY", "symbol": "AAPL", "qty": 10,
            }
            result = StockPipeline().execute(ctx, verdict)
        assert mock_exec.call_count == 1
        assert len(result.submitted) == 1
        assert result.submitted[0]["symbol"] == "AAPL"

    def test_vetoed_proposal_goes_to_skipped(self):
        ctx = SimpleNamespace(db_path=None)
        verdict = SpecialistVerdict(
            vetoed=[{"action": "BUY", "symbol": "AAPL"}],
            veto_log=["AAPL: VETO (pattern_recognizer) — bad pattern"],
        )
        result = StockPipeline().execute(ctx, verdict)
        assert len(result.skipped) == 1
        assert result.skipped[0]["vetoed_by"] == "pattern_recognizer"
        assert "bad pattern" in result.skipped[0]["reason"]

    def test_execute_trade_crash_goes_to_errors(self):
        ctx = SimpleNamespace(db_path=None)
        verdict = SpecialistVerdict(
            approved=[{"action": "BUY", "symbol": "AAPL", "price": 150}],
        )
        with patch("trader.execute_trade",
                   side_effect=RuntimeError("broker down")):
            result = StockPipeline().execute(ctx, verdict)
        assert len(result.errors) == 1
        assert "broker down" in result.errors[0]["reason"]

    def test_skip_result_goes_to_rejected(self):
        """A trader.execute_trade returning {action: SKIP} (wash trade,
        cross-direction, etc.) classifies as rejected, not submitted."""
        ctx = SimpleNamespace(db_path=None)
        verdict = SpecialistVerdict(
            approved=[{"action": "BUY", "symbol": "AAPL", "price": 150}],
        )
        with patch("trader.execute_trade") as mock_exec:
            mock_exec.return_value = {
                "action": "SKIP", "symbol": "AAPL", "reason": "wash trade",
            }
            result = StockPipeline().execute(ctx, verdict)
        assert len(result.rejected) == 1
        assert "wash trade" in result.rejected[0]["reason"]


# ---------------------------------------------------------------------------
# OptionPipeline.generate_candidates
# ---------------------------------------------------------------------------

class TestOptionGenerateCandidates:
    def test_empty_shortlist_returns_empty(self):
        ctx = SimpleNamespace(shortlist=[])
        assert OptionPipeline().generate_candidates(ctx) == []

    def test_oracle_failure_skips_that_symbol_only(self):
        ctx = SimpleNamespace(shortlist=[
            {"symbol": "AAPL", "signal": "BUY", "price": 150.0},
            {"symbol": "BAD", "signal": "BUY", "price": 100.0},
        ])
        def oracle_side_effect(sym):
            if sym == "BAD":
                raise RuntimeError("oracle exploded")
            return {
                "has_options": True,
                "iv_rank": {"rank_pct": 70.0},
            }
        with patch("options_oracle.get_options_oracle",
                   side_effect=oracle_side_effect), \
             patch("options_strategy_advisor.evaluate_candidate_for_multileg",
                   return_value=[{
                       "strategy": "bull_put_spread",
                       "expiry": "2026-06-19",
                       "strikes": {"short": 145, "long": 140},
                       "rationale": "test",
                   }]):
            candidates = OptionPipeline().generate_candidates(ctx)
        syms = [c.symbol for c in candidates]
        assert "AAPL" in syms
        assert "BAD" not in syms

    def test_symbol_without_iv_rank_skipped(self):
        ctx = SimpleNamespace(shortlist=[
            {"symbol": "AAPL", "signal": "BUY", "price": 150.0},
        ])
        with patch("options_oracle.get_options_oracle") as mock_oracle:
            mock_oracle.return_value = {
                "has_options": True,
                "iv_rank": {"rank_pct": None},
            }
            candidates = OptionPipeline().generate_candidates(ctx)
        assert candidates == []

    def test_symbol_without_options_skipped(self):
        ctx = SimpleNamespace(shortlist=[
            {"symbol": "AAPL", "signal": "BUY", "price": 150.0},
        ])
        with patch("options_oracle.get_options_oracle") as mock_oracle:
            mock_oracle.return_value = {"has_options": False}
            candidates = OptionPipeline().generate_candidates(ctx)
        assert candidates == []

    def test_iv_rank_enriches_candidate_extra(self):
        ctx = SimpleNamespace(shortlist=[
            {"symbol": "AAPL", "signal": "BUY", "price": 150.0},
        ])
        with patch("options_oracle.get_options_oracle") as mock_oracle, \
             patch("options_strategy_advisor.evaluate_candidate_for_multileg") as mock_eval:
            mock_oracle.return_value = {
                "has_options": True,
                "iv_rank": {"rank_pct": 80.0},
            }
            mock_eval.return_value = [{
                "strategy": "bull_put_spread",
                "expiry": "2026-06-19",
                "strikes": {"short": 145, "long": 140},
                "rationale": "Bullish + IV rich",
            }]
            candidates = OptionPipeline().generate_candidates(ctx)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.extra["iv_rank"] == 80.0
        assert c.extra["option_strategy"] == "bull_put_spread"
        assert c.signal == "MULTILEG_OPEN"


# ---------------------------------------------------------------------------
# OptionPipeline.decide
# ---------------------------------------------------------------------------

class TestOptionDecide:
    def test_filters_to_option_actions_only(self):
        ctx = SimpleNamespace(
            ai_provider="google", ai_model="m", ai_api_key="k",
        )
        with patch("ai_providers.call_ai") as mock_call_ai:
            mock_call_ai.return_value = (
                '{"trades": ['
                '{"action": "BUY", "symbol": "AAPL"},'
                '{"action": "MULTILEG_OPEN", "symbol": "SPY"},'
                '{"action": "OPTIONS", "symbol": "QQQ"}'
                ']}'
            )
            result = OptionPipeline().decide(ctx, "prompt")
        actions = sorted(p["action"] for p in result.proposals)
        assert actions == ["MULTILEG_OPEN", "OPTIONS"]
        assert "AAPL" not in [p["symbol"] for p in result.proposals]

    def test_cost_cap_exceeded_returns_empty_with_flag(self):
        from cost_guard import CostCapExceeded
        ctx = SimpleNamespace(
            ai_provider="google", ai_model="m", ai_api_key="k",
        )
        cap_exc = CostCapExceeded(
            user_id=1, estimated_cost_usd=0.50,
            action_summary="option_pipeline_decide",
        )
        with patch("ai_providers.call_ai", side_effect=cap_exc):
            result = OptionPipeline().decide(ctx, "prompt")
        assert result.proposals == []
        assert result.raw_response.get("cost_capped") is True


# ---------------------------------------------------------------------------
# run_cycle end-to-end composition
# ---------------------------------------------------------------------------

class TestRunCycleComposition:
    def test_stock_run_cycle_with_no_candidates_returns_empty(self):
        ctx = SimpleNamespace(shortlist=[])
        result = StockPipeline().run_cycle(ctx)
        assert isinstance(result, ExecutionResult)
        assert result.submitted == []

    def test_option_run_cycle_with_no_candidates_returns_empty(self):
        ctx = SimpleNamespace(shortlist=[])
        result = OptionPipeline().run_cycle(ctx)
        assert isinstance(result, ExecutionResult)
        assert result.submitted == []

    def test_stock_full_cycle_dispatches_through_each_method(self):
        ctx = SimpleNamespace(
            shortlist=[
                {"symbol": "AAPL", "signal": "BUY", "price": 150.0,
                 "score": 0.8},
            ],
            ai_provider="google", ai_model="m", ai_api_key="k",
            db_path=None,
        )
        with patch("pipelines.stock_prompt.build_prompt",
                   return_value="prompt-text"), \
             patch("ai_providers.call_ai") as mock_call_ai, \
             patch("pipelines.specialist_router.applicable_specialists",
                   return_value=[]), \
             patch("ensemble.run_ensemble") as mock_ensemble, \
             patch("trader.execute_trade") as mock_trader:
            mock_call_ai.return_value = (
                '{"trades": [{"action": "BUY", "symbol": "AAPL", '
                '"price": 150.0, "confidence": 70}]}'
            )
            mock_ensemble.return_value = {"per_symbol": {"AAPL": {}}}
            mock_trader.return_value = {
                "action": "BUY", "symbol": "AAPL", "qty": 10,
            }
            result = StockPipeline().run_cycle(ctx)
        # Every stage was reached
        assert mock_call_ai.call_count == 1
        assert mock_ensemble.call_count == 1
        assert mock_trader.call_count == 1
        # End-to-end produces a submitted trade
        assert len(result.submitted) == 1
        assert result.submitted[0]["symbol"] == "AAPL"
