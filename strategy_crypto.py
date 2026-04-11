"""Crypto trading strategies.

Tuned for 24/7 trading, BTC-correlated, sentiment-driven assets.
Crypto trends hard in both directions. Technical analysis works differently --
support/resistance levels and volume matter, fundamentals don't apply.

BTC data is fetched once and cached at module level (30 min TTL).
All crypto symbols use "/" format (e.g. "BTC/USD"); get_bars handles
conversion to "-" format for yfinance.

Default parameters:
  - stop_loss: 8%
  - take_profit: 10%
  - max_position: 7% of equity
  - min_volume: 0 (crypto volume measured differently)
  - volume_surge_threshold: 3x
"""

import time
import pandas as pd
from market_data import get_bars, add_indicators


# ---------------------------------------------------------------------------
# BTC cache (module-level, 30-minute TTL)
# ---------------------------------------------------------------------------

_btc_cache = {"df": None, "timestamp": 0}
_BTC_CACHE_TTL = 1800  # 30 minutes


def _get_btc_data():
    """Fetch BTC/USD data once and cache for 30 minutes."""
    now = time.time()
    if _btc_cache["df"] is not None and (now - _btc_cache["timestamp"]) < _BTC_CACHE_TTL:
        return _btc_cache["df"]

    df = get_bars("BTC/USD", limit=200)
    if not df.empty:
        df = df.copy()
        df = add_indicators(df)
        _btc_cache["df"] = df
        _btc_cache["timestamp"] = now
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
# 1. BTC Correlation Play
# ---------------------------------------------------------------------------

def btc_correlation_strategy(symbol, ctx=None, df=None):
    """When BTC bounces, alts follow (usually amplified).

    BUY  -- BTC RSI < 45 AND BTC bouncing (positive 1-day change)
            AND alt RSI < 40
    EXIT -- BTC drops below recent low OR alt hits take-profit
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])

    # Check BTC
    btc_df = _get_btc_data()
    btc_rsi = None
    btc_bouncing = False
    btc_info = "BTC data unavailable"

    if btc_df is not None and not btc_df.empty:
        btc_clean = btc_df.dropna(subset=["rsi"])
        if len(btc_clean) >= 2:
            btc_latest = btc_clean.iloc[-1]
            btc_prev = btc_clean.iloc[-2]
            btc_rsi = float(btc_latest["rsi"])
            btc_price = float(btc_latest["close"])
            btc_prev_close = float(btc_prev["close"])
            btc_day_change = ((btc_price - btc_prev_close) / btc_prev_close * 100) if btc_prev_close > 0 else 0
            btc_bouncing = btc_day_change > 0
            btc_info = f"BTC RSI {btc_rsi:.1f}, change {btc_day_change:+.1f}%"

    # BUY conditions -- BTC oversold and bouncing, alt oversold
    if btc_rsi is not None and btc_rsi < 45 and btc_bouncing and rsi < 40:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"BTC correlation: {btc_info}, "
                f"alt RSI {rsi:.1f} (< 30) -- bounce setup"
            ),
            "price": price,
            "rsi": rsi,
            "btc_rsi": btc_rsi,
        }

    # SELL -- BTC weakening while alt is extended
    if btc_rsi is not None and btc_rsi > 70 and rsi > 65:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"BTC overbought ({btc_info}), alt RSI {rsi:.1f} -- take profits"
            ),
            "price": price,
            "rsi": rsi,
            "btc_rsi": btc_rsi,
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No BTC correlation trigger ({btc_info}, alt RSI {rsi:.1f})"
        ),
        "price": price,
        "rsi": rsi,
        "btc_rsi": btc_rsi,
    }


# ---------------------------------------------------------------------------
# 2. Trend Following
# ---------------------------------------------------------------------------

def trend_following_strategy(symbol, ctx=None, df=None):
    """Crypto trends harder than equities. Ride the trend.

    BUY  -- price crosses above SMA20 from below AND volume > 1.5x avg
            AND RSI 45-65
    EXIT -- price crosses back below SMA20
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    if len(df) < 3:
        return {"symbol": symbol, "signal": "HOLD", "reason": "Not enough data for crossover check"}

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    sma_20 = float(latest["sma_20"])
    prev_close = float(prev["close"])
    prev_sma_20 = float(prev["sma_20"])
    volume = float(latest["volume"])
    vol_avg = float(latest["volume_sma_20"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0

    # Price crossed above SMA20 (exact cross OR already above with momentum)
    crossed_above = prev_close <= prev_sma_20 and price > sma_20
    above_with_momentum = price > sma_20 and rsi > 50 and price > prev_close
    # Price just crossed below SMA20
    crossed_below = prev_close >= prev_sma_20 and price < sma_20

    # BUY conditions — crossover OR above SMA with momentum
    if (crossed_above or above_with_momentum) and 40 <= rsi <= 70:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Trend following: crossed above SMA20 ({sma_20:.2f}), "
                f"vol {vol_ratio:.1f}x, RSI {rsi:.1f}"
            ),
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "volume_ratio": round(vol_ratio, 2),
        }

    # SELL -- crossed back below SMA20
    if crossed_below:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Trend broken: crossed below SMA20 ({sma_20:.2f})"
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
            f"No trend cross (price {price:.2f} vs SMA20 {sma_20:.2f}, "
            f"RSI {rsi:.1f}, vol {vol_ratio:.1f}x)"
        ),
        "price": price,
        "rsi": rsi,
        "sma_20": sma_20,
        "volume_ratio": round(vol_ratio, 2),
    }


