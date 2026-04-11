"""Mid Cap ($20-$100) trading strategies.

Tuned for institutional-owned, sector-following, liquid names. Momentum
strategies work well here -- moves are more sustained and predictable.

For Sector Momentum, we approximate by checking if SPY is above its SMA20
(market trend) since we don't have sector ETF data easily.

Default parameters:
  - stop_loss: 5%
  - take_profit: 7%
  - max_position: 8% of equity
  - min_volume: 500,000
  - volume_surge_threshold: 2x
"""

import time
import pandas as pd
from market_data import get_bars, add_indicators


# ---------------------------------------------------------------------------
# SPY cache (module-level, 30-minute TTL)
# ---------------------------------------------------------------------------

_spy_cache = {"df": None, "timestamp": 0}
_SPY_CACHE_TTL = 1800  # 30 minutes


def _get_spy_data():
    """Fetch SPY data once and cache for 30 minutes."""
    now = time.time()
    if _spy_cache["df"] is not None and (now - _spy_cache["timestamp"]) < _SPY_CACHE_TTL:
        return _spy_cache["df"]

    df = get_bars("SPY", limit=200)
    if not df.empty:
        df = df.copy()
        df = add_indicators(df)
        _spy_cache["df"] = df
        _spy_cache["timestamp"] = now
    return df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prepare_df(symbol, df, min_rows=25):
    """Fetch data if needed, add indicators, and validate row count.

    Returns (df, error_result) -- if error_result is not None the caller
    should return it immediately.
    """
    if df is None:
        df = get_bars(symbol, limit=200)

    df = df.copy()
    # Only add indicators if they're not already present (backtester pre-computes)
    if "rsi" not in df.columns:
        df = add_indicators(df)

    df = df.dropna(subset=["rsi", "sma_20", "volume_sma_20"])

    if len(df) < min_rows:
        return None, {
            "symbol": symbol,
            "signal": "HOLD",
            "reason": f"Not enough data ({len(df)} rows, need {min_rows})",
        }

    return df, None


# ---------------------------------------------------------------------------
# 1. Sector Momentum (using SPY as proxy)
# ---------------------------------------------------------------------------

def sector_momentum_strategy(symbol, ctx=None, df=None,
                             rsi_threshold=50):
    """Mid-cap sector momentum using SPY as market trend proxy.

    BUY  -- stock RSI > rsi_threshold AND SPY above SMA20 (market trending up)
            AND volume > avg
    EXIT -- stock drops below 20-day SMA OR SPY reverses below SMA20
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    sma_20 = float(latest["sma_20"])
    volume = float(latest["volume"])
    vol_avg = float(latest["volume_sma_20"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0

    # Check SPY trend
    spy_df = _get_spy_data()
    spy_above_sma = False
    spy_info = "SPY data unavailable"
    if spy_df is not None and not spy_df.empty:
        spy_clean = spy_df.dropna(subset=["sma_20"])
        if not spy_clean.empty:
            spy_latest = spy_clean.iloc[-1]
            spy_price = float(spy_latest["close"])
            spy_sma = float(spy_latest["sma_20"])
            spy_above_sma = spy_price > spy_sma
            spy_info = f"SPY {'above' if spy_above_sma else 'below'} SMA20 ({spy_sma:.2f})"

    # BUY conditions
    if rsi > rsi_threshold and spy_above_sma and vol_ratio > 1.0:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Sector momentum: RSI {rsi:.1f} (> 50), {spy_info}, "
                f"vol {vol_ratio:.1f}x avg"
            ),
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "volume_ratio": round(vol_ratio, 2),
        }

    # SELL -- stock below SMA20 or SPY reversed
    if price < sma_20:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Price ({price:.2f}) below SMA20 ({sma_20:.2f}) -- "
                f"sector momentum lost"
            ),
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "volume_ratio": round(vol_ratio, 2),
        }

    if not spy_above_sma and rsi < 45:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"SPY below SMA20 and stock RSI {rsi:.1f} -- market downturn"
            ),
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "volume_ratio": round(vol_ratio, 2),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No sector momentum trigger (RSI {rsi:.1f}, {spy_info}, "
            f"vol {vol_ratio:.1f}x)"
        ),
        "price": price,
        "rsi": rsi,
        "sma_20": sma_20,
        "volume_ratio": round(vol_ratio, 2),
    }


# ---------------------------------------------------------------------------
# 2. Breakout with Volume
# ---------------------------------------------------------------------------

def breakout_volume_strategy(symbol, ctx=None, df=None,
                             vol_multiplier=2.0, rsi_low=55, rsi_high=75):
    """Clean breakouts above resistance for mid-caps.

    BUY  -- price > 20-day high AND volume > vol_multiplier x avg AND RSI rsi_low-rsi_high
    EXIT -- price drops below 10-day low
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    df["high_20"] = df["high"].rolling(window=20).max()
    df["low_10"] = df["low"].rolling(window=10).min()
    df = df.dropna(subset=["high_20", "low_10"])

    if df.empty:
        return {"symbol": symbol, "signal": "HOLD", "reason": "Not enough data after rolling windows"}

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    volume = float(latest["volume"])
    vol_avg = float(latest["volume_sma_20"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0
    high_20 = float(latest["high_20"])
    low_10 = float(latest["low_10"])

    # BUY conditions
    if price > high_20 and vol_ratio > vol_multiplier and rsi_low <= rsi <= rsi_high:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Breakout above 20-day high ({high_20:.2f}), "
                f"volume {vol_ratio:.1f}x avg, RSI {rsi:.1f}"
            ),
            "price": price,
            "rsi": rsi,
            "high_20": high_20,
            "volume_ratio": round(vol_ratio, 2),
        }

    # SELL conditions
    if price < low_10:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Price ({price:.2f}) dropped below 10-day low ({low_10:.2f})"
            ),
            "price": price,
            "rsi": rsi,
            "high_20": high_20,
            "low_10": low_10,
            "volume_ratio": round(vol_ratio, 2),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No breakout (price {price:.2f} vs 20d-high {high_20:.2f}), "
            f"RSI {rsi:.1f}, vol {vol_ratio:.1f}x"
        ),
        "price": price,
        "rsi": rsi,
        "high_20": high_20,
        "low_10": low_10,
        "volume_ratio": round(vol_ratio, 2),
    }


