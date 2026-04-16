"""Fallback combined strategy used when a market type doesn't match any of
the market-specific strategy engines (micro/small/midcap/largecap/crypto).

Kept for backward compatibility. The live system routes through
strategy_router.py to market-specific engines; this module is only used
as a safety fallback.
"""

import pandas as pd
from market_data import get_bars, add_indicators


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prepare_df(symbol, df, min_rows=25):
    """Fetch data if needed, add indicators, and validate row count.

    Returns (df, error_result) — if error_result is not None the caller
    should return it immediately.
    """
    if df is None:
        df = get_bars(symbol, limit=200)

    df = df.copy()
    # Only add indicators if they're not already present (backtester pre-computes)
    if "rsi" not in df.columns:
        df = add_indicators(df)

    # 20-day high/low and volume averages need at least ~25 rows after NaN drop
    df = df.dropna(subset=["rsi", "sma_20", "volume_sma_20"])

    if len(df) < min_rows:
        return None, {
            "symbol": symbol,
            "signal": "HOLD",
            "reason": f"Not enough data ({len(df)} rows, need {min_rows})",
        }

    return df, None


# ---------------------------------------------------------------------------
# 1. Momentum Breakout
# ---------------------------------------------------------------------------

def momentum_breakout_strategy(symbol, df=None,
                               vol_threshold=1.5, rsi_low=50, rsi_high=80,
                               rsi_overbought=85):
    """Aggressive momentum breakout strategy.

    BUY  — price breaks above 20-day high, volume > vol_threshold x avg, RSI rsi_low-rsi_high
    SELL — price drops below 10-day low OR RSI > rsi_overbought
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
    high_20 = float(latest["high_20"])
    low_10 = float(latest["low_10"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0

    # BUY conditions
    if price > high_20 and vol_ratio > vol_threshold and rsi_low <= rsi <= rsi_high:
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
            "reason": f"Price ({price:.2f}) dropped below 10-day low ({low_10:.2f})",
            "price": price,
            "rsi": rsi,
            "low_10": low_10,
            "volume_ratio": round(vol_ratio, 2),
        }

    if rsi > rsi_overbought:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": f"RSI exhaustion ({rsi:.1f} > {rsi_overbought}) — take profit",
            "price": price,
            "rsi": rsi,
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
# 2. Volume Spike
# ---------------------------------------------------------------------------

def volume_spike_strategy(symbol, df=None,
                          vol_multiplier=2.0, price_change=2.0, rsi_ceiling=70):
    """Volume-based entry strategy.

    BUY  — volume > vol_multiplier x 20-day avg, price up > price_change% on the day, RSI < rsi_ceiling
    SELL — volume below avg AND two consecutive red days (close < open)
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3] if len(df) >= 3 else None

    price = float(latest["close"])
    open_price = float(latest["open"])
    rsi = float(latest["rsi"])
    volume = float(latest["volume"])
    vol_avg = float(latest["volume_sma_20"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0
    day_change_pct = ((price - open_price) / open_price * 100) if open_price > 0 else 0

    # BUY conditions
    if vol_ratio > vol_multiplier and day_change_pct > price_change and rsi < rsi_ceiling:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Volume spike {vol_ratio:.1f}x avg, "
                f"price up {day_change_pct:.1f}% today, RSI {rsi:.1f}"
            ),
            "price": price,
            "rsi": rsi,
            "volume_ratio": round(vol_ratio, 2),
            "day_change_pct": round(day_change_pct, 2),
        }

    # SELL conditions — volume below average AND two consecutive red days
    prev_red = float(prev["close"]) < float(prev["open"])
    latest_red = price < open_price
    below_avg_vol = vol_ratio < 1.0

    if below_avg_vol and latest_red and prev_red:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Volume fading ({vol_ratio:.1f}x avg) with "
                f"2 consecutive red days — momentum stalled"
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
            f"No volume spike trigger (vol {vol_ratio:.1f}x, "
            f"day change {day_change_pct:+.1f}%, RSI {rsi:.1f})"
        ),
        "price": price,
        "rsi": rsi,
        "volume_ratio": round(vol_ratio, 2),
        "day_change_pct": round(day_change_pct, 2),
    }


# ---------------------------------------------------------------------------
# 3. Mean Reversion Aggressive
# ---------------------------------------------------------------------------

