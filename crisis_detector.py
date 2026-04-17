"""Cross-asset crisis detection — Phase 10 of the Quant Fund Evolution roadmap.

Capital preservation is the final, non-negotiable layer. Every alpha-
generating phase above this one is worth zero if a single regime break
wipes out the account. Real funds have a risk desk whose only job is
to look at the whole market — not individual tickers — and flag
crisis conditions that warrant shrinking exposure. That's what this
module does.

Monitored signals:
  * VIX level + term structure (raw volatility regime)
  * Cross-asset correlation spike (SPY, TLT, GLD, DXY converging — the
    classic "everything sells off together" signal)
  * Bond/stock divergence (TLT rallying while SPY falling = flight to
    safety)
  * Gold rally (GLD spiking as safe haven)
  * Credit spread proxy (HYG/LQD ratio falling = high-yield bonds under
    stress while investment-grade holds)
  * Event cluster (≥3 price_shock events from Phase 9 in 30 minutes)

Crisis levels, in escalating order:
  normal        — trade normally
  elevated      — 0.5× position sizes, tighter stops (1 or 2 warning signals)
  crisis        — no new longs, hold existing (3-4 signals or VIX > 35)
  severe        — liquidate, 100% cash (5+ signals or VIX > 45)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds. Tuned conservatively — better to shrink once too often
# than to miss a real crisis.
# ---------------------------------------------------------------------------

THRESHOLDS = {
    "vix_elevated":            22.0,
    "vix_crisis":              32.0,
    "vix_severe":              45.0,
    "correlation_spike":       0.75,   # pairwise rolling correlation avg
    "bond_stock_divergence":   3.0,    # TLT up pct - SPY down pct >= threshold
    "gold_rally_pct":          3.0,    # GLD 5d move to trigger safe-haven flag
    "credit_stress_drop_pct": -2.0,    # HYG/LQD ratio drop over 10 days
    "event_cluster_count":     3,      # price shocks in last 30 minutes
}


# Crisis level constants
NORMAL = "normal"
ELEVATED = "elevated"
CRISIS = "crisis"
SEVERE = "severe"

LEVELS = (NORMAL, ELEVATED, CRISIS, SEVERE)
LEVEL_RANK = {NORMAL: 0, ELEVATED: 1, CRISIS: 2, SEVERE: 3}


# Position sizing multipliers applied at each crisis level
SIZE_MULTIPLIERS = {
    NORMAL:   1.0,
    ELEVATED: 0.5,
    CRISIS:   0.0,   # no new longs
    SEVERE:   0.0,
}


# ---------------------------------------------------------------------------
# Top-level detector
# ---------------------------------------------------------------------------

def detect_crisis_state(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Run every signal detector and classify the current crisis level.

    Returns:
        {"level": str, "signals": [signal_dicts], "readings": {...},
         "size_multiplier": float}
    """
    signals: List[Dict[str, Any]] = []
    readings: Dict[str, Any] = {}

    vix_signal = _check_vix(readings)
    if vix_signal:
        signals.append(vix_signal)

    corr_signal = _check_cross_asset_correlation(readings)
    if corr_signal:
        signals.append(corr_signal)

    div_signal = _check_bond_stock_divergence(readings)
    if div_signal:
        signals.append(div_signal)

    gold_signal = _check_gold_rally(readings)
    if gold_signal:
        signals.append(gold_signal)

    credit_signal = _check_credit_stress(readings)
    if credit_signal:
        signals.append(credit_signal)

    if db_path:
        cluster_signal = _check_event_cluster(db_path, readings)
        if cluster_signal:
            signals.append(cluster_signal)

    level = _classify_level(signals, readings.get("vix", 0) or 0)
    return {
        "level": level,
        "signals": signals,
        "readings": readings,
        "size_multiplier": SIZE_MULTIPLIERS[level],
    }


# ---------------------------------------------------------------------------
# Individual signal checks
# ---------------------------------------------------------------------------

def _fetch_close_series(symbol: str, days: int = 30) -> Optional[Any]:
    """Return a pandas Series of closing prices or None on failure."""
    try:
        from market_data import get_bars
        df = get_bars(symbol, limit=days)
        if df is None or len(df) < 5:
            return None
        return df["close"]
    except Exception:
        return None


