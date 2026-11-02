"""2026-11-02 — entry-quality policy set (docs/25 D13) and apex prompt
caching (D14). Evidence: docs/26 Experiment 2 §4 — 10,532 resolved
directional predictions since the 08-24 reset.

1. Three strategies that lose on their own direction are off
   (strategy_policy.OPERATOR_DISABLED_STRATEGIES) at the active set,
   the shadow set, ranking and the prompt line; volume_spike is marked.
2. Confidence drives nothing: sizing ladder flat, gate inert (re-armable).
3. Specialist VETO is binding for new entries (post-selection gate).
4. Regime: bull/bear need a real 40-bar move (classify_trend), shared by
   detect_regime and the short-slate throttle.
5. Apex prompt = stable block + marker + live data; the call sends the
   stable block as a cacheable system prompt on every vendor.
"""
from __future__ import annotations

import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _src(rel):
    return open(os.path.join(REPO, rel)).read()


# ── 1. strategies ──────────────────────────────────────────────────────

class TestStrategyPolicy:
    def test_disabled_set_is_the_measured_three(self):
        from strategy_policy import OPERATOR_DISABLED_STRATEGIES
        assert OPERATOR_DISABLED_STRATEGIES == frozenset(
            {"insider_selling_cluster", "earnings_drift", "vol_regime"})

    def test_disabled_strategies_never_in_the_active_set(self):
        from strategies import get_active_strategies, discover_strategies
        from strategy_policy import OPERATOR_DISABLED_STRATEGIES
        discovered = {m.NAME for m in discover_strategies("stocks")}
        assert OPERATOR_DISABLED_STRATEGIES <= discovered, "modules still exist (not deleted)"
        active = {m.NAME for m in get_active_strategies("stocks", db_path=None)}
        assert not (active & OPERATOR_DISABLED_STRATEGIES)
        assert "volume_dryup_breakout" in active or "market_engine" in active

    def test_ranking_drops_a_primary_vote_from_a_disabled_strategy(self):
        src = _src("trade_pipeline.py")
        assert "OPERATOR_DISABLED_STRATEGIES as _op_disabled" in src
        assert "deprecated_types = set(deprecated_types or set()) | set(_op_disabled)" in src

    def test_render_votes_omits_disabled_and_marks_volume_spike(self):
        from strategy_policy import render_votes, EMPHASIS_MARK
        s = render_votes({"volume_spike": "BUY", "earnings_drift": "BUY",
                          "mean_reversion": "HOLD", "gap_reversal": "SELL"})
        assert "earnings_drift" not in s and "mean_reversion" not in s
        assert s.startswith(f"volume_spike=BUY {EMPHASIS_MARK} [")
        assert "gap_reversal=SELL" in s
        assert render_votes({"x": "HOLD"}) == "no strong votes"

    def test_prompt_line_uses_the_policy_renderer(self):
        assert "from strategy_policy import render_votes" in _src("ai_analyst.py")


# ── 2. confidence ──────────────────────────────────────────────────────

class TestConfidenceOutOfDecisions:
    def test_sizing_is_flat(self):
        from confidence_sizing import confidence_multiplier, apply_confidence_sizing, CONFIDENCE_SIZING_FLAT
        assert CONFIDENCE_SIZING_FLAT
        assert {confidence_multiplier(c) for c in (0, 45, 60, 75, 85, 100, None)} == {1.0}
        assert apply_confidence_sizing(0.05, 85, 0.10) == 0.05
        assert apply_confidence_sizing(0.05, 45, 0.10) == 0.05

    def test_gate_inert_by_default_and_armable(self, monkeypatch):
        import trade_pipeline as tp
        assert tp.CONFIDENCE_GATE_ACTIVE is False
        ctx = MagicMock(); ctx.ai_confidence_threshold = 60; ctx.db_path = None
        trades = [{"symbol": "A", "action": "BUY", "confidence": 5}]
        details = []
        assert tp._apply_confidence_gate(trades, ctx, None, details) == trades and details == []
        monkeypatch.setattr(tp, "CONFIDENCE_GATE_ACTIVE", True)
        assert tp._apply_confidence_gate(trades, ctx, None, details) == []
        assert details and details[0]["action"] == "CONFIDENCE_BLOCKED"


# ── 3. specialist veto ─────────────────────────────────────────────────

