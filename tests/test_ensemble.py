"""Tests for Phase 8 — specialist ensemble and meta-coordinator.

Covers:
  - Each specialist's build_prompt / parse_response handles malformed AI output.
  - Ensemble aggregates verdicts: consensus on agreement, sell when disagreement,
    HOLD when all ambiguous, VETO when risk_assessor vetoes.
  - Cost scales with number of specialists, not number of candidates.
"""

from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _candidate(sym: str, signal: str = "BUY", price: float = 10.0) -> dict:
    return {
        "symbol": sym,
        "signal": signal,
        "price": price,
        "reason": f"{sym} setup",
        "votes": {"market_engine": signal},
    }


def _make_ai_response(*entries) -> str:
    """Wrap a list of verdict dicts as a JSON array string."""
    return json.dumps(list(entries))


# ---------------------------------------------------------------------------
# Registry discovery
# ---------------------------------------------------------------------------

class TestSpecialistMarketApplicability:
    """Crypto should only run specialists that have usable data for
    crypto. The other three produce noise without signal and cost tokens."""

    def test_crypto_only_runs_pattern_recognizer(self, sample_ctx, monkeypatch):
        sample_ctx.segment = "crypto"
        calls = []
        def fake_structured(prompt, schema, tool_name="emit", **kwargs):
            calls.append(kwargs.get("purpose"))
            return {"verdicts": []}
        monkeypatch.setattr("ai_providers.call_ai_structured", fake_structured)

        from ensemble import run_ensemble
        run_ensemble(
            [{"symbol": "BTC/USD", "signal": "BUY", "price": 60000,
              "reason": "breakout"}],
            sample_ctx,
            ai_provider="anthropic",
            ai_model="claude-haiku-4-5-20251001",
            ai_api_key="k",
        )
        # Only pattern_recognizer should have been called
        assert all("pattern_recognizer" in c for c in calls), (
            f"crypto should only call pattern_recognizer; got {calls}"
        )

    def test_equity_markets_run_all_four(self, sample_ctx, monkeypatch):
        """Equity profiles keep the full ensemble — the other specialists
        have genuine data (SEC, earnings, options)."""
        sample_ctx.segment = "midcap"
        calls = []
        def fake_structured(prompt, schema, tool_name="emit", **kwargs):
            calls.append(kwargs.get("purpose"))
            return {"verdicts": []}
        monkeypatch.setattr("ai_providers.call_ai_structured", fake_structured)

        from ensemble import run_ensemble
        run_ensemble(
            [{"symbol": "AAPL", "signal": "BUY", "price": 180, "reason": "x"}],
            sample_ctx,
            ai_provider="anthropic",
            ai_model="claude-haiku-4-5-20251001",
            ai_api_key="k",
        )
        specs_called = {c.split(":")[1] for c in calls if c and ":" in c}
        assert specs_called == {
            "earnings_analyst", "pattern_recognizer",
            "sentiment_narrative", "risk_assessor",
        }