def _check_vix(readings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """VIX level + term structure from ^VIX spot and ^VIX3M."""
    spot = _fetch_close_series("^VIX", days=30)
    if spot is None:
        return None

    vix_level = float(spot.iloc[-1])
    vix_5d_avg = float(spot.iloc[-5:].mean()) if len(spot) >= 5 else vix_level
    readings["vix"] = round(vix_level, 2)
    readings["vix_5d_avg"] = round(vix_5d_avg, 2)

    # Term structure: VIX3M / VIX. Below 1.0 = inverted (front-month higher
    # than 3-month = imminent stress priced in).
    vix_3m_series = _fetch_close_series("^VIX3M", days=10)
    if vix_3m_series is not None and len(vix_3m_series) > 0:
        vix_3m = float(vix_3m_series.iloc[-1])
        if vix_level > 0:
            ts_ratio = vix_3m / vix_level
            readings["vix_term_ratio"] = round(ts_ratio, 3)
            if ts_ratio < 0.95:
                return {
                    "name": "vix_inversion",
                    "severity": "high",
                    "detail": f"VIX term structure inverted (3M/spot = {ts_ratio:.2f})",
                }

    if vix_level >= THRESHOLDS["vix_severe"]:
        return {"name": "vix_severe", "severity": "critical",
                "detail": f"VIX {vix_level:.1f} ≥ severe threshold"}
    if vix_level >= THRESHOLDS["vix_crisis"]:
        return {"name": "vix_crisis", "severity": "high",
                "detail": f"VIX {vix_level:.1f} ≥ crisis threshold"}
    if vix_level >= THRESHOLDS["vix_elevated"]:
        return {"name": "vix_elevated", "severity": "medium",
                "detail": f"VIX {vix_level:.1f} ≥ elevated threshold"}
    return None


def _check_cross_asset_correlation(readings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Flag when normally-uncorrelated assets (SPY, TLT, GLD, DXY) move together.

    In a healthy market, stocks, bonds, and gold diversify each other. In a
    liquidity crunch, correlations spike as everyone sells everything for
    cash. Rolling 10-day pairwise correlation avg ≥ 0.75 is rare and bad.
    """
    try:
        import pandas as pd
        series = {}
        for sym in ("SPY", "TLT", "GLD", "UUP"):
            s = _fetch_close_series(sym, days=20)
            if s is None or len(s) < 11:
                return None
            series[sym] = s.pct_change().dropna()
        df = pd.DataFrame(series).dropna()
        if len(df) < 5:
            return None
        corr = df.tail(10).corr().abs()
        # Average off-diagonal correlation
        n = corr.shape[0]
        if n < 2:
            return None
        mask = ~corr.eq(1.0)
        avg_corr = float(corr.where(mask).stack().mean())
        readings["cross_asset_corr"] = round(avg_corr, 3)
        if avg_corr >= THRESHOLDS["correlation_spike"]:
            return {
                "name": "correlation_spike",
                "severity": "high",
                "detail": f"Cross-asset |corr| avg = {avg_corr:.2f} "
                          f"≥ {THRESHOLDS['correlation_spike']:.2f}",
            }
    except Exception as exc:
        logger.debug("correlation check failed: %s", exc)
    return None


def _check_bond_stock_divergence(readings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """TLT rallying while SPY falling = classic flight to safety."""
    tlt = _fetch_close_series("TLT", days=10)
    spy = _fetch_close_series("SPY", days=10)
    if tlt is None or spy is None or len(tlt) < 6 or len(spy) < 6:
        return None
    tlt_5d = (float(tlt.iloc[-1]) - float(tlt.iloc[-6])) / float(tlt.iloc[-6]) * 100
    spy_5d = (float(spy.iloc[-1]) - float(spy.iloc[-6])) / float(spy.iloc[-6]) * 100
    readings["tlt_5d_pct"] = round(tlt_5d, 2)
    readings["spy_5d_pct"] = round(spy_5d, 2)
    # Positive TLT, negative SPY: divergence magnitude = tlt_5d - spy_5d
    # Higher number = stronger flight-to-safety signal
    divergence = tlt_5d - spy_5d
    if tlt_5d > 0 and spy_5d < 0 and divergence >= THRESHOLDS["bond_stock_divergence"]:
        return {
            "name": "bond_stock_divergence",
            "severity": "high",
            "detail": f"TLT {tlt_5d:+.1f}% vs SPY {spy_5d:+.1f}% "
                      f"(divergence={divergence:.1f})",
        }
    return None


def _check_gold_rally(readings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Sharp gold rally = safe-haven demand."""
    gld = _fetch_close_series("GLD", days=10)
    if gld is None or len(gld) < 6:
        return None
    gld_5d = (float(gld.iloc[-1]) - float(gld.iloc[-6])) / float(gld.iloc[-6]) * 100
    readings["gld_5d_pct"] = round(gld_5d, 2)
    if gld_5d >= THRESHOLDS["gold_rally_pct"]:
        return {
            "name": "gold_rally",
            "severity": "medium",
            "detail": f"GLD +{gld_5d:.1f}% over 5 days (safe-haven demand)",
        }
    return None


def _check_credit_stress(readings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """HYG / LQD ratio falling = high-yield under stress relative to investment grade."""
    hyg = _fetch_close_series("HYG", days=15)
    lqd = _fetch_close_series("LQD", days=15)
    if hyg is None or lqd is None or len(hyg) < 11 or len(lqd) < 11:
        return None
    ratio_now = float(hyg.iloc[-1]) / float(lqd.iloc[-1])
    ratio_then = float(hyg.iloc[-11]) / float(lqd.iloc[-11])
    if ratio_then <= 0:
        return None
    change_pct = (ratio_now - ratio_then) / ratio_then * 100
    readings["hyg_lqd_ratio_10d_pct"] = round(change_pct, 2)
    if change_pct <= THRESHOLDS["credit_stress_drop_pct"]:
        return {
            "name": "credit_stress",
            "severity": "high",
            "detail": f"HYG/LQD ratio {change_pct:+.1f}% over 10d "
                      f"(high-yield distress)",
        }
    return None


def _check_event_cluster(db_path: str,
                         readings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Phase 9 price_shock events clustering in time = regime break in progress."""
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """SELECT COUNT(*) FROM events
               WHERE type = 'price_shock'
                 AND detected_at >= datetime('now', '-30 minutes')""",
        ).fetchone()
        conn.close()
        n = int(row[0] if row else 0)
        readings["price_shock_count_30m"] = n
        if n >= THRESHOLDS["event_cluster_count"]:
            return {
                "name": "event_cluster",
                "severity": "high",
                "detail": f"{n} price shocks in last 30 minutes",
            }
    except Exception as exc:
        logger.warning("Event cluster check failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify_level(signals: List[Dict[str, Any]], vix_level: float) -> str:
    """Map the signal set to a crisis level.

    Rules (VIX-first, then signal count):
      VIX ≥ severe threshold   → SEVERE
      VIX ≥ crisis threshold   → CRISIS (upgrades to SEVERE with ≥ 2 other signals)
      VIX ≥ elevated           → ELEVATED (upgrades to CRISIS with ≥ 3 signals)
      VIX normal               → ELEVATED with ≥ 2 signals, CRISIS with ≥ 4
    """
    critical_signals = [s for s in signals if s.get("severity") == "critical"]
    high_signals = [s for s in signals if s.get("severity") == "high"]
    total = len(signals)

    if critical_signals or vix_level >= THRESHOLDS["vix_severe"]:
        return SEVERE

    if vix_level >= THRESHOLDS["vix_crisis"]:
        return SEVERE if len(high_signals) >= 2 else CRISIS

    if vix_level >= THRESHOLDS["vix_elevated"]:
        if total >= 4:
            return CRISIS
        return ELEVATED

    if total >= 5:
        return SEVERE
    if total >= 3:
        return CRISIS
    if total >= 1:
        return ELEVATED
    return NORMAL