def mean_reversion_aggressive(symbol, df=None,
                              rsi_threshold=25, sma_distance=-10):
    """Aggressive oversold bounce play.

    BUY  — RSI < rsi_threshold AND price > sma_distance% below 20-day SMA
    SELL — price returns to 20-day SMA OR RSI > 60
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    sma_20 = float(latest["sma_20"])
    pct_below_sma = ((price - sma_20) / sma_20 * 100) if sma_20 > 0 else 0

    # BUY — deeply oversold
    if rsi < rsi_threshold and pct_below_sma < sma_distance:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Oversold bounce: RSI {rsi:.1f} (< 25), "
                f"price {pct_below_sma:.1f}% below SMA20 ({sma_20:.2f})"
            ),
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "pct_below_sma": round(pct_below_sma, 2),
        }

    # SELL — price recovered to SMA or RSI normalized
    if price >= sma_20:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Price ({price:.2f}) returned to SMA20 ({sma_20:.2f}) — "
                f"mean reversion target hit"
            ),
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "pct_below_sma": round(pct_below_sma, 2),
        }

    if rsi > 60:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": f"RSI recovered to {rsi:.1f} (> 60) — exit bounce trade",
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "pct_below_sma": round(pct_below_sma, 2),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"Not oversold enough (RSI {rsi:.1f}, "
            f"{pct_below_sma:+.1f}% from SMA20)"
        ),
        "price": price,
        "rsi": rsi,
        "sma_20": sma_20,
        "pct_below_sma": round(pct_below_sma, 2),
    }


# ---------------------------------------------------------------------------
# 4. Gap and Go
# ---------------------------------------------------------------------------

def gap_and_go_strategy(symbol, df=None,
                        gap_threshold=3.0, breakout_vol_threshold=1.0):
    """Gap-up momentum strategy.

    BUY  — today's open > gap_threshold% above yesterday's close AND volume above avg
    SELL — price drops below today's open (gap fill = exit)
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    price = float(latest["close"])
    today_open = float(latest["open"])
    prev_close = float(prev["close"])
    volume = float(latest["volume"])
    vol_avg = float(latest["volume_sma_20"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0
    gap_pct = ((today_open - prev_close) / prev_close * 100) if prev_close > 0 else 0

    # BUY — gap up with volume confirmation
    if gap_pct > gap_threshold and vol_ratio > breakout_vol_threshold:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Gap up {gap_pct:.1f}% (open {today_open:.2f} vs "
                f"prev close {prev_close:.2f}), volume {vol_ratio:.1f}x avg"
            ),
            "price": price,
            "gap_pct": round(gap_pct, 2),
            "today_open": today_open,
            "prev_close": prev_close,
            "volume_ratio": round(vol_ratio, 2),
        }

    # SELL — gap fill (price fell back below today's open after a gap up)
    if gap_pct > gap_threshold and price < today_open:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Gap fill: price ({price:.2f}) dropped below today's open "
                f"({today_open:.2f}) after {gap_pct:.1f}% gap — exit"
            ),
            "price": price,
            "gap_pct": round(gap_pct, 2),
            "today_open": today_open,
            "prev_close": prev_close,
            "volume_ratio": round(vol_ratio, 2),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No significant gap (open gap {gap_pct:+.1f}%, "
            f"vol {vol_ratio:.1f}x avg)"
        ),
        "price": price,
        "gap_pct": round(gap_pct, 2),
        "today_open": today_open,
        "prev_close": prev_close,
        "volume_ratio": round(vol_ratio, 2),
    }


# ---------------------------------------------------------------------------
# 5. Aggressive Combined (master strategy)
# ---------------------------------------------------------------------------

def fallback_combined_strategy(symbol, df=None, params=None):
    """Run all four aggressive strategies, score them, and return the
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
    rsi_oversold = float(params.get("rsi_oversold", 25.0))
    rsi_overbought = float(params.get("rsi_overbought", 85.0))
    volume_surge_mult = float(params.get("volume_surge_multiplier", 2.0))
    breakout_vol_threshold = float(params.get("breakout_volume_threshold", 1.0))
    gap_threshold = float(params.get("gap_pct_threshold", 3.0))

    # Strategy toggles
    use_momentum = bool(params.get("strategy_momentum_breakout", True))
    use_volume_spike = bool(params.get("strategy_volume_spike", True))
    use_mean_reversion = bool(params.get("strategy_mean_reversion", True))
    use_gap_and_go = bool(params.get("strategy_gap_and_go", True))

    # Fetch data once and share across strategies
    if df is None:
        df = get_bars(symbol, limit=200)

    strategies = {
        "momentum_breakout": lambda sym, df=None: (
            momentum_breakout_strategy(sym, df=df,
                                       vol_threshold=breakout_vol_threshold + 0.5,
                                       rsi_low=50, rsi_high=80,
                                       rsi_overbought=rsi_overbought)
        ),
        "volume_spike": lambda sym, df=None: (
            volume_spike_strategy(sym, df=df,
                                  vol_multiplier=volume_surge_mult,
                                  price_change=2.0,
                                  rsi_ceiling=70)
        ),
        "mean_reversion": lambda sym, df=None: (
            mean_reversion_aggressive(sym, df=df,
                                      rsi_threshold=rsi_oversold,
                                      sma_distance=-10)
        ),
        "gap_and_go": lambda sym, df=None: (
            gap_and_go_strategy(sym, df=df,
                                gap_threshold=gap_threshold,
                                breakout_vol_threshold=breakout_vol_threshold)
        ),
    }

    # Strategy toggle map
    toggle_map = {
        "momentum_breakout": use_momentum,
        "volume_spike": use_volume_spike,
        "mean_reversion": use_mean_reversion,
        "gap_and_go": use_gap_and_go,
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
        result = fn(symbol, df=df.copy())
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