# ---------------------------------------------------------------------------
# 3. Pullback to Support
# ---------------------------------------------------------------------------

def pullback_support_strategy(symbol, ctx=None, df=None,
                              rsi_low=40, rsi_high=55):
    """Buy dips in uptrends for mid-caps.

    BUY  -- price pulls back to 20-day SMA from above AND RSI rsi_low-rsi_high
            AND SMA20 still rising
    EXIT -- price closes below SMA50
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    sma_20 = float(latest["sma_20"])
    sma_50 = float(latest["sma_50"]) if pd.notna(latest.get("sma_50")) else None

    # Compute SMA20 slope (compare current vs 5 bars ago)
    if len(df) >= 6:
        sma_20_prev = float(df.iloc[-6]["sma_20"])
        sma_rising = sma_20 > sma_20_prev
    else:
        sma_rising = False

    # Price near SMA20 (within 2% above or touching)
    pct_from_sma = ((price - sma_20) / sma_20 * 100) if sma_20 > 0 else 0
    near_sma = -2 <= pct_from_sma <= 2

    rsi_in_range = rsi_low <= rsi <= rsi_high

    # BUY conditions
    if near_sma and rsi_in_range and sma_rising:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Pullback to SMA20 ({sma_20:.2f}), RSI {rsi:.1f}, "
                f"SMA20 rising -- buying the dip"
            ),
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "pct_from_sma": round(pct_from_sma, 2),
        }

    # SELL -- price closes below SMA50
    if sma_50 is not None and price < sma_50:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Price ({price:.2f}) below SMA50 ({sma_50:.2f}) -- "
                f"uptrend broken"
            ),
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "sma_50": sma_50,
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No pullback setup (price {pct_from_sma:+.1f}% from SMA20, "
            f"RSI {rsi:.1f}, SMA rising: {sma_rising})"
        ),
        "price": price,
        "rsi": rsi,
        "sma_20": sma_20,
        "pct_from_sma": round(pct_from_sma, 2),
    }


# ---------------------------------------------------------------------------
# 4. MACD Cross
# ---------------------------------------------------------------------------

def macd_cross_strategy(symbol, ctx=None, df=None):
    """Momentum shift detection via MACD crossover.

    BUY  -- MACD crosses above signal line AND histogram turning positive
            AND price > SMA50
    EXIT -- MACD crosses below signal line
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    # Need MACD data
    df = df.dropna(subset=["macd", "macd_signal", "macd_histogram"])
    if len(df) < 3:
        return {"symbol": symbol, "signal": "HOLD", "reason": "Not enough MACD data"}

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    macd_val = float(latest["macd"])
    macd_sig = float(latest["macd_signal"])
    macd_hist = float(latest["macd_histogram"])
    prev_macd = float(prev["macd"])
    prev_sig = float(prev["macd_signal"])
    sma_50 = float(latest["sma_50"]) if pd.notna(latest.get("sma_50")) else None

    # MACD just crossed above signal
    macd_crossed_up = prev_macd <= prev_sig and macd_val > macd_sig
    # MACD just crossed below signal
    macd_crossed_down = prev_macd >= prev_sig and macd_val < macd_sig

    # BUY conditions
    if macd_crossed_up and macd_hist > 0 and sma_50 is not None and price > sma_50:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"MACD bullish cross (MACD {macd_val:.3f} > Signal {macd_sig:.3f}), "
                f"histogram positive, price above SMA50 ({sma_50:.2f})"
            ),
            "price": price,
            "rsi": rsi,
            "macd": round(macd_val, 4),
            "macd_signal": round(macd_sig, 4),
            "macd_histogram": round(macd_hist, 4),
        }

    # SELL -- MACD crossed below signal
    if macd_crossed_down:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"MACD bearish cross (MACD {macd_val:.3f} < Signal {macd_sig:.3f})"
            ),
            "price": price,
            "rsi": rsi,
            "macd": round(macd_val, 4),
            "macd_signal": round(macd_sig, 4),
            "macd_histogram": round(macd_hist, 4),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No MACD cross (MACD {macd_val:.3f}, Signal {macd_sig:.3f}, "
            f"Hist {macd_hist:.3f})"
        ),
        "price": price,
        "rsi": rsi,
        "macd": round(macd_val, 4),
        "macd_signal": round(macd_sig, 4),
        "macd_histogram": round(macd_hist, 4),
    }