class TestBindingVeto:
    def test_gate_blocks_entries_passes_exits_and_records(self, monkeypatch):
        import trade_pipeline as tp
        drops = []
        import journal
        monkeypatch.setattr(journal, "record_trade_drop", lambda **kw: drops.append(kw))
        ctx = SimpleNamespace(db_path="/x/p.db")
        trades = [
            {"symbol": "CRH", "action": "SHORT", "confidence": 70, "_pred_id": 7},
            {"symbol": "CF", "action": "SELL", "confidence": 70},
            {"symbol": "AAPL", "action": "BUY", "confidence": 70},
            {"symbol": "CRH", "action": "COVER", "confidence": 70},
        ]
        details = []
        kept = tp._apply_specialist_veto_gate(trades, {"CRH": "dark pool accumulation", "CF": "x"},
                                              ctx, "cyc", details)
        assert [(t["symbol"], t["action"]) for t in kept] == [("CF", "SELL"), ("AAPL", "BUY"), ("CRH", "COVER")]
        assert details[0]["action"] == "SPECIALIST_VETOED" and details[0]["symbol"] == "CRH"
        assert drops[0]["drop_code"] == "SPECIALIST_VETO" and drops[0]["pred_id"] == 7

    def test_wired_after_recording_and_before_confidence_gate(self):
        src = _src("trade_pipeline.py")
        i_rec = src.index("# Record a prediction for EVERY candidate the AI analyzed.")
        i_veto = src.index("# ── STEP 4.8: Specialist veto is BINDING")
        i_conf = src.index("# ── STEP 4.85: Confidence gate")
        assert i_rec < i_veto < i_conf
        assert "_ensemble_vetoed = {" in src and 'v.get("vetoed")' in src

    def test_drop_code_is_cross_cutting_in_the_ui(self):
        from views import _CROSS_CUTTING_DROP_CODES
        assert "SPECIALIST_VETO" in _CROSS_CUTTING_DROP_CODES


# ── 4. regime ──────────────────────────────────────────────────────────

def _series(kind, n=120):
    import math
    if kind == "flat":      # rally then flat range: old rule says bull
        return [100 + i * 0.5 for i in range(60)] + [130 + 2 * math.sin(i / 3) for i in range(n - 60)]
    if kind == "rally":
        return [100 + i * 0.4 for i in range(n)]
    if kind == "selloff":
        return [160 - i * 0.4 for i in range(n)]
    raise ValueError(kind)


class TestRegimeRule:
    def test_flat_after_rally_is_sideways_rally_bull_selloff_bear(self):
        from market_regime import classify_trend
        assert classify_trend(_series("flat"))["regime"] == "sideways"
        assert classify_trend(_series("rally"))["regime"] == "bull"
        assert classify_trend(_series("selloff"))["regime"] == "bear"

    def test_thin_data_is_never_a_confident_label(self):
        from market_regime import classify_trend
        assert classify_trend([100 + i for i in range(30)])["regime"] == "sideways"
        assert classify_trend([100 + i for i in range(52)])["regime"] == "sideways"   # <41 bars of return

    def test_detect_regime_uses_the_rule(self, monkeypatch):
        import pandas as pd
        import market_regime
        import market_data
        market_regime._cache["regime"] = None; market_regime._cache["regime_ts"] = 0
        closes = _series("flat", 100)
        df = pd.DataFrame({"close": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes]})
        monkeypatch.setattr(market_data, "get_bars", lambda symbol, limit=60, **kw: df)
        monkeypatch.setattr(market_regime, "_vix_from_spy_options", lambda: 15.0)
        r = market_regime.detect_regime()
        assert r["regime"] == "sideways" and r["spy_ret_40d"] is not None
        market_regime._cache["regime"] = None

    def test_short_slate_throttle_shares_the_rule(self):
        src = _src("trade_pipeline.py")
        assert "from market_regime import classify_trend as _classify_trend" in src
        assert 'trend["regime"] == "bull" and close_now > sma_200' in src
        assert "sma_20 > sma_50" not in src


# ── 5. prompt caching ──────────────────────────────────────────────────

