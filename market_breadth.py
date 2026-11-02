"""Market breadth from our own universe bars (2026-11-02, docs/25 D12).

The screener fetches daily bars for every symbol in the universe each
cycle (`screener._get_bars_for_symbols`). Those bars already say how
broad the tape is; nobody was looking. `record_bars` is called from
that loop with each symbol's frame; `snapshot` summarizes the symbols
seen in the last RECENT_SEC into the market-wide numbers the prompt,
the rules and the feature payload read:

  pct_above_sma20 / pct_above_sma50  share of symbols closing above
                                     their own 20- / 50-day mean
  advancers / decliners / adv_dec    today's close vs yesterday's
  n                                  symbols in the snapshot

No external source, no cost. A snapshot with fewer than MIN_SYMBOLS is
reported as has_data=False — a thin sample is not breadth.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

_LOCK = threading.Lock()
_STATE: Dict[str, Dict[str, Any]] = {}

RECENT_SEC = 2 * 60 * 60
MIN_SYMBOLS = 40
THRUST_PCT = 70.0
WASHOUT_PCT = 30.0


def record_bars(symbol: str, df) -> None:
    """Store this symbol's breadth facts from a daily-bar frame with a
    `close` column (oldest first). Silent on any shape problem."""
    try:
        closes = [float(x) for x in df["close"].tolist()]
    except Exception:
        return
    if len(closes) < 2:
        return
    last, prev = closes[-1], closes[-2]
    rec: Dict[str, Any] = {"ts": time.time(), "up": last > prev, "down": last < prev}
    if len(closes) >= 20:
        rec["above_sma20"] = last > sum(closes[-20:]) / 20.0
    if len(closes) >= 50:
        rec["above_sma50"] = last > sum(closes[-50:]) / 50.0
    with _LOCK:
        _STATE[symbol.upper()] = rec


def snapshot(now: float = None) -> Dict[str, Any]:
    now = now or time.time()
    with _LOCK:
        recs = [r for r in _STATE.values() if now - r["ts"] <= RECENT_SEC]
    n = len(recs)
    if n < MIN_SYMBOLS:
        return {"has_data": False, "n": n, "reason": f"only {n} symbols in the last 2h"}
    adv = sum(1 for r in recs if r["up"])
    dec = sum(1 for r in recs if r["down"])
    with20 = [r for r in recs if "above_sma20" in r]
    with50 = [r for r in recs if "above_sma50" in r]
    out: Dict[str, Any] = {
        "has_data": True, "n": n, "advancers": adv, "decliners": dec,
        "adv_dec": round(adv / dec, 2) if dec else None,
    }
    if with20:
        out["pct_above_sma20"] = round(100.0 * sum(1 for r in with20 if r["above_sma20"]) / len(with20), 1)
    if with50:
        out["pct_above_sma50"] = round(100.0 * sum(1 for r in with50 if r["above_sma50"]) / len(with50), 1)
    p = out.get("pct_above_sma20")
    out["regime"] = ("thrust" if p is not None and p >= THRUST_PCT
                     else "washout" if p is not None and p <= WASHOUT_PCT
                     else "mixed")
    return out


def reset() -> None:
    with _LOCK:
        _STATE.clear()
