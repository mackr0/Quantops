"""market_engine — wraps the existing market-specific strategy router.

This is the legacy single-strategy behavior, exposed through the new
strategy registry interface. Preserves backward compatibility while
the new strategies (insider_cluster, earnings_drift, etc.) come online.

Phase 6 of the Quant Fund Evolution roadmap (see ROADMAP.md).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


NAME = "market_engine"
APPLICABLE_MARKETS = ["*"]   # works in every market type


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    """Run the per-market strategy engine across the universe.

    Each candidate dict carries the standard signal contract:
      symbol, signal, score, votes, reason, price (when available)
    """
    from strategy_router import run_strategy

    market_type = getattr(ctx, "segment", "small")

    strategy_params = {
        "rsi_oversold": getattr(ctx, "rsi_oversold", 25.0),
        "rsi_overbought": getattr(ctx, "rsi_overbought", 85.0),
        "volume_surge_multiplier": getattr(ctx, "volume_surge_multiplier", 2.0),
        "breakout_volume_threshold": getattr(ctx, "breakout_volume_threshold", 1.0),
        "momentum_5d_gain": getattr(ctx, "momentum_5d_gain", 3.0),
        "momentum_20d_gain": getattr(ctx, "momentum_20d_gain", 5.0),
        "gap_pct_threshold": getattr(ctx, "gap_pct_threshold", 3.0),
        "strategy_momentum_breakout": getattr(ctx, "strategy_momentum_breakout", True),
        "strategy_volume_spike": getattr(ctx, "strategy_volume_spike", True),
        "strategy_mean_reversion": getattr(ctx, "strategy_mean_reversion", True),
        "strategy_gap_and_go": getattr(ctx, "strategy_gap_and_go", True),
    }

    out = []
    for symbol in universe:
        try:
            signal = run_strategy(symbol, market_type, ctx=ctx, params=strategy_params)
            # Only keep non-HOLD signals
            if signal.get("signal", "HOLD") != "HOLD":
                out.append(signal)
        except Exception:
            continue
    return out