class TestSpecialistRegistry:
    def test_discover_all_four_specialists(self):
        from specialists import discover_specialists
        names = {s.NAME for s in discover_specialists()}
        assert names == {
            "earnings_analyst",
            "pattern_recognizer",
            "sentiment_narrative",
            "risk_assessor",
        }

    def test_every_specialist_exposes_required_interface(self):
        from specialists import discover_specialists
        for spec in discover_specialists():
            assert callable(spec.build_prompt)
            assert callable(spec.parse_response)
            assert isinstance(spec.NAME, str)
            assert isinstance(spec.DESCRIPTION, str)

    def test_risk_assessor_has_veto_flag(self):
        from specialists.risk_assessor import HAS_VETO_AUTHORITY
        assert HAS_VETO_AUTHORITY is True


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class TestResponseParsing:
    def test_parses_clean_json(self):
        from specialists._common import extract_verdict_array
        raw = _make_ai_response(
            {"symbol": "AAPL", "verdict": "BUY", "confidence": 80,
             "reasoning": "solid"},
            {"symbol": "MSFT", "verdict": "HOLD", "confidence": 30,
             "reasoning": "mixed"},
        )
        out = extract_verdict_array(raw)
        assert len(out) == 2
        assert out[0]["symbol"] == "AAPL"
        assert out[0]["verdict"] == "BUY"
        assert out[0]["confidence"] == 80

    def test_parses_array_from_noisy_response(self):
        from specialists._common import extract_verdict_array
        raw = "Analysis complete.\n[{\"symbol\": \"A\", \"verdict\": \"BUY\", \"confidence\": 50, \"reasoning\": \"x\"}]\n"
        out = extract_verdict_array(raw)
        assert len(out) == 1
        assert out[0]["symbol"] == "A"

    def test_drops_malformed_entries(self):
        from specialists._common import extract_verdict_array
        raw = _make_ai_response(
            {"symbol": "A", "verdict": "BUY", "confidence": 70, "reasoning": ""},
            {"symbol": "B", "verdict": "MAYBE", "confidence": 50},  # invalid verdict
            {"verdict": "SELL"},                                    # no symbol
            "garbage",                                              # not a dict
        )
        out = extract_verdict_array(raw)
        assert len(out) == 1
        assert out[0]["symbol"] == "A"

    def test_clamps_confidence_to_valid_range(self):
        from specialists._common import extract_verdict_array
        raw = _make_ai_response(
            {"symbol": "A", "verdict": "BUY", "confidence": 250, "reasoning": ""},
            {"symbol": "B", "verdict": "BUY", "confidence": -10, "reasoning": ""},
        )
        out = extract_verdict_array(raw)
        assert out[0]["confidence"] == 100.0
        assert out[1]["confidence"] == 0.0

    def test_returns_empty_on_garbage(self):
        from specialists._common import extract_verdict_array
        assert extract_verdict_array("not json") == []
        assert extract_verdict_array("") == []

    def test_accepts_single_object_not_wrapped_in_array(self):
        """Haiku sometimes returns a single object `{...}` instead of
        an array `[{...}]` despite the prompt. This is the exact bug
        that caused every specialist to abstain in production."""
        from specialists._common import extract_verdict_array
        raw = '{"symbol": "AAPL", "verdict": "BUY", "confidence": 70, "reasoning": "x"}'
        out = extract_verdict_array(raw)
        assert len(out) == 1
        assert out[0]["symbol"] == "AAPL"
        assert out[0]["verdict"] == "BUY"

    def test_accepts_multiple_concatenated_objects(self):
        """Another Haiku failure mode: streaming one object per line
        instead of a single JSON array."""
        from specialists._common import extract_verdict_array
        raw = (
            '{"symbol": "AAPL", "verdict": "BUY", "confidence": 70, "reasoning": "x"}\n'
            '{"symbol": "MSFT", "verdict": "SELL", "confidence": 60, "reasoning": "y"}\n'
        )
        out = extract_verdict_array(raw)
        assert len(out) == 2
        assert out[0]["symbol"] == "AAPL"
        assert out[1]["symbol"] == "MSFT"

    def test_accepts_object_with_surrounding_prose(self):
        from specialists._common import extract_verdict_array
        raw = (
            "Sure, here's my analysis:\n"
            '{"symbol": "AAPL", "verdict": "HOLD", "confidence": 30, "reasoning": "mixed"}\n'
            "Let me know if you need more."
        )
        out = extract_verdict_array(raw)
        assert len(out) == 1
        assert out[0]["symbol"] == "AAPL"


# ---------------------------------------------------------------------------
# Specialist prompt construction
# ---------------------------------------------------------------------------

class TestPrompts:
    def test_prompts_are_non_empty_and_mention_symbols(self, sample_ctx):
        from specialists import discover_specialists
        candidates = [_candidate("AAPL"), _candidate("MSFT")]
        for spec in discover_specialists():
            prompt = spec.build_prompt(candidates, sample_ctx)
            assert "AAPL" in prompt
            assert "MSFT" in prompt
            assert len(prompt) > 100

    def test_prompts_instruct_strict_json(self, sample_ctx):
        from specialists import discover_specialists
        candidates = [_candidate("AAPL")]
        for spec in discover_specialists():
            prompt = spec.build_prompt(candidates, sample_ctx)
            # Each specialist must tell the AI to return a JSON array
            assert "JSON" in prompt or "json" in prompt


# ---------------------------------------------------------------------------
# Ensemble aggregation
# ---------------------------------------------------------------------------