# ---------------------------------------------------------------------------
# Combined Mid Cap Strategy
# ---------------------------------------------------------------------------

def mid_combined_strategy(symbol, ctx=None, df=None, params=None):
    """Run all four mid-cap strategies, score them, and return the
    strongest signal.

    Scoring:
        Each BUY vote  = +1
        Each SELL vote = -1
        HOLD           =  0

    Result mapping:
        score >= 2  -> STRONG_BUY
        score == 1  -> BUY
        score == -1 -> SELL
        score <= -2 -> STRONG_SELL
        else        -> HOLD
    """
    if params is None:
        params = {}

    # Extract user-configurable thresholds from params, falling back to defaults
    rsi_oversold = float(params.get("rsi_oversold", 50.0))
    volume_surge_mult = float(params.get("volume_surge_multiplier", 2.0))
    breakout_vol_threshold = float(params.get("breakout_volume_threshold", 2.0))

    # Strategy toggles
    use_momentum = bool(params.get("strategy_momentum_breakout", True))
    use_volume_spike = bool(params.get("strategy_volume_spike", True))
    use_mean_reversion = bool(params.get("strategy_mean_reversion", True))

    # Fetch data once and share across strategies
    if df is None:
        df = get_bars(symbol, limit=200)

    strategies = {
        "sector_momentum": lambda sym, ctx=ctx, df=None: (
            sector_momentum_strategy(sym, ctx=ctx, df=df,
                                     rsi_threshold=rsi_oversold)
        ),
        "breakout_volume": lambda sym, ctx=ctx, df=None: (
            breakout_volume_strategy(sym, ctx=ctx, df=df,
                                     vol_multiplier=breakout_vol_threshold,
                                     rsi_low=55, rsi_high=75)
        ),
        "pullback_support": lambda sym, ctx=ctx, df=None: (
            pullback_support_strategy(sym, ctx=ctx, df=df,
                                      rsi_low=40, rsi_high=55)
        ),
        "macd_cross": macd_cross_strategy,
    }

    # Strategy toggle map
    toggle_map = {
        "sector_momentum": use_momentum,
        "breakout_volume": use_volume_spike,
        "pullback_support": use_mean_reversion,
        "macd_cross": True,  # MACD has no user params, always active
    }

    votes = {}
    results = {}
    score = 0

    for name, fn in strategies.items():
        if not toggle_map.get(name, True):
            results[name] = {"symbol": symbol, "signal": "HOLD",
                             "reason": f"{name} disabled by user settings"}
            votes[name] = "HOLD"
            continue
        result = fn(symbol, ctx=ctx, df=df.copy())
        results[name] = result
        sig = result.get("signal", "HOLD")

        if "BUY" in sig:
            votes[name] = "BUY"
            score += 1
        elif "SELL" in sig:
            votes[name] = "SELL"
            score -= 1
        else:
            votes[name] = "HOLD"

    # Map score to final signal
    if score >= 2:
        signal = "STRONG_BUY"
    elif score == 1:
        signal = "BUY"
    elif score == -1:
        signal = "SELL"
    elif score <= -2:
        signal = "STRONG_SELL"
    else:
        signal = "HOLD"

    # Build a concise reason from the individual votes
    vote_summary = ", ".join(f"{name}={vote}" for name, vote in votes.items())
    buy_reasons = [
        results[n]["reason"] for n, v in votes.items() if v == "BUY"
    ]
    sell_reasons = [
        results[n]["reason"] for n, v in votes.items() if v == "SELL"
    ]

    reason_parts = [f"Score {score} ({vote_summary})"]
    if buy_reasons:
        reason_parts.append("BUY reasons: " + "; ".join(buy_reasons))
    if sell_reasons:
        reason_parts.append("SELL reasons: " + "; ".join(sell_reasons))

    # Use the price from whichever sub-strategy returned one
    price = None
    for r in results.values():
        if r.get("price") is not None:
            price = r["price"]
            break

    return {
        "symbol": symbol,
        "signal": signal,
        "reason": " | ".join(reason_parts),
        "price": price,
        "score": score,
        "votes": votes,
        "strategy_results": results,
    }
