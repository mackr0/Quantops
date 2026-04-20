"""Market regime detection — bull, bear, sideways, volatile."""

import logging
import time
from typing import Dict, Any

import yfinance as yf

logger = logging.getLogger(__name__)

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

        # VIX — yfinance only (Alpaca doesn't serve index data)
        # Wrapped in lock + cached to minimize Yahoo calls
        try:
            import threading
            _vix_lock = threading.Lock()
            with _vix_lock:
                vix_ticker = yf.Ticker("^VIX")
                vix_hist = vix_ticker.history(period="5d")
            if not vix_hist.empty:
                vix_val = float(vix_hist["Close"].iloc[-1])
                result["vix"] = round(vix_val, 2)
            else:
                vix_val = 20.0
        except Exception as vix_err:
            logger.warning("Failed to fetch VIX: %s", vix_err)
            vix_val = 20.0

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