class TestEnsembleAggregation:
    def _run_with_verdicts(self, sample_ctx, verdicts_by_spec, monkeypatch):
        """Patch call_ai_structured to return the canned verdicts per specialist.

        The ensemble now routes Anthropic calls through call_ai_structured
        (tool_use) which returns a dict {"verdicts": [...]}, not a string."""
        def fake_structured(prompt, schema, tool_name="emit",
                            provider="anthropic", model=None, api_key=None,
                            max_tokens=4096, db_path=None, purpose=None):
            # Route by specialist name embedded in purpose tag
            if purpose and ":" in purpose:
                spec_name = purpose.split(":", 1)[1]
            else:
                # Fallback for other callers
                return {"verdicts": []}
            return {"verdicts": verdicts_by_spec.get(spec_name, [])}

        monkeypatch.setattr("ai_providers.call_ai_structured", fake_structured)
        # Also stub plain call_ai so any fallback path doesn't hit network
        monkeypatch.setattr("ai_providers.call_ai",
                            lambda *a, **kw: "[]")

        from ensemble import run_ensemble
        return run_ensemble(
            [_candidate("AAPL"), _candidate("MSFT")],
            sample_ctx,
            ai_provider="anthropic",
            ai_model="claude-haiku-4-5-20251001",
            ai_api_key="k",
        )

    def test_full_agreement_yields_buy(self, sample_ctx, monkeypatch):
        verdict = {"verdict": "BUY", "confidence": 80, "reasoning": "x"}
        verdicts = {
            "earnings_analyst":   [{"symbol": "AAPL", **verdict}, {"symbol": "MSFT", **verdict}],
            "pattern_recognizer": [{"symbol": "AAPL", **verdict}, {"symbol": "MSFT", **verdict}],
            "sentiment_narrative":[{"symbol": "AAPL", **verdict}, {"symbol": "MSFT", **verdict}],
            "risk_assessor":      [{"symbol": "AAPL", **verdict}, {"symbol": "MSFT", **verdict}],
        }
        result = self._run_with_verdicts(sample_ctx, verdicts, monkeypatch)
        assert result["per_symbol"]["AAPL"]["verdict"] == "BUY"
        assert result["per_symbol"]["AAPL"]["confidence"] == 100

    def test_risk_veto_overrides_bullish_consensus(self, sample_ctx, monkeypatch):
        bullish = {"verdict": "BUY", "confidence": 90, "reasoning": "strong"}
        veto = {"verdict": "VETO", "confidence": 80, "reasoning": "illiquid"}
        verdicts = {
            "earnings_analyst":   [{"symbol": "AAPL", **bullish}],
            "pattern_recognizer": [{"symbol": "AAPL", **bullish}],
            "sentiment_narrative":[{"symbol": "AAPL", **bullish}],
            "risk_assessor":      [{"symbol": "AAPL", **veto}],
        }
        result = self._run_with_verdicts(sample_ctx, verdicts, monkeypatch)
        assert result["per_symbol"]["AAPL"]["vetoed"] is True
        assert result["per_symbol"]["AAPL"]["verdict"] == "VETO"
        assert result["per_symbol"]["AAPL"]["veto_reason"] == "illiquid"

    def test_low_confidence_below_floor_ignored(self, sample_ctx, monkeypatch):
        # One specialist says BUY @ 80, others are HOLD @ 10 (below floor)
        verdicts = {
            "earnings_analyst":   [{"symbol": "AAPL", "verdict": "BUY", "confidence": 80, "reasoning": ""}],
            "pattern_recognizer": [{"symbol": "AAPL", "verdict": "HOLD", "confidence": 10, "reasoning": ""}],
            "sentiment_narrative":[{"symbol": "AAPL", "verdict": "HOLD", "confidence": 10, "reasoning": ""}],
            "risk_assessor":      [{"symbol": "AAPL", "verdict": "HOLD", "confidence": 10, "reasoning": ""}],
        }
        result = self._run_with_verdicts(sample_ctx, verdicts, monkeypatch)
        # Only the BUY contributes — verdict should be BUY
        assert result["per_symbol"]["AAPL"]["verdict"] == "BUY"

    def test_disagreement_buy_vs_sell(self, sample_ctx, monkeypatch):
        # Two bullish, two bearish — bullish slightly weighted via pattern_recognizer
        verdicts = {
            "earnings_analyst":   [{"symbol": "AAPL", "verdict": "BUY", "confidence": 80, "reasoning": ""}],
            "pattern_recognizer": [{"symbol": "AAPL", "verdict": "BUY", "confidence": 80, "reasoning": ""}],
            "sentiment_narrative":[{"symbol": "AAPL", "verdict": "SELL", "confidence": 80, "reasoning": ""}],
            "risk_assessor":      [{"symbol": "AAPL", "verdict": "SELL", "confidence": 70, "reasoning": ""}],
        }
        result = self._run_with_verdicts(sample_ctx, verdicts, monkeypatch)
        # pattern weight 1.2 + earnings 1.0 → BUY side 1.76
        # sentiment 0.9 + risk 1.0 → SELL side 1.42
        # BUY wins
        assert result["per_symbol"]["AAPL"]["verdict"] == "BUY"

    def test_abstention_when_specialists_fail_or_return_nothing(self, sample_ctx, monkeypatch):
        verdicts = {
            "earnings_analyst":   [],
            "pattern_recognizer": [],
            "sentiment_narrative":[],
            "risk_assessor":      [],
        }
        result = self._run_with_verdicts(sample_ctx, verdicts, monkeypatch)
        assert result["per_symbol"]["AAPL"]["verdict"] == "HOLD"
        # Each specialist shows up as ABSTAIN in the breakdown
        specs = result["per_symbol"]["AAPL"]["specialists"]
        assert all(s["verdict"] == "ABSTAIN" for s in specs)


