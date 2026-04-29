"""catalyst_filing_short — short on adverse SEC filings + price weakness.

P3.2 of LONG_SHORT_PLAN.md. The strongest catalyst-driven shorts
come from material company-specific events disclosed in SEC
filings — going-concern warnings, material weaknesses, and
high-severity adverse changes. These predict 6-12 month
underperformance with statistical significance (Beneish 1999;
Dechow et al. 2011).

The system already maintains `sec_filings_history` per profile DB
(populated by the SEC analysis task). This strategy reads that
table — no API calls in the hot path. The strategy fires when:

  1. There's a filing in the last 30 days with one of:
     - going_concern_flag = 1
     - material_weakness_flag = 1
     - alert_severity = 'high' AND alert_signal = 'concerning'
  2. Stock has weakened since the filing (current close < pre-
     filing close × 0.97 — confirms the market is reacting,
     not ignoring).
  3. We're within 30 trading days of the filing (continuation
     window — beyond that the catalyst is mostly priced in).

Score: 3 (high-conviction catalyst short). Tagged in
_CATALYST_SHORT_STRATEGIES so it survives the strong-bull
regime gate — these events override market drift.

Markets: equities only. Crypto has no comparable filings system.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List


NAME = "catalyst_filing_short"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    db_path = getattr(ctx, "db_path", None) if ctx is not None else None
    if not db_path:
        return []

    cutoff_date = (datetime.utcnow() - timedelta(days=30)).date().isoformat()
    universe_set = {s.upper() for s in universe} if universe else set()

    out = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Pull every adverse filing in the last 30 days for any symbol
        # we care about. The table is small enough on most profile DBs
        # that we don't need a per-symbol prefilter.
        rows = conn.execute(
            "SELECT symbol, form_type, filed_date, alert_severity, "
            "alert_signal, going_concern_flag, material_weakness_flag, "
            "alert_summary "
            "FROM sec_filings_history "
            "WHERE filed_date >= ? "
            "AND ("
            "  going_concern_flag = 1 "
            "  OR material_weakness_flag = 1 "
            "  OR (alert_severity = 'high' AND alert_signal = 'concerning')"
            ") "
            "ORDER BY filed_date DESC",
            (cutoff_date,),
        ).fetchall()
        conn.close()
    except Exception:
        return []  # table missing or other error — graceful degrade

    for r in rows:
        sym = (r["symbol"] or "").upper()
        if not sym:
            continue
        # If a universe was passed, restrict to it; otherwise scan all.
        if universe_set and sym not in universe_set:
            continue

        try:
            bars = get_bars(sym, limit=45)
            if bars is None or len(bars) < 5:
                continue
            close_now = float(bars["close"].iloc[-1])

            # Price-action confirmation: current close should be at
            # least 3% below the filing-day close. Use the closest bar
            # at or before filed_date as the reference.
            filed_date = r["filed_date"]
            try:
                filing_dt = datetime.strptime(filed_date, "%Y-%m-%d").date()
            except Exception:
                continue

            # Find the bar closest to filing date
            ref_close = None
            try:
                if "timestamp" in bars.columns:
                    ts_col = "timestamp"
                elif "date" in bars.columns:
                    ts_col = "date"
                else:
                    ts_col = None
                if ts_col:
                    matching = bars[bars[ts_col].astype(str).str[:10]
                                     <= filing_dt.isoformat()]
                    if len(matching) > 0:
                        ref_close = float(matching["close"].iloc[-1])
            except Exception:
                pass
            if ref_close is None:
                # Fall back to "5 bars ago" as a rough proxy
                ref_close = float(bars["close"].iloc[-5]) if len(bars) >= 5 else None
            if ref_close is None or ref_close <= 0:
                continue

            move_since_filing_pct = (close_now - ref_close) / ref_close * 100
            if move_since_filing_pct > -3.0:
                # Price hasn't reacted (or recovered) — pattern not active
                continue

            # Determine the most severe flag for the reason text
            flags = []
            if r["going_concern_flag"]:
                flags.append("going-concern")
            if r["material_weakness_flag"]:
                flags.append("material-weakness")
            if (r["alert_severity"] == "high"
                    and r["alert_signal"] == "concerning"):
                flags.append("high-severity adverse change")
            flag_str = " + ".join(flags) or "concerning"

            out.append({
                "symbol": sym,
                "signal": "SHORT",
                "score": 3,  # high-conviction catalyst
                "votes": {NAME: "SHORT"},
                "price": close_now,
                "reason": (
                    f"SEC filing catalyst ({r['form_type']} on "
                    f"{filed_date}): {flag_str}; "
                    f"price {move_since_filing_pct:+.1f}% since filing "
                    f"(${ref_close:.2f}→${close_now:.2f})"
                ),
            })
        except Exception:
            continue

    return out
