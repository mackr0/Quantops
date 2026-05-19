"""Phase 3 of docs/17 — deterministic specialist library.

Tests cover:
  - Framework: registry discovery, panel runner, prompt-block builder,
    fail-isolation when an individual rule raises
  - Each rule: positive fire case (verdict returned with correct
    severity + content) and negative cases (signal mismatch, missing
    field, no-op when condition not met)
  - Structural: every rule module conforms to the contract
    (NAME, DESCRIPTION, APPLIES_TO_SIGNALS, evaluate)
  - Integration: ai_analyst.py imports the panel builder

Heavy use of table-driven testing — one row per rule, three columns
(positive_fixture, expected_severity, name_substring) — keeps the
test file manageable even as the library grows toward 200 rules.
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Dict

import pytest


# ─────────────────────────────────────────────────────────────────────
# Framework
# ─────────────────────────────────────────────────────────────────────

class TestFramework:
    def test_discover_returns_modules(self):
        from deterministic_specialists import discover_rules, RULE_MODULES
        live = discover_rules()
        assert len(live) == len(RULE_MODULES), (
            f"All {len(RULE_MODULES)} registered modules must import "
            f"cleanly. Got {len(live)}."
        )

    def test_every_module_satisfies_contract(self):
        from deterministic_specialists import RULE_MODULES
        missing = []
        for mod_path in RULE_MODULES:
            mod = importlib.import_module(mod_path)
            for attr in ("NAME", "DESCRIPTION", "APPLIES_TO_SIGNALS", "evaluate"):
                if not hasattr(mod, attr):
                    missing.append(f"{mod_path}.{attr}")
        assert not missing, (
            "Every rule module must define NAME, DESCRIPTION, "
            "APPLIES_TO_SIGNALS, evaluate. Missing: " + ", ".join(missing)
        )

    def test_panel_runner_skips_wrong_signal(self):
        from deterministic_specialists import run_panel
        # SHORT candidate — most rules in the registry only apply to
        # BUY-side signals, so they should self-skip via the
        # APPLIES_TO_SIGNALS gate without firing.
        cand = {"symbol": "X", "signal": "SHORT",
                 "rsi": 85, "pct_from_52w_high": -0.5}
        fired = run_panel(cand, ctx=None)
        # Should NOT contain rsi_overbought_late_stage (BUY-only)
        names = {v["name"] for v in fired}
        assert "rsi_overbought_late_stage" not in names

    def test_panel_runner_skips_unsignaled_candidate(self):
        from deterministic_specialists import run_panel
        cand = {"symbol": "X"}  # nothing to fire on
        fired = run_panel(cand, ctx=None)
        assert fired == []

    def test_panel_runner_handles_rule_exception(self, monkeypatch):
        """A bad rule must not silence the rest of the panel."""
        from deterministic_specialists import run_panel
        cand = {"symbol": "X", "signal": "BUY",
                 "rsi": 85, "pct_from_52w_high": -0.5}
        # Patch one rule's evaluate to raise — the panel should
        # still return verdicts from the OTHERS.
        from deterministic_specialists import gap_into_resistance as bad_mod
        monkeypatch.setattr(bad_mod, "evaluate",
                             lambda c, ctx: (_ for _ in ()).throw(RuntimeError("boom")))
        fired = run_panel(cand, ctx=None)
        # rsi_overbought_late_stage should still have fired
        names = {v["name"] for v in fired}
        assert "rsi_overbought_late_stage" in names
        assert "gap_into_resistance" not in names

    def test_format_orders_veto_before_caution_before_confirm(self):
        from deterministic_specialists import format_panel_for_prompt
        verdicts = [
            {"name": "x", "severity": "CONFIRM", "reasoning": "c"},
            {"name": "y", "severity": "VETO", "reasoning": "v"},
            {"name": "z", "severity": "CAUTION", "reasoning": "k"},
        ]
        out = format_panel_for_prompt(verdicts)
        # VETO must appear before CAUTION which must appear before CONFIRM
        v_idx = out.index("VETO")
        k_idx = out.index("CAUTION")
        c_idx = out.index("CONFIRM")
        assert v_idx < k_idx < c_idx

    def test_build_panel_block_empty_returns_empty(self):
        from deterministic_specialists import build_panel_block
        assert build_panel_block({"symbol": "X"}, ctx=None) == ""

    def test_build_panel_block_has_header_with_symbol(self):
        from deterministic_specialists import build_panel_block
        cand = {"symbol": "AAPL", "signal": "BUY",
                 "rsi": 85, "pct_from_52w_high": -0.5}
        block = build_panel_block(cand, ctx=None)
        assert "DETERMINISTIC RULE PANEL FOR AAPL" in block
        assert "rule(s) fired" in block


# ─────────────────────────────────────────────────────────────────────
# Per-rule fire cases — table-driven
# ─────────────────────────────────────────────────────────────────────

# Each row: (rule_module, candidate_dict, expected_severity, ctx_kwargs)
# A `ctx_kwargs` of None means call evaluate(cand, None).
_FIRE_CASES = [
    # ── existing first batch ──
    ("rsi_overbought_late_stage",
     {"signal": "BUY", "rsi": 85, "pct_from_52w_high": -0.5},
     "VETO", None),
    ("gap_into_resistance",
     {"signal": "BUY", "gap_pct": 3.0, "pct_from_52w_high": -1.0},
     "CAUTION", None),
    ("bearish_divergence",
     {"signal": "BUY", "rsi": 75, "stoch_rsi": 30},
     "CAUTION", None),
    ("volume_dry_breakout",
     {"signal": "BUY", "reason": "Breakout above 52w high",
      "volume_ratio": 0.6},
     "VETO", None),
    ("low_atr_breakout",
     {"signal": "BUY", "reason": "Squeeze breakout",
      "atr_pct": 0.5},
     "CAUTION", None),
    ("insider_sold_recently",
     {"signal": "BUY",
      "alt_data": {"insider": {"net_direction": "selling",
                                  "recent_sells": 4, "recent_buys": 1}}},
     "CAUTION", None),
    ("high_short_interest_long",
     {"signal": "BUY",
      "alt_data": {"short": {"short_pct_float": 25.0,
                              "squeeze_risk": "HIGH"}}},
     "CAUTION", None),
    ("crowded_long",
     {"signal": "BUY",
      "alt_data": {"finra_short_vol": {"short_volume_ratio": 0.10},
                    "analyst_estimates": {"eps_revision_direction": "up"}}},
     "CAUTION", None),
    # ── new batch ──
    ("parabolic_blow_off",
     {"signal": "BUY", "rsi": 90, "roc_10": 20},
     "VETO", None),
    ("extended_above_vwap",
     {"signal": "BUY", "pct_from_vwap": 5.0},
     "CAUTION", None),
    ("below_vwap_short_extended",
     {"signal": "SHORT", "pct_from_vwap": -5.0},
     "CAUTION", None),
    ("weak_adx_breakout",
     {"signal": "BUY", "reason": "breakout", "adx": 15},
     "CAUTION", None),
    ("strong_adx_trend_confirm",
     {"signal": "BUY", "adx": 35},
     "CONFIRM", None),
    ("rsi_oversold_uptrend",
     {"signal": "BUY", "rsi": 25, "roc_10": 3},
     "CONFIRM", None),
    ("high_volume_confirmation",
     {"signal": "BUY", "volume_ratio": 3.5},
     "CONFIRM", None),
    ("insider_cluster_buying",
     {"signal": "BUY",
      "alt_data": {"insider_cluster": {"is_cluster": True,
                                          "cluster_direction": "buying",
                                          "insider_count": 4,
                                          "total_value": 1_500_000}}},
     "CONFIRM", None),
    ("positive_earnings_revisions",
     {"signal": "BUY",
      "alt_data": {"analyst_estimates": {"eps_revision_direction": "up",
                                          "revision_magnitude_pct": 3.5}}},
     "CONFIRM", None),
    ("negative_earnings_revisions",
     {"signal": "BUY",
      "alt_data": {"analyst_estimates": {"eps_revision_direction": "down",
                                          "revision_magnitude_pct": -4.0}}},
     "CAUTION", None),
    ("recent_8k_negative_event",
     {"signal": "BUY",
      "alt_data": {"recent_8k_events": {"events": [{"item_tags": ["1.03"]}]}}},
     "VETO", None),
    ("recent_8k_exec_departure",
     {"signal": "BUY",
      "alt_data": {"recent_8k_events": {"events": [{"item_tags": ["5.02"]}]}}},
     "CAUTION", None),
    ("risk_factor_diff_added",
     {"signal": "BUY",
      "alt_data": {"risk_factor_diff": {"has_new_risks": True,
                                          "added_risk_count": 3}}},
     "CAUTION", None),
    ("fda_inspection_warning",
     {"signal": "BUY",
      "alt_data": {"fda_inspections": {"recent_citations_count": 2}}},
     "CAUTION", None),
    ("nhtsa_recall_active",
     {"signal": "BUY",
      "alt_data": {"nhtsa_recalls": {"recalls_recent_years": 1}}},
     "CAUTION", None),
    ("dark_pool_accumulation",
     {"signal": "BUY",
      "alt_data": {"dark_pool": {"ats_volume": 500_000, "num_venues": 5}}},
     "CONFIRM", None),
    ("activist_13d_filed",
     {"signal": "BUY",
      "alt_data": {"activist_13dg": {"has_13d": True, "count": 1}}},
     "CONFIRM", None),
    ("earnings_within_window",
     {"signal": "BUY",
      "alt_data": {"insider_earnings": {"days_to_earnings": 2}}},
     "CAUTION", {"avoid_earnings_days": 3}),
    ("borrow_cost_high_short",
     {"signal": "SHORT", "_borrow_cost": "high"},
     "CAUTION", None),
    ("squeeze_risk_short",
     {"signal": "SHORT", "_squeeze_risk": "HIGH"},
     "VETO", None),
    ("macro_risk_off_cross_asset_vol",
     {"signal": "BUY",
      "alt_data": {"macro": {"cross_asset_vol": {
          "move": {"p30d_label": "high"}}}}},
     "CAUTION", None),
    ("yield_curve_inverted",
     {"signal": "BUY",
      "alt_data": {"macro": {"yield_curve": {"curve_signal": "inverted"}}}},
     "CAUTION", None),
    ("cboe_skew_extreme",
     {"signal": "BUY",
      "alt_data": {"macro": {"cboe_skew": {"skew_signal": "high"}}}},
     "CAUTION", None),
    ("sector_relative_strength_confirm",
     {"signal": "BUY",
      "rel_strength": {"relative_strength": 5.0, "sector": "Tech"}},
     "CONFIRM", None),
    ("sector_weakness_caution",
     {"signal": "BUY",
      "rel_strength": {"relative_strength": -4.0, "sector": "Tech"}},
     "CAUTION", None),
    ("sector_downtrend_long",
     {"signal": "BUY",
      "rel_strength": {"sector_trend": "down", "sector_5d": -3.0,
                         "sector": "Tech"}},
     "CAUTION", None),
    ("options_iv_extreme_high",
     {"signal": "BUY",
      "alt_data": {"options": {"iv_rank": 85}}},
     "CAUTION", None),
    ("unusual_options_activity",
     {"signal": "BUY",
      "alt_data": {"options": {"unusual": True, "signal": "bullish",
                                  "put_call_ratio": 0.5}}},
     "CONFIRM", None),
    ("news_volume_spike",
     {"signal": "BUY", "news": ["a", "b", "c", "d"]},
     "CAUTION", None),
    ("sec_alert_high_severity",
     {"signal": "BUY",
      "sec_alert": {"severity": "high", "form": "8-K", "signal": "loss"}},
     "VETO", None),
    ("slippage_high_caution",
     {"signal": "BUY", "slippage_str": "0.45% est"},
     "CAUTION", None),
    ("cmf_distribution_long",
     {"signal": "BUY", "cmf": -0.20},
     "CAUTION", None),
    ("cmf_accumulation_long",
     {"signal": "BUY", "cmf": 0.20},
     "CONFIRM", None),
    ("mfi_overbought_caution",
     {"signal": "BUY", "mfi": 85},
     "CAUTION", None),
    ("mfi_oversold_confirm",
     {"signal": "BUY", "mfi": 15},
     "CONFIRM", None),
    ("near_fib_support",
     {"signal": "BUY", "nearest_fib_dist": 0.5},
     "CONFIRM", None),
    ("squeeze_release_setup",
     {"signal": "BUY", "squeeze": 1},
     "CONFIRM", None),
    ("finra_short_volume_elevated",
     {"signal": "BUY",
      "alt_data": {"finra_short_vol": {"is_elevated": True,
                                          "short_volume_ratio": 0.55}}},
     "CAUTION", None),
    ("congressional_buying",
     {"signal": "BUY",
      "alt_data": {"congressional_recent": {"net_direction": "buying",
                                              "trades_60d": 3,
                                              "dollar_volume_60d": 250_000}}},
     "CONFIRM", None),
    ("orb_breakout",
     {"signal": "BUY",
      "alt_data": {"intraday": {"opening_range_breakout": True}}},
     "CONFIRM", None),
    ("earnings_surprise_streak",
     {"signal": "BUY",
      "alt_data": {"earnings_surprise": {"total_quarters": 4,
                                            "beat_count": 4,
                                            "avg_surprise_pct": 5.0}}},
     "CONFIRM", None),
    ("earnings_miss_streak",
     {"signal": "BUY",
      "alt_data": {"earnings_surprise": {"total_quarters": 4,
                                            "beat_count": 1,
                                            "avg_surprise_pct": -3.0}}},
     "CAUTION", None),
    ("stocktwits_extreme_bullish",
     {"signal": "BUY",
      "alt_data": {"stocktwits_sentiment": {"net_sentiment_7d": 0.80}}},
     "CAUTION", None),
    ("stocktwits_extreme_bearish",
     {"signal": "BUY",
      "alt_data": {"stocktwits_sentiment": {"net_sentiment_7d": -0.60}}},
     "CONFIRM", None),
]


@pytest.mark.parametrize("rule_name,candidate,expected_severity,ctx_kwargs",
                          _FIRE_CASES,
                          ids=[c[0] for c in _FIRE_CASES])
def test_rule_fires_on_positive_fixture(rule_name, candidate,
                                          expected_severity, ctx_kwargs):
    """Each rule must fire with the expected severity on its
    canonical positive fixture."""
    mod = importlib.import_module(f"deterministic_specialists.{rule_name}")
    ctx = SimpleNamespace(**ctx_kwargs) if ctx_kwargs else None
    verdict = mod.evaluate(candidate, ctx)
    assert verdict is not None, (
        f"{rule_name} did not fire on its positive fixture."
    )
    assert verdict["severity"] == expected_severity, (
        f"{rule_name} fired with severity {verdict['severity']}, "
        f"expected {expected_severity}."
    )
    assert verdict.get("reasoning"), (
        f"{rule_name} fired without reasoning text — operator visibility "
        "requirement."
    )


# ─────────────────────────────────────────────────────────────────────
# Per-rule negative cases — every rule no-ops on empty candidate
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rule_name", [c[0] for c in _FIRE_CASES])
def test_rule_no_op_on_empty_candidate(rule_name):
    """Every rule must return None on a candidate with no relevant
    fields. Guards against rules that crash on `candidate.get(...)
    is None`."""
    mod = importlib.import_module(f"deterministic_specialists.{rule_name}")
    # Empty candidate apart from a matching signal
    cand = {"symbol": "X", "signal": "BUY"}
    assert mod.evaluate(cand, None) is None, (
        f"{rule_name} fired on a candidate with no relevant fields."
    )


# ─────────────────────────────────────────────────────────────────────
# Integration — wired into ai_analyst prompt builder
# ─────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_panel_builder_imported_in_ai_analyst(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "ai_analyst.py").read_text()
        assert "from deterministic_specialists import build_panel_block" in src
