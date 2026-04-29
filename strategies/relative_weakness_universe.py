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
  - Compute 20-day return for each name in the universe vs SPY.
  - Rank by this gap (RS vs market). Lowest = weakest.
  - Emit bottom WEAKNESS_FRACTION as SHORT candidates with score=1.
  - Trend confirmation: stock must be below its 20-day MA (filter out
    names that are weak on a single day spike).
  - Min RS gap: stock must be at least RS_GAP_THRESHOLD percent below
    SPY (0.5% over 1 day is noise; 5%+ over 20 days is a real trend).

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
RS_GAP_THRESHOLD = 5.0  # percent (cumulative underperformance vs SPY)
LOOKBACK_DAYS = 20


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    if not universe or len(universe) < 5:
        return []

    # Compute SPY return once per scan.
    spy_ret = None
    try:
        spy_df = get_bars("SPY", limit=LOOKBACK_DAYS + 5)
        if (spy_df is not None and len(spy_df) >= LOOKBACK_DAYS + 1
                and float(spy_df["close"].iloc[-(LOOKBACK_DAYS + 1)]) > 0):
            close_back = float(spy_df["close"].iloc[-(LOOKBACK_DAYS + 1)])
            close_now = float(spy_df["close"].iloc[-1])
            spy_ret = (close_now - close_back) / close_back * 100
    except Exception:
        return []
    if spy_ret is None:
        return []

    # Score every symbol with its RS gap.
    scored: List[Dict[str, Any]] = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=LOOKBACK_DAYS + 5)
            if df is None or len(df) < LOOKBACK_DAYS + 1:
                continue
            close_back = float(df["close"].iloc[-(LOOKBACK_DAYS + 1)])
            close_now = float(df["close"].iloc[-1])
            if close_back <= 0:
                continue
            stock_ret = (close_now - close_back) / close_back * 100
            rs_gap = spy_ret - stock_ret  # positive = stock lagging SPY
            if rs_gap < RS_GAP_THRESHOLD:
                continue
            # Trend confirmation: below 20-day MA. Filters out single-day
            # noise where a stock dropped today after weeks of strength.
            sma20 = df["close"].iloc[-(LOOKBACK_DAYS + 1):-1].astype(float).mean()
            if close_now >= sma20:
                continue
            scored.append({
                "symbol": symbol,
                "rs_gap": rs_gap,
                "stock_ret": stock_ret,
                "spy_ret": spy_ret,
                "close_now": close_now,
            })
        except Exception:
            continue

    if not scored:
        return []

    # Rank ascending by stock_ret (most negative first = most relatively weak).
    scored.sort(key=lambda x: x["stock_ret"])
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
                f"Relative weakness vs SPY over {LOOKBACK_DAYS}d: "
                f"stock {s['stock_ret']:+.1f}% vs SPY {s['spy_ret']:+.1f}% "
                f"(gap {s['rs_gap']:.1f}%). Below 20d MA — confirmed downtrend."
            ),
        })
    return out
