"""Market regime detection — bull, bear, sideways, volatile."""

import logging
import time
from datetime import date as _date
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def _vix_from_spy_options() -> Optional[float]:
    """Compute VIX-equivalent from SPY options chain.

    VIX is defined as the 30-day annualized implied volatility of SPX
    (S&P 500) options at the money. SPY tracks SPX at 1/10 scale but
    with the same volatility characteristics, so the ATM IV of a
    ~30-day SPY option IS the VIX (within a few basis points).

    Migrated 2026-05-01 from yfinance ^VIX to this Alpaca-native
    computation. Real-time, sub-second; uses our own options chain
    fetch (which has its own cache).

    Returns VIX value as a percentage (e.g., 18.5 for VIX=18.5),
    or None if no options chain is available.
    """
    try:
        from options_chain_alpaca import fetch_chain_alpaca
        chain = fetch_chain_alpaca("SPY")
        if not chain:
            return None
        spot = chain["current_price"]
        today = _date.today()

        # Find the expiration closest to 30 days out
        target_days = 30
        best = None
        for exp_iso in chain["expirations"]:
            try:
                exp_d = _date.fromisoformat(exp_iso)
                days_out = (exp_d - today).days
                if days_out <= 0:
                    continue
                gap = abs(days_out - target_days)
                if best is None or gap < best[0]:
                    best = (gap, exp_iso, days_out)
            except ValueError:
                continue
        if not best:
            return None
        target_exp = best[1]

        # Find that chain in chains[] (might not be there if not in
        # the near 3 expirations). If not, fetch_chain_alpaca already
        # has the data — we'd need a separate call. For SPY with
        # multiple weekly expirations, the 30-day expiry might not
        # be in the first 3. Search chains[] first.
        target_chain = next(
            (c for c in chain["chains"] if c["expiration"] == target_exp),
            None,
        )
        if not target_chain:
            # Fall back to whatever's farthest in chains[]
            target_chain = chain["chains"][-1]

        calls = target_chain["calls"]
        if calls is None or calls.empty:
            return None

        # ATM call IV — strike closest to spot
        idx = (calls["strike"] - spot).abs().idxmin()
        atm_iv = float(calls.loc[idx, "impliedVolatility"])
        if atm_iv <= 0:
            return None
        # Convert decimal (0.18) to percentage (18.0) — VIX convention
        return atm_iv * 100
    except Exception as exc:
        logger.debug("VIX computation from SPY options failed: %s", exc)
        return None

# Cache for 30 minutes
_cache: Dict[str, Any] = {"regime": None, "regime_ts": 0}
_CACHE_TTL = 30 * 60


