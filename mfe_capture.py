"""MFE capture ratio — how much of available favorable excursion the
system actually realizes.

Fix 1 of the asymmetric-edge trio (Fix 1 + Fix 3 + INTRADAY_STOPS_PLAN
Stages 1-3). For each closed trade with a recorded max-favorable-
excursion, compute:

    capture_ratio = realized_pnl_pct / mfe_pct

A capture ratio of 1.0 means we exited at or near the high water — full
realization of the move. 0.0 means we exited at break-even or worse
despite the position having moved favorably during its life. Negative
means we lost despite a favorable excursion (worst case — the IBM
$2.70 win on a $1500 unrealized winner had ~0.001 capture).

Surfaced to:
  - Performance dashboard: "Capture: 12% — leaving money on the table"
  - AI prompt: tells the AI when current exit logic is materially
    underperforming the underlying signals.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Need at least this many closed trades with MFE data to compute a
# meaningful capture ratio. Below this, the average is too noisy.
MIN_TRADES_FOR_CAPTURE = 10

# Capture below this ratio is "leaving money on the table" — surfaced
# as a warning in the prompt block.
LOW_CAPTURE_THRESHOLD = 0.30


def compute_capture_ratio(db_path: str, lookback: int = 50) -> Optional[Dict[str, Any]]:
    """Compute the average capture ratio across the most-recent closed
    trades that have an MFE recording.

    Schema reality: MFE lives on the BUY entry row (updated each cycle
    while the position is open). Realized P&L lives on the SELL exit
    row that's inserted when the position closes. To compute capture
    we self-join on symbol — for each SELL with pnl, find the most
    recent prior BUY for that symbol with an MFE, use its entry price
    and high-water mark.

    Args:
      db_path: profile journal DB
      lookback: how many recent SELL rows to average over

    Returns:
      Dict with avg_capture_ratio, median_capture_ratio, n_trades,
      n_negative_capture (trades that lost despite an MFE > entry —
      the most damaging pattern). None on insufficient data.
    """
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(db_path)
        # Pull recent SELL rows (the exits, where pnl lives).
        sells = conn.execute(
            "SELECT id, timestamp, symbol, qty, price, pnl "
            "FROM trades "
            "WHERE pnl IS NOT NULL "
            "AND side IN ('sell', 'cover') "
            "AND qty > 0 AND price > 0 "
            "ORDER BY id DESC LIMIT ?",
            (lookback,),
        ).fetchall()
        if not sells:
            conn.close()
            return None
        # For each SELL, find the most recent prior BUY for that symbol
        # with an MFE recorded. The BUY's price is the entry; its MFE
        # is the highest favorable price during the position's life.
        captures = []
        n_negative = 0
        for sid, ts, sym, sell_qty, sell_px, pnl in sells:
            buy = conn.execute(
                "SELECT price, max_favorable_excursion FROM trades "
                "WHERE symbol = ? AND side = 'buy' "
                "AND timestamp < ? "
                "AND max_favorable_excursion IS NOT NULL "
                "AND max_favorable_excursion > 0 "
                "ORDER BY id DESC LIMIT 1",
                (sym, ts),
            ).fetchone()
            if not buy:
                continue
            entry_price, mfe = buy
            if not entry_price or entry_price <= 0 or not mfe or mfe <= entry_price:
                # No favorable excursion (price never went above entry).
                # Capture is undefined — exclude.
                continue
            notional = abs(sell_qty * entry_price)
            if notional <= 0:
                continue
            realized_pct = (pnl / notional) * 100.0
            mfe_pct = ((float(mfe) - entry_price) / entry_price) * 100.0
            capture = realized_pct / mfe_pct
            captures.append(capture)
            if capture < 0:
                n_negative += 1
        conn.close()
    except Exception as exc:
        logger.debug("compute_capture_ratio query failed: %s", exc)
        return None

    if len(captures) < MIN_TRADES_FOR_CAPTURE:
        return None

    avg = sum(captures) / len(captures)
    sorted_caps = sorted(captures)
    median = sorted_caps[len(sorted_caps) // 2]

    return {
        "avg_capture_ratio": round(avg, 4),
        "median_capture_ratio": round(median, 4),
        "n_trades": len(captures),
        "n_negative_capture": n_negative,
    }


def render_for_prompt(capture: Optional[Dict[str, Any]]) -> str:
    """Format the capture ratio as an AI prompt block.

    Suppress when there's no signal (None) or when capture is high
    enough that the AI doesn't need to be told (>= 0.50). The point
    of the block is to flag asymmetric edge, not noise the prompt
    when things are working.
    """
    if not capture:
        return ""
    avg = capture.get("avg_capture_ratio") or 0
    if avg >= 0.50:
        return ""
    n = capture.get("n_trades") or 0
    n_neg = capture.get("n_negative_capture") or 0
    pct = avg * 100
    block = (
        f"\nMFE CAPTURE: {pct:.0f}% over last {n} trades "
        f"(realized P&L as fraction of available favorable excursion)\n"
    )
    if avg < LOW_CAPTURE_THRESHOLD:
        block += (
            f"  → Exit logic is leaving substantial money on the table. "
            f"Trades that ran favorably are giving back most of the "
            f"unrealized gain before exit fires. Consider: tighter "
            f"trailing stops, scale-out at intermediate targets, or "
            f"earlier take-profits.\n"
        )
    if n_neg > 0:
        block += (
            f"  → {n_neg} of these trades LOST money despite running "
            f"favorably during their life — the most damaging pattern. "
            f"These are positions that ran up then collapsed past entry.\n"
        )
    return block
