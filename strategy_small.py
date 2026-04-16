"""Small Cap ($5-$20) trading strategies.

Tuned for volatile but more established names. Sector-sensitive with real
earnings and fundamentals. Mean reversion works better here than in micro-caps.

Default parameters:
  - stop_loss: 6%
  - take_profit: 8%
  - max_position: 8% of equity
  - min_volume: 300,000
  - volume_surge_threshold: 3x
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
# 1. Mean Reversion
# ---------------------------------------------------------------------------

def mean_reversion_strategy(symbol, ctx=None, df=None,
                            rsi_threshold=28, sma_distance=-8):
    """Classic oversold bounce for small caps.

    BUY  -- RSI < rsi_threshold AND price > sma_distance% below 20-day SMA
    EXIT -- price returns to 20-day SMA OR RSI > 55
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    sma_20 = float(latest["sma_20"])
    pct_below_sma = ((price - sma_20) / sma_20 * 100) if sma_20 > 0 else 0

    # BUY -- deeply oversold
    if rsi < rsi_threshold and pct_below_sma < sma_distance:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Mean reversion: RSI {rsi:.1f} (< 28), "
                f"price {pct_below_sma:.1f}% below SMA20 ({sma_20:.2f})"
            ),
            "price": price,
            "rsi": rsi,
            "sma_20": sma_20,
            "pct_below_sma": round(pct_below_sma, 2),
        }

    # SELL -- price recovered to SMA or RSI normalized
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
# 2. Volume Spike Entry
# ---------------------------------------------------------------------------

def volume_spike_entry_strategy(symbol, ctx=None, df=None,
                                vol_multiplier=3.0, price_change=3.0):
    """Institutional interest or catalyst detection.

    BUY  -- volume > vol_multiplier x 20-day avg AND price up > price_change% AND RSI 30-65
    EXIT -- 2 consecutive red days with declining volume
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
    if vol_ratio > vol_multiplier and day_change_pct > price_change and 30 <= rsi <= 65:
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

    # SELL -- 2 consecutive red days with declining volume
    prev_red = float(prev["close"]) < float(prev["open"])
    latest_red = price < open_price
    prev_vol = float(prev["volume"])
    vol_declining = volume < prev_vol

    if latest_red and prev_red and vol_declining:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"2 consecutive red days with declining volume -- momentum stalled"
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
# 3. Gap and Go
# ---------------------------------------------------------------------------

def gap_and_go_strategy(symbol, ctx=None, df=None,
                        gap_threshold=2.5):
    """Opening gaps with momentum follow-through.

    BUY  -- open > gap_threshold% above previous close AND volume > 1.5x avg
    EXIT -- price drops below today's open (gap fill)
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

    # BUY -- gap up with volume confirmation
    if gap_pct > gap_threshold and vol_ratio > 1.5:
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

    # SELL -- gap fill
    if gap_pct > gap_threshold and price < today_open:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Gap fill: price ({price:.2f}) dropped below today's open "
                f"({today_open:.2f}) after {gap_pct:.1f}% gap"
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
# 4. Momentum Continuation
# ---------------------------------------------------------------------------

def momentum_continuation_strategy(symbol, ctx=None, df=None,
                                   rsi_low=50, rsi_high=70):
    """Riding established uptrends.

    BUY  -- price above 20-day SMA AND SMA20 slope positive AND RSI rsi_low-rsi_high
            AND volume > avg
    EXIT -- price closes below 20-day SMA
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

    # Compute SMA20 slope (compare current vs 5 bars ago)
    if len(df) >= 6:
        sma_20_prev = float(df.iloc[-6]["sma_20"])
        sma_slope_positive = sma_20 > sma_20_prev
    else:
        sma_slope_positive = False

    above_sma = price > sma_20
    rsi_in_range = rsi_low <= rsi <= rsi_high
    vol_above_avg = vol_ratio > 1.0

    # BUY conditions
    if above_sma and sma_slope_positive and rsi_in_range and vol_above_avg:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Momentum continuation: above SMA20 ({sma_20:.2f}), "
                f"slope positive, RSI {rsi:.1f}, vol {vol_ratio:.1f}x"
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
            f"No momentum continuation (above SMA20: {above_sma}, "
            f"slope+: {sma_slope_positive}, RSI {rsi:.1f}, vol {vol_ratio:.1f}x)"
        ),
        "price": price,
        "rsi": rsi,
        "sma_20": sma_20,
        "volume_ratio": round(vol_ratio, 2),
    }


# ---------------------------------------------------------------------------
# Combined Small Cap Strategy
# ---------------------------------------------------------------------------

def small_combined_strategy(symbol, ctx=None, df=None, params=None):
    """Run all four small-cap strategies, score them, and return the
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
    rsi_oversold = float(params.get("rsi_oversold", 28.0))
    volume_surge_mult = float(params.get("volume_surge_multiplier", 3.0))
    gap_threshold = float(params.get("gap_pct_threshold", 2.5))

    # Strategy toggles
    use_mean_reversion = bool(params.get("strategy_mean_reversion", True))
    use_volume_spike = bool(params.get("strategy_volume_spike", True))
    use_gap_and_go = bool(params.get("strategy_gap_and_go", True))
    use_momentum = bool(params.get("strategy_momentum_breakout", True))

    # Fetch data once and share across strategies
    if df is None:
        df = get_bars(symbol, limit=200)

    strategies = {
        "mean_reversion": lambda sym, ctx=ctx, df=None: (
            mean_reversion_strategy(sym, ctx=ctx, df=df,
                                    rsi_threshold=rsi_oversold,
                                    sma_distance=-8)
        ),
        "volume_spike_entry": lambda sym, ctx=ctx, df=None: (
            volume_spike_entry_strategy(sym, ctx=ctx, df=df,
                                        vol_multiplier=volume_surge_mult,
                                        price_change=3.0)
        ),
        "gap_and_go": lambda sym, ctx=ctx, df=None: (
            gap_and_go_strategy(sym, ctx=ctx, df=df,
                                gap_threshold=gap_threshold)
        ),
        "momentum_continuation": lambda sym, ctx=ctx, df=None: (
            momentum_continuation_strategy(sym, ctx=ctx, df=df,
                                           rsi_low=50, rsi_high=70)
        ),
    }

    # Strategy toggle map
    toggle_map = {
        "mean_reversion": use_mean_reversion,
        "volume_spike_entry": use_volume_spike,
        "gap_and_go": use_gap_and_go,
        "momentum_continuation": use_momentum,
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
