"""Micro Cap ($1-$5) trading strategies.

Tuned for extreme volatility, low liquidity, penny-stock behavior.
These stocks are catalyst-driven and can 2x or lose 50% in a day.
Strong filters are essential to avoid death traps.

Default parameters:
  - stop_loss: 10%
  - take_profit: 15%
  - max_position: 5% of equity
  - min_volume: 100,000
  - volume_surge_threshold: 5x
"""

import pandas as pd
from market_data import get_bars, add_indicators


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
# 1. Volume Explosion
# ---------------------------------------------------------------------------

def volume_explosion_strategy(symbol, ctx=None, df=None):
    """Volume explosion strategy for micro-caps.

    BUY  -- volume > 5x 20-day avg AND price up > 5% on the day AND RSI < 75
    EXIT -- volume drops below 2x avg (catalyst fading)
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    price = float(latest["close"])
    open_price = float(latest["open"])
    rsi = float(latest["rsi"])
    volume = float(latest["volume"])
    vol_avg = float(latest["volume_sma_20"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0
    day_change_pct = ((price - open_price) / open_price * 100) if open_price > 0 else 0

    # BUY conditions
    if vol_ratio > 5.0 and day_change_pct > 5.0 and rsi < 75:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Volume explosion {vol_ratio:.1f}x avg, "
                f"price up {day_change_pct:.1f}% today, RSI {rsi:.1f}"
            ),
            "price": price,
            "rsi": rsi,
            "volume_ratio": round(vol_ratio, 2),
            "day_change_pct": round(day_change_pct, 2),
        }

    # SELL conditions -- catalyst fading
    if vol_ratio < 2.0 and rsi > 60:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Catalyst fading: volume {vol_ratio:.1f}x avg (below 2x), "
                f"RSI {rsi:.1f}"
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
            f"No volume explosion (vol {vol_ratio:.1f}x, "
            f"day change {day_change_pct:+.1f}%, RSI {rsi:.1f})"
        ),
        "price": price,
        "rsi": rsi,
        "volume_ratio": round(vol_ratio, 2),
        "day_change_pct": round(day_change_pct, 2),
    }


# ---------------------------------------------------------------------------
# 2. Penny Reversal
# ---------------------------------------------------------------------------

def penny_reversal_strategy(symbol, ctx=None, df=None):
    """Deep oversold bounce for micro-caps.

    BUY  -- RSI < 20 AND price > 20% below 10-day SMA AND volume increasing
    EXIT -- price returns to 10-day SMA OR RSI > 50
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    # Compute SMA10
    df["sma_10"] = df["close"].rolling(window=10).mean()
    df = df.dropna(subset=["sma_10"])
    if df.empty:
        return {"symbol": symbol, "signal": "HOLD", "reason": "Not enough data for SMA10"}

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    sma_10 = float(latest["sma_10"])
    pct_below_sma10 = ((price - sma_10) / sma_10 * 100) if sma_10 > 0 else 0
    volume = float(latest["volume"])
    prev_volume = float(prev["volume"])
    vol_increasing = volume > prev_volume

    # BUY -- deeply oversold penny bounce
    if rsi < 20 and pct_below_sma10 < -20 and vol_increasing:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Penny reversal: RSI {rsi:.1f} (< 20), "
                f"price {pct_below_sma10:.1f}% below SMA10, volume increasing"
            ),
            "price": price,
            "rsi": rsi,
            "sma_10": sma_10,
            "pct_below_sma10": round(pct_below_sma10, 2),
        }

    # SELL -- price recovered to SMA10 or RSI normalized
    if price >= sma_10:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Price ({price:.2f}) returned to SMA10 ({sma_10:.2f}) -- "
                f"reversal target hit"
            ),
            "price": price,
            "rsi": rsi,
            "sma_10": sma_10,
            "pct_below_sma10": round(pct_below_sma10, 2),
        }

    if rsi > 50:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": f"RSI recovered to {rsi:.1f} (> 50) -- exit reversal trade",
            "price": price,
            "rsi": rsi,
            "sma_10": sma_10,
            "pct_below_sma10": round(pct_below_sma10, 2),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"Not oversold enough for penny reversal (RSI {rsi:.1f}, "
            f"{pct_below_sma10:+.1f}% from SMA10)"
        ),
        "price": price,
        "rsi": rsi,
        "sma_10": sma_10,
        "pct_below_sma10": round(pct_below_sma10, 2),
    }


