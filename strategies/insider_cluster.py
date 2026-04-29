"""insider_cluster — flag stocks where insiders are buying in size.

Insider buying clusters are among the strongest predictive signals in
finance. When 3+ insiders buy in the same window, the stock typically
outperforms over the following quarter.

Crypto is excluded (insider data doesn't apply).
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "insider_cluster"
APPLICABLE_MARKETS = ["micro", "small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    """Return symbols where insider activity is meaningfully bullish."""
    from alternative_data import get_insider_activity

    out = []
    for symbol in universe:
        try:
            insider = get_insider_activity(symbol)
            buys = insider.get("recent_buys", 0)
            sells = insider.get("recent_sells", 0)
            buy_value = insider.get("total_buy_value", 0)

            # Cluster trigger: 3+ buys with material capital, dominating sells
            if buys >= 3 and buy_value >= 250_000 and buys > sells * 1.5:
                out.append({
                    "symbol": symbol,
                    "signal": "BUY",
                    # P3.5 of LONG_SHORT_PLAN.md — promoted from 2 to 3.
                    # Insider clusters have documented edge (Seyhun 1986,
                    # Cohen et al. 2012). The higher score lifts these
                    # signals into the AI's top-15 shortlist reliably,
                    # which they were previously losing to noisier
                    # technical signals at score 2.
                    "score": 3,
                    "votes": {"insider_cluster": "BUY"},
                    "reason": (
                        f"Insider cluster: {buys} buys totaling "
                        f"${buy_value:,.0f} (vs {sells} sells)"
                    ),
                })
        except Exception:
            continue
    return out
