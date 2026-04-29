"""Centralized safe bounds for every autonomously-tuned parameter.

Each entry is `(min, max)` — the absolute range the tuner is allowed
to set this parameter to. Tuner functions call `clamp(name, value)`
before writing; values outside the bounds are clamped to the nearest
edge. This guarantees no autonomous adjustment can push a parameter
to a dangerous or absurd value, regardless of bugs in the detection
logic.

Bounds are absolute floors and ceilings, NOT operating ranges. The
tuner's own logic restricts day-to-day movement to small steps; these
bounds catch programming errors and prevent runaway adjustments.

When adding a new autonomously-tuned parameter:
  1. Add an entry to PARAM_BOUNDS below
  2. Wire your tuning rule to call `clamp(param_name, candidate_value)`
     before invoking `update_trading_profile`
  3. The `tests/test_every_lever_is_tuned.py` guardrail will then
     consider this parameter "covered"
"""

from __future__ import annotations

from typing import Dict, Tuple, Union

Number = Union[int, float]

# Absolute safe bounds for every tunable parameter.
# Format: param_name -> (min_value, max_value)
PARAM_BOUNDS: Dict[str, Tuple[Number, Number]] = {
    # ── AI behavior ───────────────────────────────────────────────
    "ai_confidence_threshold":       (10, 90),
    "avoid_earnings_days":           (0, 7),
    "skip_first_minutes":            (0, 30),

    # ── Sizing & risk ─────────────────────────────────────────────
    "max_position_pct":              (0.01, 0.25),
    "max_total_positions":           (3, 25),
    "max_correlation":               (0.30, 0.95),
    "max_sector_positions":          (1, 10),
    "drawdown_pause_pct":            (0.10, 0.30),
    "drawdown_reduce_pct":           (0.05, 0.15),

    # ── Exits ─────────────────────────────────────────────────────
    "stop_loss_pct":                 (0.01, 0.15),
    "take_profit_pct":               (0.02, 0.50),
    "short_stop_loss_pct":           (0.01, 0.20),
    "short_take_profit_pct":         (0.03, 0.20),
    # P1.9b of LONG_SHORT_PLAN.md — short-side sizing and time stop.
    # Floor at 1% so the tuner can never zero out shorts via clamp;
    # ceiling at 30% to mirror the long max_position_pct ceiling.
    "short_max_position_pct":        (0.01, 0.30),
    "short_max_hold_days":           (1, 30),
    "atr_multiplier_sl":             (1.0, 4.0),
    "atr_multiplier_tp":             (1.0, 5.0),
    "trailing_atr_multiplier":       (0.5, 3.0),

    # ── Entry filters ─────────────────────────────────────────────
    "min_volume":                    (100_000, 5_000_000),
    "volume_surge_multiplier":       (1.0, 5.0),
    "breakout_volume_threshold":     (0.5, 3.0),
    "gap_pct_threshold":             (1.0, 10.0),
    "momentum_5d_gain":              (1.0, 15.0),
    "momentum_20d_gain":             (1.0, 15.0),
    "rsi_overbought":                (70, 95),
    "rsi_oversold":                  (5, 30),

    # ── Price band (ratio bounds enforced separately by caller) ──
    # min_price and max_price get a hard floor / ceiling here, but
    # callers should ALSO restrict day-to-day movement to 0.5x-2.0x of
    # the current value to prevent profile-identity drift.
    "min_price":                     (0.50, 200.0),
    "max_price":                     (5.0, 1000.0),

    # ── Boolean toggles (treated as 0.0-1.0 weights in Layer 2) ──
    # These ranges are intentionally [0.0, 1.0] so the same clamp()
    # works whether the value is being treated as a binary 0/1 or a
    # graduated weight.
    "enable_short_selling":          (0.0, 1.0),
    "use_atr_stops":                 (0.0, 1.0),
    "use_trailing_stops":            (0.0, 1.0),
    "use_limit_orders":              (0.0, 1.0),
    "maga_mode":                     (0.0, 1.0),

    # Strategy toggles (legacy 4 — preserved as 0/1 booleans for now;
    # Layer 2 will turn them into graduated weights)
    "strategy_momentum_breakout":    (0.0, 1.0),
    "strategy_volume_spike":         (0.0, 1.0),
    "strategy_mean_reversion":       (0.0, 1.0),
    "strategy_gap_and_go":           (0.0, 1.0),
}


def clamp(param_name: str, value: Number) -> Number:
    """Clamp `value` to the safe bounds for `param_name`.

    If `param_name` is unknown (no bounds entry), returns the value
    unchanged. Returns the same numeric type that came in (int stays
    int, float stays float) when bounds allow.
    """
    bounds = PARAM_BOUNDS.get(param_name)
    if bounds is None:
        return value
    lo, hi = bounds
    clamped = max(lo, min(hi, value))
    # Preserve int-ness if the input was int and the bounds are int.
    if isinstance(value, int) and isinstance(lo, int) and isinstance(hi, int):
        return int(round(clamped))
    return clamped


def is_bounded(param_name: str) -> bool:
    """Whether this parameter has explicit bounds defined."""
    return param_name in PARAM_BOUNDS


def get_bounds(param_name: str) -> Tuple[Number, Number]:
    """Raise KeyError if param has no bounds entry."""
    return PARAM_BOUNDS[param_name]