# ---------------------------------------------------------------------------
# 3. Breakout Above Resistance
# ---------------------------------------------------------------------------

def breakout_resistance_strategy(symbol, ctx=None, df=None):
    """Breakout above resistance for micro-caps.

    BUY  -- price > 10-day high AND volume > 3x avg
    EXIT -- price drops below breakout level (failed breakout)
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    df["high_10"] = df["high"].rolling(window=10).max()
    df["low_5"] = df["low"].rolling(window=5).min()
    df = df.dropna(subset=["high_10", "low_5"])

    if df.empty:
        return {"symbol": symbol, "signal": "HOLD", "reason": "Not enough data for rolling windows"}

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    volume = float(latest["volume"])
    vol_avg = float(latest["volume_sma_20"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0
    high_10 = float(latest["high_10"])
    low_5 = float(latest["low_5"])

    # BUY -- breakout above 10-day high with volume
    if price > high_10 and vol_ratio > 3.0:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Breakout above 10-day high ({high_10:.2f}), "
                f"volume {vol_ratio:.1f}x avg"
            ),
            "price": price,
            "rsi": rsi,
            "high_10": high_10,
            "volume_ratio": round(vol_ratio, 2),
        }

    # SELL -- failed breakout (price dropped below recent support)
    if price < low_5:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Failed breakout: price ({price:.2f}) below 5-day low ({low_5:.2f})"
            ),
            "price": price,
            "rsi": rsi,
            "high_10": high_10,
            "low_5": low_5,
            "volume_ratio": round(vol_ratio, 2),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No breakout (price {price:.2f} vs 10d-high {high_10:.2f}), "
            f"vol {vol_ratio:.1f}x"
        ),
        "price": price,
        "rsi": rsi,
        "high_10": high_10,
        "volume_ratio": round(vol_ratio, 2),
    }


# ---------------------------------------------------------------------------
# 4. Avoid Traps (filter)
# ---------------------------------------------------------------------------

def avoid_traps_filter(symbol, ctx=None, df=None):
    """Filter OUT micro-cap death traps.

    SELL if: average volume < 100K (too illiquid),
             price declining for 10+ consecutive days (falling knife).
    Otherwise HOLD (neutral -- does not generate BUY signals).
    """
    df, err = _prepare_df(symbol, df, min_rows=15)
    if err is not None:
        return err

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    vol_avg = float(latest["volume_sma_20"])

    # Check for illiquidity
    if vol_avg < 100_000:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Trap filter: avg volume {vol_avg:,.0f} < 100K (too illiquid)"
            ),
            "price": price,
            "rsi": rsi,
            "volume_avg": vol_avg,
        }

    # Check for falling knife (10+ consecutive red days)
    recent = df.tail(10)
    consecutive_red = 0
    for _, row in recent.iterrows():
        if float(row["close"]) < float(row["open"]):
            consecutive_red += 1
        else:
            consecutive_red = 0

    if consecutive_red >= 10:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Trap filter: {consecutive_red} consecutive red days (falling knife)"
            ),
            "price": price,
            "rsi": rsi,
            "consecutive_red": consecutive_red,
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": f"Passed trap filters (vol avg {vol_avg:,.0f}, no falling knife)",
        "price": price,
        "rsi": rsi,
        "volume_avg": vol_avg,
    }


# ---------------------------------------------------------------------------
# Combined Micro Cap Strategy
# ---------------------------------------------------------------------------

def micro_combined_strategy(symbol, ctx=None, df=None):
    """Run all four micro-cap strategies, score them, and return the
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
        "volume_explosion": volume_explosion_strategy,
        "penny_reversal": penny_reversal_strategy,
        "breakout_resistance": breakout_resistance_strategy,
        "avoid_traps": avoid_traps_filter,
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