class TestPromptCaching:
    def test_split_helper(self):
        from ai_analyst import split_prompt_for_caching, PROMPT_LIVE_DATA_MARKER
        assert split_prompt_for_caching("plain") == (None, "plain")
        s, u = split_prompt_for_caching("STABLE" + PROMPT_LIVE_DATA_MARKER + "LIVE")
        assert (s, u) == ("STABLE", "LIVE")

    def test_builder_emits_stable_then_marker_then_live(self):
        from ai_analyst import _build_batch_prompt, PROMPT_LIVE_DATA_MARKER
        ctx = SimpleNamespace(max_position_pct=0.1, max_total_positions=10, enable_short_selling=True,
                              segment="stocks", signal_weights=None)
        cands = [{"symbol": "AAPL", "price": 100.0, "signal": "BUY", "score": 3, "votes": {"volume_spike": "BUY"},
                  "reason": "r", "rsi": 50, "volume_ratio": 1.2}]
        p = _build_batch_prompt(cands, {"positions": [], "cash": 1000, "equity": 1000},
                                {"regime": "sideways", "vix": 15, "spy_trend": "flat"}, ctx)
        stable, live = p.split(PROMPT_LIVE_DATA_MARKER, 1)
        assert stable.startswith("You are a portfolio manager for an automated stocks trading system.")
        assert "RULES:" in stable and "Respond ONLY with valid JSON" in stable
        assert "AAPL" not in stable                      # no live data in the cached block
        assert "AAPL" in live and "THIS CYCLE:" in live and "Actions allowed:" in live
        assert "volume_spike=BUY ★" in live

    def test_call_site_sends_the_stable_block_as_system(self):
        src = _src("ai_analyst.py")
        assert "_system_block, _user_block = split_prompt_for_caching(prompt)" in src
        assert '**({"system": _system_block} if _system_block else {})' in src

    def test_anthropic_system_is_a_cached_block(self, monkeypatch):
        import ai_providers as ap
        captured = {}
        class _M:
            def create(self, **kw):
                captured.update(kw)
                return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="ok")],
                                             stop_reason="end_turn",
                                             usage=types.SimpleNamespace(input_tokens=1, output_tokens=1,
                                                                         cache_read_input_tokens=900))
        class _C:
            def __init__(self, api_key=None): self.messages = _M()
        monkeypatch.setattr("anthropic.Anthropic", _C)
        text, _i, _o, cached = ap._call_anthropic("live", "claude-haiku-5-5", "k", 256, system="STABLE")
        assert captured["system"] == [{"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}}]
        assert captured["messages"] == [{"role": "user", "content": "live"}]
        assert cached == 900

    def test_openai_and_google_place_the_system_block(self, monkeypatch):
        import ai_providers as ap
        captured = {}
        class _Comp:
            def create(self, **kw):
                captured.update(kw)
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="{}"))],
                    usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                                prompt_tokens_details=types.SimpleNamespace(cached_tokens=5)))
        class _OpenAI:
            def __init__(self, api_key=None, base_url=None):
                self.chat = types.SimpleNamespace(completions=_Comp())
        monkeypatch.setattr("openai.OpenAI", _OpenAI)
        _t, _i, _o, cached = ap._call_openai("live", "gpt-6.1-terra", "k", 64, system="STABLE")
        assert captured["messages"][0] == {"role": "system", "content": "STABLE"}
        assert "reasoning_effort" not in captured          # gpt-6 runs at the vendor default
        assert cached == 5
        _t, _i, _o, _c = ap._call_deepseek("live", "deepseek-flash", "k", 64, system="STABLE")
        assert captured["messages"][0]["role"] == "system"
        gcap = {}
        class _Models:
            def generate_content(self, **kw):
                gcap.update(kw)
                return types.SimpleNamespace(text="{}", usage_metadata=types.SimpleNamespace(
                    prompt_token_count=1, candidates_token_count=1, cached_content_token_count=0))
        class _G:
            def __init__(self, api_key=None): self.models = _Models()
        import google.genai as genai
        monkeypatch.setattr(genai, "Client", _G)
        ap._call_google("live", "gemini-3.1-flash-lite", "k", 64, system="STABLE")
        assert gcap["config"]["system_instruction"] == "STABLE"

    def test_call_ai_forwards_system_only_when_set(self, monkeypatch):
        import ai_providers as ap
        seen = []
        monkeypatch.setattr(ap, "_call_provider", lambda *a, **k: seen.append(k) or ("{}", 1, 1, 0))
        monkeypatch.setattr(ap, "_RETRY_DELAYS_SECONDS", ())
        ap.call_ai("p", provider="openai", model="gpt-5.6-luna", api_key="k")
        ap.call_ai("p", provider="openai", model="gpt-5.6-luna", api_key="k", system="S")
        assert "system" not in seen[0] and seen[1]["system"] == "S"
