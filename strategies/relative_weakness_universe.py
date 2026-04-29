"""relative_weakness_universe — short the worst-relative-strength names.

The "anti-momentum" trade. Academic momentum literature (Jegadeesh &
Titman 1993) is symmetric: top-decile winners outperform, bottom-decile
losers underperform. Real long/short funds run this both ways.

Why this exists separately from `relative_weakness_in_strong_sector`:
that strategy requires a SPECIFIC structural setup (sector strong but
stock lagging). This one is universe-wide and triggers in any regime —
which is critical for dedicated short profiles (target_short_pct ≥ 0.4)
that need to fill a substantial short book even when textbook bearish
technical patterns are rare (e.g., extended bull markets).

Detection:
  - Compute 20-day return for each name in the universe vs SPY (the
    structural weakness signal) AND 5-day return vs SPY (the CURRENT
    weakness signal).
  - Filters:
    * 20d RS gap ≥ RS_GAP_THRESHOLD (5%): cumulative underperformance
    * 5d RS gap ≥ RECENT_RS_GAP_THRESHOLD (1%): weakness is CURRENT,
      not stale. Stops the strategy from picking names that crashed
      months ago and have been quietly recovering since.
    * Stock below 20-day MA (trend confirmation)
    * Stock NOT down more than DRAWDOWN_FILTER_PCT (40%) from 252-day
      high — avoids the "bottom-pickers' graveyard" of names that
      already crashed and are more likely to bounce than continue
      lower (real short profit comes from names with further to fall,
      not names that already fell).
  - Rank ascending by 5d return (most negative first); emit bottom
    WEAKNESS_FRACTION (cap 5).

Score is 1 (vs 2 for the focused setups) because there's no specific
bearish catalyst — it's purely relative weakness. The AI sees this
context and weights accordingly.

Markets: equities only. Crypto's universe is too small for ranking.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "relative_weakness_universe"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]

# Tunable constants — kept module-level so self_tuning can adjust later
# if needed. Conservative defaults: bottom 5% AND a 5%+ underperformance
# gap, so a small universe (20 names) returns ~1 candidate.
WEAKNESS_FRACTION = 0.05
RS_GAP_THRESHOLD = 5.0          # percent (cumulative 20d underperformance vs SPY)
RECENT_RS_GAP_THRESHOLD = 1.0   # percent (5d underperformance — "weak NOW")
DRAWDOWN_FILTER_PCT = 40.0      # max 1y drawdown — past this, name is too "crashed"
LOOKBACK_DAYS = 20
RECENT_LOOKBACK_DAYS = 5
DRAWDOWN_LOOKBACK_DAYS = 252


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    if not universe or len(universe) < 5:
        return []

    # Compute SPY 20d AND 5d returns once per scan.
    needed_bars = max(LOOKBACK_DAYS, RECENT_LOOKBACK_DAYS) + 5
    spy_ret_20d = None
    spy_ret_5d = None
    try:
        spy_df = get_bars("SPY", limit=needed_bars)
        if (spy_df is not None and len(spy_df) >= LOOKBACK_DAYS + 1):
            close_now = float(spy_df["close"].iloc[-1])
            close_20 = float(spy_df["close"].iloc[-(LOOKBACK_DAYS + 1)])
            close_5 = float(spy_df["close"].iloc[-(RECENT_LOOKBACK_DAYS + 1)])
            if close_20 > 0 and close_5 > 0:
                spy_ret_20d = (close_now - close_20) / close_20 * 100
                spy_ret_5d = (close_now - close_5) / close_5 * 100
    except Exception:
        return []
    if spy_ret_20d is None or spy_ret_5d is None:
        return []

    # Score every symbol against the 20d/5d RS + drawdown filters.
    scored: List[Dict[str, Any]] = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=DRAWDOWN_LOOKBACK_DAYS + 5)
            if df is None or len(df) < LOOKBACK_DAYS + 1:
                continue
            close_now = float(df["close"].iloc[-1])
            close_20 = float(df["close"].iloc[-(LOOKBACK_DAYS + 1)])
            close_5 = float(df["close"].iloc[-(RECENT_LOOKBACK_DAYS + 1)])
            if close_20 <= 0 or close_5 <= 0:
                continue

            stock_ret_20d = (close_now - close_20) / close_20 * 100
            stock_ret_5d = (close_now - close_5) / close_5 * 100
            rs_gap_20d = spy_ret_20d - stock_ret_20d
            rs_gap_5d = spy_ret_5d - stock_ret_5d

            # Cumulative weakness vs market.
            if rs_gap_20d < RS_GAP_THRESHOLD:
                continue
            # Recent weakness — confirms the underperformance is CURRENT,
            # not just a leftover from old crashes the stock has been
            # quietly recovering from.
            if rs_gap_5d < RECENT_RS_GAP_THRESHOLD:
                continue
            # Trend confirmation: below 20-day MA.
            sma20 = df["close"].iloc[-(LOOKBACK_DAYS + 1):-1].astype(float).mean()
            if close_now >= sma20:
                continue
            # Drawdown filter — skip names that have already crashed too
            # far (they're more likely to bounce than continue lower).
            # Use 252d high if we have enough history; else skip the
            # filter rather than fall back to a shorter window that
            # would be too lenient.
            if len(df) >= DRAWDOWN_LOOKBACK_DAYS + 1:
                hi_252 = float(df["high"].iloc[-(DRAWDOWN_LOOKBACK_DAYS + 1):].max())
                if hi_252 > 0:
                    drawdown_pct = (hi_252 - close_now) / hi_252 * 100
                    if drawdown_pct > DRAWDOWN_FILTER_PCT:
                        continue
            scored.append({
                "symbol": symbol,
                "rs_gap_20d": rs_gap_20d,
                "rs_gap_5d": rs_gap_5d,
                "stock_ret_20d": stock_ret_20d,
                "stock_ret_5d": stock_ret_5d,
                "spy_ret_20d": spy_ret_20d,
                "close_now": close_now,
            })
        except Exception:
            continue

    if not scored:
        return []

    # Rank ascending by recent (5d) return — surface CURRENT weakness
    # over historical weakness.
    scored.sort(key=lambda x: x["stock_ret_5d"])
    n_emit = max(1, int(len(scored) * WEAKNESS_FRACTION))
    n_emit = min(n_emit, 5)  # absolute cap — never flood the shortlist

    out = []
    for s in scored[:n_emit]:
        out.append({
            "symbol": s["symbol"],
            "signal": "SHORT",
            "score": 1,
            "votes": {NAME: "SHORT"},
            "price": s["close_now"],
            "reason": (
                f"Current weakness vs SPY: 5d {s['stock_ret_5d']:+.1f}% "
                f"(gap {s['rs_gap_5d']:.1f}%) AND 20d {s['stock_ret_20d']:+.1f}% "
                f"vs SPY {s['spy_ret_20d']:+.1f}% (gap {s['rs_gap_20d']:.1f}%). "
                f"Below 20d MA, not a deep-drawdown bounce candidate."
            ),
        })
    return out