def detect_regime() -> Dict[str, Any]:
    """Detect current market regime by analyzing SPY and VIX.

    Returns dict with keys: regime, spy_price, spy_sma50, spy_trend,
    vix, vix_level, breadth, volatility, recommendation, summary.

    Cached for 30 minutes.
    """
    # Check cache
    if _cache["regime"] is not None and (time.time() - _cache["regime_ts"]) < _CACHE_TTL:
        return _cache["regime"]

    result = {
        "regime": "unknown",
        "spy_price": 0.0,
        "spy_sma50": 0.0,
        "spy_trend": "flat",
        "vix": 0.0,
        "vix_level": "moderate",
        "breadth": 0.0,
        "volatility": "normal",
        "recommendation": "",
        "summary": "",
    }

    try:
        # Use Alpaca for SPY (reliable, no rate limiting) instead of yfinance
        from market_data import get_bars
        spy_hist = get_bars("SPY", limit=60)

        if spy_hist is None or spy_hist.empty or len(spy_hist) < 50:
            logger.warning("Not enough SPY data for regime detection")
            return result

        spy_price = float(spy_hist["close"].iloc[-1])
        sma50 = float(spy_hist["close"].tail(50).mean())
        result["spy_price"] = round(spy_price, 2)
        result["spy_sma50"] = round(sma50, 2)

        if len(spy_hist) >= 60:
            sma50_10d_ago = float(spy_hist["close"].iloc[-60:-10].tail(50).mean())
        else:
            sma50_10d_ago = sma50
        sma50_slope = sma50 - sma50_10d_ago

        if spy_price > sma50 and sma50_slope > 0:
            result["spy_trend"] = "up"
        elif spy_price < sma50 and sma50_slope < 0:
            result["spy_trend"] = "down"
        else:
            result["spy_trend"] = "flat"

        # VIX — computed from SPY 30-day ATM IV via Alpaca real-time
        # options chain (replaces yfinance ^VIX). VIX is by definition
        # the 30-day ATM IV of SPX/SPY, so this is the same number,
        # just computed locally from real-time data instead of
        # delayed yfinance feed.
        vix_val = _vix_from_spy_options()
        if vix_val is None:
            logger.warning("VIX from SPY options unavailable; defaulting to 20")
            vix_val = 20.0
        result["vix"] = round(vix_val, 2)

        # VIX level classification
        if vix_val < 15:
            result["vix_level"] = "low"
        elif vix_val < 25:
            result["vix_level"] = "moderate"
        elif vix_val < 35:
            result["vix_level"] = "high"
        else:
            result["vix_level"] = "extreme"

        # Calculate 14-day ATR for volatility
        high = spy_hist["high"].tail(15)
        low = spy_hist["low"].tail(15)
        close_prev = spy_hist["close"].shift(1).tail(15)
        tr = []
        for i in range(len(high)):
            h = float(high.iloc[i])
            l = float(low.iloc[i])
            cp = float(close_prev.iloc[i]) if i > 0 else l
            tr.append(max(h - l, abs(h - cp), abs(l - cp)))
        atr = sum(tr[-14:]) / 14 if len(tr) >= 14 else sum(tr) / max(len(tr), 1)
        atr_pct = (atr / spy_price) * 100 if spy_price > 0 else 0

        # Volatility classification based on ATR as % of price
        if atr_pct > 2.0:
            result["volatility"] = "high"
        elif atr_pct > 1.0:
            result["volatility"] = "moderate"
        else:
            result["volatility"] = "low"

        # Market breadth: approximate using SPY recent performance
        # (A true breadth calculation would need the full universe, but
        # we use a simplified approach based on how many of the last 20 days
        # had closes above the 20-day SMA)
        closes_20 = spy_hist["close"].tail(20)
        if len(closes_20) >= 20:
            sma20 = float(closes_20.mean())
            above_sma20 = sum(1 for c in closes_20 if float(c) > sma20)
            breadth = above_sma20 / len(closes_20)
        else:
            breadth = 0.5
        result["breadth"] = round(breadth, 2)

        # Determine regime
        if vix_val > 30 and result["volatility"] == "high":
            regime = "volatile"
        elif result["spy_trend"] == "up" and spy_price > sma50:
            regime = "bull"
        elif result["spy_trend"] == "down" and spy_price < sma50:
            regime = "bear"
        else:
            regime = "sideways"
        result["regime"] = regime

        # Recommendation based on regime
        recommendations = {
            "bull": "Favor long positions. Momentum and breakout strategies tend to work well. Use standard stop-losses.",
            "bear": "Favor short positions. Be skeptical of BUY signals — most oversold bounces fail in bear markets. Tighten stop-losses.",
            "sideways": "Mixed signals expected. Focus on mean reversion strategies. Reduce position sizes.",
            "volatile": "Extreme volatility — reduce position sizes significantly. Only take highest-conviction trades. Widen stop-losses to avoid whipsaws.",
        }
        result["recommendation"] = recommendations.get(regime, "")

        # Summary
        trend_word = {"up": "rising", "down": "falling", "flat": "flat"}.get(result["spy_trend"], "flat")
        above_below = "above" if spy_price > sma50 else "below"
        vix_desc = {"low": "low complacency", "moderate": "moderate", "high": "elevated fear", "extreme": "extreme fear/panic"}.get(result["vix_level"], "")
        result["summary"] = (
            f"{regime.upper()} market with {result['volatility']} volatility. "
            f"SPY ${spy_price:.2f} {above_below} SMA50 ${sma50:.2f} (trend {trend_word}), "
            f"VIX {vix_val:.1f} ({vix_desc})."
        )

        # Cache result
        _cache["regime"] = result
        _cache["regime_ts"] = time.time()

        logger.info("Market regime detected: %s (VIX %.1f, SPY trend %s)",
                     regime, vix_val, result["spy_trend"])
        return result

    except Exception as exc:
        logger.error("Failed to detect market regime: %s", exc)
        return result


def get_regime_context() -> str:
    """Return formatted string for AI prompt injection.

    Returns empty string if regime detection fails.
    """
    try:
        regime = detect_regime()
        if regime["regime"] == "unknown":
            return ""

        above_below = "above" if regime["spy_price"] > regime["spy_sma50"] else "below"
        vix_label = regime["vix_level"].upper()
        vix_desc = {
            "LOW": "low fear/complacency",
            "MODERATE": "normal conditions",
            "HIGH": "elevated fear",
            "EXTREME": "extreme fear/panic",
        }.get(vix_label, "")

        breadth_desc = "strong" if regime["breadth"] > 0.6 else "weak" if regime["breadth"] < 0.4 else "mixed"
        breadth_pct = int(regime["breadth"] * 100)

        lines = [
            "MARKET REGIME (Current Conditions):",
            f"Regime: {regime['regime'].upper()} MARKET",
            f"SPY: ${regime['spy_price']:.2f} ({above_below} SMA50 of ${regime['spy_sma50']:.2f}, trend: {regime['spy_trend']})",
            f"VIX: {regime['vix']:.1f} ({vix_label} — {vix_desc})",
            f"Market Breadth: {breadth_pct}% of recent days above SMA20 ({breadth_desc})",
            f"Volatility: {regime['volatility']}",
            f"Recommendation: {regime['recommendation']}",
        ]
        return "\n".join(lines)

    except Exception as exc:
        logger.warning("Failed to get regime context: %s", exc)
        return ""
