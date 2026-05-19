"""Market regime detection — bull, bear, sideways, volatile."""

import logging
import time
from datetime import date as _date
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def _vix_from_yfinance() -> Optional[float]:
    """Tier-2 fallback: pull ^VIX from yfinance directly.

    Used only when the Alpaca SPY-options path returns None. Per the
    Alpaca-first / custom-altdata / yfinance-last data-source rule,
    yfinance is the last resort but still vastly better than the old
    hardcoded VIX=20 fallback (which silently fed false data to the
    AI prompt every time the options chain hiccupped — caught
    2026-05-16 zero-error audit).
    """
    try:
        import yfinance as yf
        v = yf.Ticker("^VIX").history(period="1d", interval="1d")
        if v is None or v.empty:
            return None
        close = float(v["Close"].iloc[-1])
        if close <= 0:
            return None
        return close
    except Exception as exc:
        logger.warning(
            "VIX yfinance fallback also failed: %s: %s",
            type(exc).__name__, exc,
        )
        return None


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
        # Pre-2026-05-16 this was logger.debug — invisible by default,
        # so every failure looked the same as "no chain available".
        # Surface the specific cause so the failure mode is observable.
        logger.warning(
            "VIX computation from SPY options failed: %s: %s",
            type(exc).__name__, exc,
        )
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

        # VIX priority: Alpaca SPY options ATM IV → yfinance ^VIX →
        # explicit unknown. Pre-2026-05-16 a missing Alpaca chain
        # silently defaulted to VIX=20 (vix_level='moderate', "normal
        # market" classification fed to AI prompt + every downstream
        # consumer). Caught in the zero-error audit: 3+/day silent
        # false-VIX events. Two real sources beat one hardcoded
        # fake — per the Alpaca-first / custom-altdata / yfinance-last
        # data-source rule, yfinance ^VIX is the right tier-2 here.
        vix_val = _vix_from_spy_options()
        vix_source = "alpaca_spy_options"
        if vix_val is None:
            logger.warning(
                "VIX from SPY options unavailable; trying yfinance ^VIX"
            )
            vix_val = _vix_from_yfinance()
            vix_source = "yfinance_vix"
        if vix_val is None:
            # Both tiers failed. Mark UNKNOWN — never silently
            # substitute a fake VIX. Downstream consumers must
            # check `vix_source` and degrade gracefully (skip the
            # VIX-derived regime classification, AI prompt shows
            # "VIX unavailable").
            logger.error(
                "VIX unavailable from BOTH Alpaca SPY options AND "
                "yfinance ^VIX — marking source='unknown'. AI prompt "
                "will receive 'VIX unavailable' instead of a fake "
                "default."
            )
            result["vix"] = None
            result["vix_level"] = "unknown"
            result["vix_source"] = "unknown"
        else:
            result["vix"] = round(vix_val, 2)
            result["vix_source"] = vix_source

        # VIX level classification — only when VIX is real. When the
        # source is "unknown" leave vix_level as already-set to
        # "unknown" (the else-branch above did that).
        if vix_val is not None:
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

        # Determine regime. Volatile classification needs a real VIX;
        # if VIX is unknown, fall through to the trend-based logic
        # rather than silently misclassifying.
        if vix_val is not None and vix_val > 30 and result["volatility"] == "high":
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
        vix_desc = {"low": "low complacency", "moderate": "moderate", "high": "elevated fear", "extreme": "extreme fear/panic", "unknown": "unavailable"}.get(result["vix_level"], "")
        vix_str = (
            f"VIX {vix_val:.1f} ({vix_desc})"
            if vix_val is not None else
            "VIX unavailable"
        )
        result["summary"] = (
            f"{regime.upper()} market with {result['volatility']} volatility. "
            f"SPY ${spy_price:.2f} {above_below} SMA50 ${sma50:.2f} (trend {trend_word}), "
            f"{vix_str}."
        )

        # Cache result
        _cache["regime"] = result
        _cache["regime_ts"] = time.time()

        logger.info(
            "Market regime detected: %s (VIX %s, SPY trend %s)",
            regime,
            f"{vix_val:.1f}" if vix_val is not None else "unavailable",
            result["spy_trend"],
        )

        # Phase 4c shadow predict (2026-05-18 PM). Runs the ML
        # classifier alongside the rule output and logs both to
        # `regime_shadow_calls` for accumulated comparison data.
        # The production result above is NOT changed — promotion
        # to the ML path requires measured outperformance from the
        # logged comparisons.
        _shadow_log_if_enabled(result, spy_hist, vix_val)

        return result

    except Exception as exc:
        logger.error("Failed to detect market regime: %s", exc)
        return result


def _shadow_log_if_enabled(rule_result, spy_hist, vix_val):
    """Phase 4c — best-effort shadow predict + log.

    Reads `config.DB_PATH` and `config.REGIME_CLASSIFIER_PATH`
    (latter defaults to `/opt/quantopsai/regime_classifier_ml_v1.pkl`).
    Skips silently when:
      - the model pickle isn't present (model not yet trained)
      - the master DB path isn't configured
      - the VIX is unknown (we don't shadow-predict without a real VIX
        — feeding a fake into the model would pollute the comparison)

    No exception from the shadow path can break regime detection;
    the outer detect_regime() try/except already wraps this call,
    and we also wrap the body for defense in depth.
    """
    try:
        import config
        db = getattr(config, "DB_PATH", None)
        model_path = getattr(
            config, "REGIME_CLASSIFIER_PATH",
            "/opt/quantopsai/regime_classifier_ml_v1.pkl",
        )
        if not db:
            return
        if vix_val is None:
            return
        # Build the three series the classifier needs from the SPY
        # DataFrame the rule path already pulled. spy_hist has at
        # least 50 bars (per the earlier guard); the classifier needs
        # 200 — skip if we're under.
        if spy_hist is None or len(spy_hist) < 200:
            return
        spy_close = [float(c) for c in spy_hist["close"].tolist()]
        spy_high = [float(h) for h in spy_hist["high"].tolist()]
        spy_low = [float(l) for l in spy_hist["low"].tolist()]
        # For VIX history we use a flat series of the current value —
        # the bootstrap dataset includes vix_change_20d but in
        # production we don't fetch the VIX history per cycle. Future
        # enhancement: add a small VIX history fetcher. For now the
        # shadow logs `vix_change_20d=0` which the model treats as a
        # mild bias toward "no recent vol change."
        vix_series = [float(vix_val)] * 25

        from regime_classifier_ml import shadow_predict_and_log
        shadow_predict_and_log(
            model_path=model_path,
            db_path=db,
            rule_regime=rule_result.get("regime", "unknown"),
            spy_close=spy_close,
            spy_high=spy_high,
            spy_low=spy_low,
            vix_series=vix_series,
            spy_price_now=rule_result.get("spy_price", 0.0),
            vix_now=vix_val,
        )
    except Exception as exc:
        # Production regime detection MUST NOT fail because of shadow.
        logger.debug(
            "regime ML shadow log skipped: %s: %s",
            type(exc).__name__, exc,
        )


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
