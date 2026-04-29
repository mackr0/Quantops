"""iv_regime_short — short into elevated-IV downtrends.

P3.4 of LONG_SHORT_PLAN.md. Different thesis from
`high_iv_rank_fade` (mean-reversion). This is a CONTINUATION
strategy: when implied volatility is elevated AND the stock is
in an established downtrend with active selling pressure, the
combination of priced-in fear + technical breakdown predicts
multi-day continuation lower.

Why elevated IV matters for shorts: the options market is signaling
material uncertainty about the name, and that uncertainty almost
never resolves to the upside on a stock already breaking down.
Prices that "look cheap" with elevated IV usually keep getting
cheaper before stabilizing.

Detection (all must hold):

  1. IV rank ≥ 70 (elevated but not extreme — extremes mean-revert
     the other way, see high_iv_rank_fade for that pattern).
  2. Stock below 20-day SMA (downtrend).
  3. Stock down ≥3% over the trailing 10 trading days (active
     selling pressure, not just sideways below SMA).
  4. RSI between 35-65 (NOT oversold — oversold is mean-reversion
     territory; this strategy fires in the meat of a downtrend).
  5. Volume on most-recent day at ≥1.2× the 20d average (active
     distribution confirmation).

Score: 2 (medium-conviction; structural pattern with technical
confirmation). NOT tagged as catalyst — IV regime is a market
condition, not a company-specific event.

Markets: equities only — IV rank is options-driven and crypto
options are markedly different.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "iv_regime_short"
APPLICABLE_MARKETS = ["midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars, add_indicators
    from options_oracle import get_options_oracle

    out = []
    for symbol in universe:
        try:
            oracle = get_options_oracle(symbol) or {}
            iv_rank = oracle.get("iv_rank")
            if iv_rank is None or iv_rank < 70:
                continue

            df = get_bars(symbol, limit=30)
            if df is None or len(df) < 22:
                continue
            if "rsi" not in df.columns:
                df = add_indicators(df)

            close_now = float(df["close"].iloc[-1])
            close_10ago = float(df["close"].iloc[-11])
            sma_20 = float(df["close"].iloc[-20:].astype(float).mean())

            # Downtrend confirmation
            if close_now >= sma_20:
                continue

            move_10d_pct = (close_now - close_10ago) / close_10ago * 100
            if move_10d_pct >= -3.0:
                continue  # not enough active selling

            # RSI sweet spot — middle of a downtrend, not oversold
            rsi_val = df["rsi"].iloc[-1]
            if rsi_val is None:
                continue
            rsi = float(rsi_val)
            if rsi < 35 or rsi > 65:
                continue

            # Volume confirmation on the latest bar
            avg_vol = float(df["volume"].iloc[-21:-1].astype(float).mean())
            vol_now = float(df["volume"].iloc[-1])
            if avg_vol <= 0 or vol_now < avg_vol * 1.2:
                continue

            out.append({
                "symbol": symbol,
                "signal": "SHORT",
                "score": 2,
                "votes": {NAME: "SHORT"},
                "price": close_now,
                "reason": (
                    f"Elevated-IV downtrend: IV rank {iv_rank:.0f}, "
                    f"price {move_10d_pct:+.1f}% over 10d, "
                    f"below 20d SMA, RSI {rsi:.0f}, "
                    f"{vol_now/avg_vol:.1f}× avg volume"
                ),
            })
        except Exception:
            continue
    return out
