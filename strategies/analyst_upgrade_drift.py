"""analyst_upgrade_drift — follow the 2–3 day drift after a recent upgrade.

Sell-side analyst revisions are followed by predictable short-term drift
in the direction of the revision (Womack 1996, Jegadeesh et al. 2004).
The effect lives roughly 2–5 trading days before being absorbed.

We use yfinance's `recommendations` property as a proxy for the most
recent rating change and trigger when a fresh upgrade or downgrade
shows up alongside supporting price action.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "analyst_upgrade_drift"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    import yfinance as yf
    from market_data import get_bars

    import datetime as _dt
    now = _dt.datetime.utcnow()

    out = []
    for symbol in universe:
        try:
            recs = yf.Ticker(symbol).recommendations
            if recs is None or len(recs) == 0:
                continue

            # Find the most recent action row (yfinance schema varies by
            # version — `To Grade` / `From Grade` columns are the stable
            # ones across versions).
            latest = recs.tail(1).iloc[0]
            to_grade = str(latest.get("To Grade", "")).lower()
            from_grade = str(latest.get("From Grade", "")).lower()

            # Date column is the index in most yfinance versions
            try:
                last_change = recs.index[-1]
                if hasattr(last_change, "to_pydatetime"):
                    last_change = last_change.to_pydatetime()
                days_since = (now - last_change.replace(tzinfo=None)).days
            except Exception:
                days_since = 999
            if days_since > 5:
                continue

            upgrade_terms = ("buy", "strong buy", "outperform", "overweight",
                             "accumulate", "positive")
            downgrade_terms = ("sell", "strong sell", "underperform",
                               "underweight", "reduce", "negative")

            is_upgrade = any(t in to_grade for t in upgrade_terms) and \
                         not any(t in from_grade for t in upgrade_terms)
            is_downgrade = any(t in to_grade for t in downgrade_terms) and \
                           not any(t in from_grade for t in downgrade_terms)
            if not (is_upgrade or is_downgrade):
                continue

            df = get_bars(symbol, limit=5)
            if df is None or len(df) < 2:
                continue
            price = float(df["close"].iloc[-1])
            prior = float(df["close"].iloc[-2])
            move_pct = (price - prior) / prior * 100 if prior > 0 else 0

            # Require price to confirm the revision direction
            if is_upgrade and move_pct < 0:
                continue
            if is_downgrade and move_pct > 0:
                continue

            signal = "BUY" if is_upgrade else "SELL"
            out.append({
                "symbol": symbol,
                "signal": signal,
                "score": 1,
                "votes": {NAME: signal},
                "price": price,
                "reason": (
                    f"Analyst revision: {from_grade or '?'} → {to_grade} "
                    f"{days_since}d ago, price confirming ({move_pct:+.1f}%)"
                ),
            })
        except Exception:
            continue
    return out
