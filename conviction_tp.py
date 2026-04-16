"""Conviction-based take-profit override.

When a long position hits its fixed take-profit threshold, the default
behavior is to sell and realize the gain. That's disciplined but caps
the upside on runaway winners — a +20% TP sells IONQ right before it
continues to +35%, and the best the system can do afterward is buy
back 1-2% higher (paying spread + slippage twice for no extra return).

This module implements the override. When enabled on a profile, we
skip the fixed TP trigger (only for long positions) when ALL of these
conditions are true:

    1. Most recent AI prediction for the symbol had confidence >= threshold
       (default 70). This is the "AI still wants this trade" signal.
    2. ADX on the latest daily bar >= threshold (default 25). This
       confirms the trend has actual strength, not just a recent spike.
    3. Current price is >= the previous bar's high. This confirms the
       trend is STILL intact right now, not already rolling over.

When skip fires, the trailing stop (ATR-based) continues to manage the
exit. If the trend reverses, the trailing stop catches it. If it keeps
running, we keep the gains.

Stop-loss is NEVER overridden. Short-position take-profit is never
overridden (shorts typically profit on fast reversals, not sustained
trends).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _latest_ai_confidence(db_path: str, symbol: str) -> Optional[float]:
    """Return the AI confidence of the most recent prediction for `symbol`
    in this profile's journal DB, or None if not found."""
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT confidence FROM ai_predictions "
            "WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        conn.close()
        return float(row[0]) if row and row[0] is not None else None
    except Exception as exc:
        logger.warning("conviction_tp: AI confidence lookup failed for %s: %s",
                       symbol, exc)
        return None


def _latest_trend_snapshot(symbol: str) -> Optional[dict]:
    """Return latest ADX + last-bar-high + current-close for `symbol`."""
    try:
        from market_data import get_bars
        bars = get_bars(symbol, limit=30)
        if bars is None or bars.empty or len(bars) < 2:
            return None

        # add_indicators is already called by get_bars pipelines upstream
        # but defensive: compute ADX if missing.
        if "adx" not in bars.columns:
            try:
                from indicators import add_indicators
                bars = add_indicators(bars.copy())
            except Exception:
                return None

        last = bars.iloc[-1]
        prev = bars.iloc[-2]
        return {
            "adx": float(last["adx"]) if "adx" in last and last["adx"] == last["adx"] else None,
            "prev_high": float(prev["high"]),
            "current_close": float(last["close"]),
        }
    except Exception as exc:
        logger.warning("conviction_tp: trend snapshot failed for %s: %s",
                       symbol, exc)
        return None


def should_skip_take_profit(symbol: str,
                            ai_confidence: Optional[float],
                            trend: Optional[dict],
                            min_confidence: float,
                            min_adx: float) -> bool:
    """Pure predicate — takes already-fetched data and returns skip decision.

    Isolated from IO so tests can pin behavior without mocking the bar feed.
    """
    if ai_confidence is None or ai_confidence < min_confidence:
        return False
    if trend is None:
        return False
    adx = trend.get("adx")
    if adx is None or adx < min_adx:
        return False
    # Current price must be at or above the previous bar's high — the
    # trend has to be demonstrably still intact RIGHT NOW, not just
    # historically strong.
    if trend.get("current_close", 0) < trend.get("prev_high", 0):
        return False
    return True


def build_conviction_skip(ctx, db_path: Optional[str]) -> Callable[[str, float], bool]:
    """Return a `(symbol, pct_change) -> bool` skip predicate suitable for
    passing as `conviction_tp_skip` to `check_stop_loss_take_profit`."""
    min_confidence = float(getattr(ctx, "conviction_tp_min_confidence", 70.0))
    min_adx = float(getattr(ctx, "conviction_tp_min_adx", 25.0))

    def _skip(symbol: str, pct_change: float) -> bool:
        ai_conf = _latest_ai_confidence(db_path, symbol)
        trend = _latest_trend_snapshot(symbol)
        skip = should_skip_take_profit(
            symbol, ai_conf, trend, min_confidence, min_adx,
        )
        if skip:
            logger.info(
                "conviction_tp: SKIP take-profit for %s at %+.2f%% "
                "(AI conf=%.0f, ADX=%.1f, close=%.2f, prev_high=%.2f) — "
                "letting trailing stop manage exit",
                symbol, pct_change * 100, ai_conf,
                trend["adx"], trend["current_close"], trend["prev_high"],
            )
        return skip

    return _skip
