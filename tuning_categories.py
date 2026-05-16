"""Categorize self-tuner adjustments — single source of truth.

Both the web layer (views.py — tuning-history badge rendering) and the
scheduler (tuning_auto_expiry.py — revert decisions) need to ask "is
this adjustment a gate-tighten?" Put the categorizer here so neither
imports the other.

The categories matter because of the 2026-05-14 incident: lumping
gate-tightens (which restrict trades) with refinements (ATR
multipliers / RSI thresholds / signal-weight intensity — which don't)
caused both me and the user to misread the system's tuning state.
Auto-expiry only triggers on `gate_tighten`; reverting refinements
would be wrong.

Categories:
  gate_tighten — restricts trade volume or scope. Watched bucket;
    accumulating gate-tightens caused the 2026-05-14 collapse.
  refinement  — changes HOW a threshold computes, not WHETHER to
    trade. ATR multipliers, RSI thresholds, Layer-2 signal-weight
    intensity (0.0-1.0).
  loosen      — explicit easing — wider opening filter, restored
    blacklist, lower confidence threshold.
  neutral     — evaluations with no change, manual rollbacks,
    phantom-stop cleanups, auto-reversals of prior changes.
"""
from __future__ import annotations


_RULES = (
    # exact-match overrides come first
    ("evaluation",              "neutral"),
    ("manual_revert",           "neutral"),
    ("auto_reversal",           "neutral"),
    ("auto_expiry_revert",      "neutral"),  # don't auto-expire an auto-expiry
    ("rollback_phantom_stop",   "neutral"),
    # signal-weight changes are Layer-2 intensity refinements, NOT gates
    ("signal_weight_down",      "refinement"),
    ("signal_weight_up",        "refinement"),
    # ATR / RSI threshold tunings are how-it-computes refinements
    ("atr_tp_tighten",          "refinement"),
    ("atr_tp_loosen",           "refinement"),
    ("atr_sl_tighten",          "refinement"),
    ("atr_sl_loosen",           "refinement"),
    ("rsi_oversold_lower",      "refinement"),
    ("rsi_oversold_raise",      "refinement"),
    ("rsi_overbought_lower",    "refinement"),
    ("rsi_overbought_raise",    "refinement"),
    ("stop_take_profit",        "refinement"),
    ("trailing_atr_multiplier", "refinement"),
)


def categorize(adjustment_type) -> str:
    """Return one of: 'gate_tighten', 'refinement', 'loosen', 'neutral'."""
    if not adjustment_type:
        return "neutral"
    at = str(adjustment_type).lower()
    for needle, category in _RULES:
        if at == needle:
            return category
    # Suffix detection for the long tail of optimizers.
    if (at.endswith("_tighten") or at.endswith("_reduce")
            or at.endswith("_raise") or at.endswith("_upward")
            or at == "strategy_deprecate" or at == "concentration_reduce"
            or at == "correlation_tighten" or at == "fast_lane_retirement"
            or at == "stop_out_blacklist"):
        return "gate_tighten"
    if at.endswith("_loosen") or at.endswith("_lower"):
        return "loosen"
    return "neutral"