# ---------------------------------------------------------------------------
# Cost characteristics
# ---------------------------------------------------------------------------

class TestCostCharacteristics:
    def test_cost_scales_with_chunks_not_candidate_count(self,
                                                          sample_ctx,
                                                          monkeypatch):
        """Ensemble chunks candidates into groups of CHUNK_SIZE. Cost is
        specialists × ceil(N_shortlisted / CHUNK_SIZE), NOT candidates."""
        calls = {"n": 0}

        def fake_structured(prompt, schema, tool_name="emit", **kwargs):
            calls["n"] += 1
            return {"verdicts": []}

        monkeypatch.setattr("ai_providers.call_ai_structured", fake_structured)

        from ensemble import run_ensemble, CHUNK_SIZE
        # Pass 50 candidates — ensemble caps at max_candidates=15, then
        # chunks into groups of CHUNK_SIZE (= 5) → 3 chunks per specialist.
        # 4 specialists × 3 chunks = 12 calls.
        candidates = [_candidate(f"T{i}") for i in range(50)]
        result = run_ensemble(
            candidates, sample_ctx,
            ai_provider="anthropic",
            ai_model="claude-haiku-4-5-20251001",
            ai_api_key="k",
        )
        expected = 4 * ((15 + CHUNK_SIZE - 1) // CHUNK_SIZE)
        assert result["cost_calls"] == expected
        assert calls["n"] == expected

    def test_single_chunk_when_few_candidates(self, sample_ctx, monkeypatch):
        """3 candidates fit in one chunk — cost should be 4 calls (one
        per specialist), not 12."""
        calls = {"n": 0}
        def fake_structured(prompt, schema, tool_name="emit", **kwargs):
            calls["n"] += 1
            return {"verdicts": []}
        monkeypatch.setattr("ai_providers.call_ai_structured", fake_structured)

        from ensemble import run_ensemble
        result = run_ensemble(
            [_candidate(f"T{i}") for i in range(3)],
            sample_ctx,
            ai_provider="anthropic",
            ai_model="claude-haiku-4-5-20251001",
            ai_api_key="k",
        )
        assert result["cost_calls"] == 4

    def test_empty_candidates_no_calls(self, sample_ctx, monkeypatch):
        calls = {"n": 0}

        def fake_call_ai(*a, **kw):
            calls["n"] += 1
            return "[]"

        monkeypatch.setattr("ai_providers.call_ai", fake_call_ai)

        from ensemble import run_ensemble
        result = run_ensemble(
            [], sample_ctx,
            ai_provider="anthropic",
            ai_model="claude-haiku-4-5-20251001",
            ai_api_key="k",
        )
        assert result["cost_calls"] == 0
        assert calls["n"] == 0


# ---------------------------------------------------------------------------
# Format helper for prompt injection
# ---------------------------------------------------------------------------

class TestFormatForFinalPrompt:
    def test_formats_vetoed_entry(self):
        from ensemble import format_for_final_prompt
        per_symbol = {
            "AAPL": {
                "verdict": "VETO", "confidence": 100, "vetoed": True,
                "veto_reason": "illiquid and gappy",
                "specialists": [
                    {"specialist": "risk_assessor", "verdict": "VETO", "confidence": 80, "reasoning": ""},
                ],
            }
        }
        out = format_for_final_prompt(per_symbol, "AAPL")
        assert "VETOED" in out

    def test_formats_standard_consensus(self):
        from ensemble import format_for_final_prompt
        per_symbol = {
            "AAPL": {
                "verdict": "BUY", "confidence": 75, "vetoed": False,
                "specialists": [
                    {"specialist": "earnings_analyst", "verdict": "BUY", "confidence": 80, "reasoning": ""},
                    {"specialist": "pattern_recognizer", "verdict": "BUY", "confidence": 70, "reasoning": ""},
                ],
            }
        }
        out = format_for_final_prompt(per_symbol, "AAPL")
        assert "BUY" in out
        assert "75%" in out
