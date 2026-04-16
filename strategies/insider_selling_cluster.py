"""insider_selling_cluster — bearish mirror of insider_cluster.

When multiple insiders sell in size over a short window, the stock
tends to underperform over the following quarter (Seyhun 1986). We
trigger when 3+ insider sells total >=$500K and dominate any recent
buying — this filters out routine diversification and tax-loss activity.

Crypto is excluded (no insider data).
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "insider_selling_cluster"
APPLICABLE_MARKETS = ["micro", "small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from alternative_data import get_insider_activity
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            insider = get_insider_activity(symbol) or {}
            sells = int(insider.get("recent_sells", 0) or 0)
            buys = int(insider.get("recent_buys", 0) or 0)
            sell_value = float(insider.get("total_sell_value", 0) or 0)

            # Cluster trigger: 3+ sells with material capital, dominating buys
            if sells < 3 or sell_value < 500_000 or sells <= buys * 1.5:
                continue

            df = get_bars(symbol, limit=5)
            if df is None or len(df) < 1:
                continue
            price = float(df["close"].iloc[-1])

            out.append({
                "symbol": symbol,
                "signal": "SELL",
                "score": 2,
                "votes": {NAME: "SELL"},
                "price": price,
                "reason": (
                    f"Insider selling cluster: {sells} sells totaling "
                    f"${sell_value:,.0f} (vs {buys} buys)"
                ),
            })
        except Exception:
            continue
    return out
