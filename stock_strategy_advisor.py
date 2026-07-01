"""Mirror of options_strategy_advisor for stock-action recommendations.

For every candidate the screener flags, this module produces a
fully-specified stock trade recommendation (action / size / stop /
take-profit / rationale) so the AI prompt can present stock setups
WITH THE SAME RIGOR as the pre-built multi-leg options strategies.

Why this exists.
The 2026-05-14 audit found that since the multileg pipeline shipped
2026-05-06, the AI had been picking MULTILEG_OPEN over BUY on
essentially every actionable candidate. The root cause was prompt
asymmetry: options had a fully-analyzed recommendation block
(strategy, strikes, expiry, rationale) while stocks were a bare
indicator dump for the AI to figure out from scratch.

Mack: "stocks and options are not in competition with each other —
they are two different opportunities; we should take the best
candidates from both and determine action."

This module makes the symmetry real: same level of pre-computed
analysis for both. The AI sees parallel STOCK ACTION RECOMMENDATIONS
and MULTI-LEG OPTIONS STRATEGIES blocks and picks based on quality
of setup, not on which side has more pre-built work.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def evaluate_candidate_for_stock_action(
    candidate: Dict[str, Any],
    ctx: Any = None,
) -> List[Dict[str, Any]]:
    """For a screener candidate, return stock-action recommendations
    (BUY or SHORT) with sizing and stop/TP pre-computed.

    Returns at most one recommendation per candidate (the dominant
    direction from the strategy ensemble). Empty list when the
    candidate has no actionable directional signal (HOLD or invalid).

    Sizing: max_position_pct scaled by conviction (abs score capped).
    Stops/targets: ATR-based when ATR is available; otherwise
    falls back to ctx defaults.
    """
    symbol = candidate.get("symbol")
    signal = (candidate.get("signal") or "").upper()
    price = float(candidate.get("price") or 0)

    if not symbol or price <= 0:
        return []

    # Map signal to executable action.
    if "BUY" in signal:
        action = "BUY"
    elif "SHORT" in signal:
        action = "SHORT"
    elif "SELL" in signal:
        # SELL on a non-held symbol is a SHORT signal in the
        # ensemble vocabulary; the trade pipeline gates whether
        # the profile can actually short.
        action = "SHORT"
    else:
        return []

    # Conviction-based sizing. score is the merged-strategy score
    # from multi_strategy.aggregate_candidates: |score| ≥ 2 →
    # STRONG_BUY/SELL territory, |score| == 1 → moderate. Scale
    # max_position_pct linearly from 0.5x at |score|=1 to 1.0x at
    # |score|≥2. This mirrors the conviction-derived sizing the
    # trade pipeline applies during execution.
    base_size = float(getattr(ctx, "max_position_pct", 0.08) or 0.08)
    if action == "SHORT":
        # Asymmetric short risk — halve the long-side max.
        # Matches the rule documented in ai_analyst.py prompt RULES.
        short_max = getattr(ctx, "short_max_position_pct", None)
        if short_max is not None:
            base_size = float(short_max)
        else:
            base_size = base_size * 0.5
    score = float(candidate.get("score") or 0)
    conviction = min(1.0, max(0.5, abs(score) / 2.0))
    size_pct = base_size * conviction

    # ATR-based stop / take-profit. ATR is in price units; convert
    # to a percentage of entry price, then CLAMP to a sensible band.
    # See risk_clamps.py for the rationale: raw ATR-as-percent
    # explodes for low-priced volatile stocks (RGNT 84% TP, NEXR 63%
    # SL) and collapses to near-zero when ATR is stale (RGNT 0.3%
    # stop). Clamping addresses both at the source of truth.
    atr = float(candidate.get("atr") or 0)
    atr_mult_sl = float(getattr(ctx, "atr_multiplier_sl", 2.0) or 2.0)
    atr_mult_tp = float(getattr(ctx, "atr_multiplier_tp", 3.0) or 3.0)
    if atr > 0 and price > 0:
        from risk_clamps import clamp_tp_pct, clamp_sl_pct
        raw_sl_frac = atr * atr_mult_sl / price
        raw_tp_frac = atr * atr_mult_tp / price
        stop_loss_pct = round(clamp_sl_pct(raw_sl_frac) * 100, 1)
        take_profit_pct = round(clamp_tp_pct(raw_tp_frac) * 100, 1)
    else:
        stop_loss_pct = float(getattr(ctx, "stop_loss_pct", 3.0) or 3.0)
        take_profit_pct = float(
            getattr(ctx, "take_profit_pct", 6.0) or 6.0
        )

    # Confidence: strategy-level conviction normalized to 0-100.
    # The AI's own confidence (the value it returns in the JSON)
    # may be different — this is the strategy ensemble's prior.
    confidence = int(round(min(100, max(0, abs(score) * 33))))

    # Compact rationale that names the technicals driving the call.
    rsi = float(candidate.get("rsi") or 0)
    adx = float(candidate.get("adx") or 0)
    vol_ratio = float(candidate.get("volume_ratio") or 1.0)
    rationale = (
        f"{action} {symbol} (ensemble score={score:+.1f}). "
        f"RSI {rsi:.0f}, ADX {adx:.0f}, vol {vol_ratio:.1f}x. "
        f"Size {size_pct*100:.1f}% equity, "
        f"ATR-stop -{stop_loss_pct:.1f}%, "
        f"ATR-target +{take_profit_pct:.1f}%."
    )

    return [
        {
            "action": action,
            "symbol": symbol,
            "size_pct": round(size_pct * 100, 1),
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "confidence": confidence,
            "rationale": rationale,
        }
    ]


# NOTE (2026-07-01, selection-engine P2b): the standalone
# render_stock_recs_for_prompt block was removed. Candidate stock
# recommendations are now scored on the risk-adjusted axis and rendered
# INTERLEAVED with option recs by `opportunity_ledger.render_opportunity_ledger`
# (which calls `evaluate_candidate_for_stock_action` above). The single ranked
# ledger is a STRONGER anti-asymmetry guarantee than two equal-length blocks:
# both expressions now compete on one number. See docs/SELECTION_ENGINE_DESIGN.md.