# ---------------------------------------------------------------------------
# 3. Extreme Oversold Bounce
# ---------------------------------------------------------------------------

def extreme_oversold_strategy(symbol, ctx=None, df=None):
    """Crypto drops are extreme but bounces are violent.

    BUY  -- RSI < 20 AND price > 25% below 20-day SMA
    EXIT -- RSI > 45 OR price returns to SMA20
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    sma_20 = float(latest["sma_20"])
    pct_below_sma = ((price - sma_20) / sma_20 * 100) if sma_20 > 0 else 0

    # BUY -- extreme oversold
    if rsi < 30 and pct_below_sma < -10:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Extreme oversold: RSI {rsi:.1f} (< 20), "
                f"price {pct_below_sma:.1f}% below SMA20 ({sma_20:.2f})"
            ),
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "pct_below_sma": round(pct_below_sma, 2),
        }

    # SELL -- only if we're in an overbought condition (not just "above SMA20")
    # The exit conditions (price>=SMA, RSI>45) only matter if you hold a position
    # from a previous oversold BUY. Without position tracking, we can't know that,
    # so only signal SELL on genuine overbought conditions.
    if rsi > 75:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": f"RSI overbought at {rsi:.1f} (> 75) -- potential reversal",
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "pct_below_sma": round(pct_below_sma, 2),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"Not extreme oversold (RSI {rsi:.1f}, "
            f"{pct_below_sma:+.1f}% from SMA20)"
        ),
        "price": price,
        "rsi": rsi,
        "sma_20": sma_20,
        "pct_below_sma": round(pct_below_sma, 2),
    }


# ---------------------------------------------------------------------------
# 4. Volume Surge
# ---------------------------------------------------------------------------

def volume_surge_strategy(symbol, ctx=None, df=None):
    """Big volume on crypto usually means something.

    BUY  -- volume > 3x avg AND price up > 3% AND RSI < 65
    EXIT -- volume drops below avg for 2 consecutive periods
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(latest["close"])
    open_price = float(latest["open"])
    rsi = float(latest["rsi"])
    volume = float(latest["volume"])
    vol_avg = float(latest["volume_sma_20"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0
    day_change_pct = ((price - open_price) / open_price * 100) if open_price > 0 else 0

    # BUY conditions
    if vol_ratio > 1.5 and day_change_pct > 1.5 and rsi < 70:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Volume surge {vol_ratio:.1f}x avg, "
                f"price up {day_change_pct:.1f}%, RSI {rsi:.1f}"
            ),
            "price": price,
            "rsi": rsi,
            "volume_ratio": round(vol_ratio, 2),
            "day_change_pct": round(day_change_pct, 2),
        }

    # SELL -- volume dried up for 2 consecutive periods
    prev_vol = float(prev["volume"])
    prev_vol_avg = float(prev["volume_sma_20"]) if float(prev["volume_sma_20"]) > 0 else 1
    prev_vol_ratio = prev_vol / prev_vol_avg

    if vol_ratio < 1.0 and prev_vol_ratio < 1.0:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Volume dried up: {vol_ratio:.1f}x and {prev_vol_ratio:.1f}x avg "
                f"for 2 periods"
            ),
            "price": price,
            "rsi": rsi,
            "volume_ratio": round(vol_ratio, 2),
            "day_change_pct": round(day_change_pct, 2),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No volume surge (vol {vol_ratio:.1f}x, "
            f"day change {day_change_pct:+.1f}%, RSI {rsi:.1f})"
        ),
        "price": price,
        "rsi": rsi,
        "volume_ratio": round(vol_ratio, 2),
        "day_change_pct": round(day_change_pct, 2),
    }


# ---------------------------------------------------------------------------
# Combined Crypto Strategy
# ---------------------------------------------------------------------------

def crypto_combined_strategy(symbol, ctx=None, df=None):
    """Run all four crypto strategies, score them, and return the
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
    # Fetch data once and share across strategies
    if df is None:
        df = get_bars(symbol, limit=200)

    strategies = {
        "btc_correlation": btc_correlation_strategy,
        "trend_following": trend_following_strategy,
        "extreme_oversold": extreme_oversold_strategy,
        "volume_surge": volume_surge_strategy,
    }

    votes = {}
    results = {}
    score = 0

    for name, fn in strategies.items():
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
