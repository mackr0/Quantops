"""Large Cap ($50-$500) trading strategies.

Tuned for highly liquid, institutional-driven names that move with the market.
Macro and sector rotation matter more than individual stock picking.
Lower volatility = tighter stops work.

SPY data is fetched once and cached at module level (30 min TTL).

Default parameters:
  - stop_loss: 4%
  - take_profit: 6%
  - max_position: 7% of equity
  - min_volume: 1,000,000
  - volume_surge_threshold: 1.5x
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
# 1. Index Correlation Buy
# ---------------------------------------------------------------------------

def index_correlation_strategy(symbol, ctx=None, df=None,
                               spy_rsi_threshold=35, stock_rsi_threshold=40):
    """When SPY bounces from oversold, large caps bounce too.

    BUY  -- SPY RSI < spy_rsi_threshold AND stock RSI < stock_rsi_threshold
    EXIT -- SPY reaches overbought OR stock hits take-profit
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])

    # Check SPY
    spy_df = _get_spy_data()
    spy_rsi = None
    spy_info = "SPY data unavailable"
    if spy_df is not None and not spy_df.empty:
        spy_clean = spy_df.dropna(subset=["rsi"])
        if not spy_clean.empty:
            spy_latest = spy_clean.iloc[-1]
            spy_rsi = float(spy_latest["rsi"])
            spy_info = f"SPY RSI {spy_rsi:.1f}"

    # BUY conditions
    if spy_rsi is not None and spy_rsi < spy_rsi_threshold and rsi < stock_rsi_threshold:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Index correlation: {spy_info} (< 35), "
                f"stock RSI {rsi:.1f} (< 40) -- market bounce setup"
            ),
            "price": price,
            "rsi": rsi,
            "spy_rsi": spy_rsi,
        }

    # SELL -- SPY overbought
    if spy_rsi is not None and spy_rsi > 75:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"SPY overbought (RSI {spy_rsi:.1f} > 75) -- take profits"
            ),
            "price": price,
            "rsi": rsi,
            "spy_rsi": spy_rsi,
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No index correlation trigger ({spy_info}, stock RSI {rsi:.1f})"
        ),
        "price": price,
        "rsi": rsi,
        "spy_rsi": spy_rsi,
    }


# ---------------------------------------------------------------------------
# 2. Relative Strength
# ---------------------------------------------------------------------------

def relative_strength_strategy(symbol, ctx=None, df=None,
                               rsi_ceiling=70):
    """Buy stocks outperforming the market.

    BUY  -- stock up more than SPY over 5 days AND volume > avg AND RSI < rsi_ceiling
    EXIT -- stock underperforms SPY for 3 consecutive days
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    if len(df) < 6:
        return {"symbol": symbol, "signal": "HOLD", "reason": "Not enough data for 5-day comparison"}

    latest = df.iloc[-1]
    five_ago = df.iloc[-6]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    volume = float(latest["volume"])
    vol_avg = float(latest["volume_sma_20"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0

    stock_5d_return = ((price - float(five_ago["close"])) / float(five_ago["close"]) * 100) if float(five_ago["close"]) > 0 else 0

    # Check SPY 5-day return
    spy_df = _get_spy_data()
    spy_5d_return = 0
    if spy_df is not None and not spy_df.empty:
        spy_clean = spy_df.dropna(subset=["rsi"])
        if len(spy_clean) >= 6:
            spy_latest = spy_clean.iloc[-1]
            spy_five_ago = spy_clean.iloc[-6]
            spy_5d_return = ((float(spy_latest["close"]) - float(spy_five_ago["close"])) / float(spy_five_ago["close"]) * 100) if float(spy_five_ago["close"]) > 0 else 0

    outperforming = stock_5d_return > spy_5d_return
    relative_strength = stock_5d_return - spy_5d_return

    # BUY conditions
    if outperforming and vol_ratio > 1.0 and rsi < rsi_ceiling:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Relative strength: stock {stock_5d_return:+.1f}% vs SPY {spy_5d_return:+.1f}% "
                f"(5d), RS {relative_strength:+.1f}%, vol {vol_ratio:.1f}x, RSI {rsi:.1f}"
            ),
            "price": price,
            "rsi": rsi,
            "stock_5d_return": round(stock_5d_return, 2),
            "spy_5d_return": round(spy_5d_return, 2),
            "relative_strength": round(relative_strength, 2),
            "volume_ratio": round(vol_ratio, 2),
        }

    # SELL -- stock underperforming SPY with weak momentum
    if not outperforming and rsi > 60:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Underperforming SPY: stock {stock_5d_return:+.1f}% vs "
                f"SPY {spy_5d_return:+.1f}% (5d), RSI {rsi:.1f}"
            ),
            "price": price,
            "rsi": rsi,
            "stock_5d_return": round(stock_5d_return, 2),
            "spy_5d_return": round(spy_5d_return, 2),
            "relative_strength": round(relative_strength, 2),
            "volume_ratio": round(vol_ratio, 2),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No relative strength trigger (stock {stock_5d_return:+.1f}% vs "
            f"SPY {spy_5d_return:+.1f}%, RSI {rsi:.1f}, vol {vol_ratio:.1f}x)"
        ),
        "price": price,
        "rsi": rsi,
        "stock_5d_return": round(stock_5d_return, 2),
        "spy_5d_return": round(spy_5d_return, 2),
        "relative_strength": round(relative_strength, 2),
        "volume_ratio": round(vol_ratio, 2),
    }


# ---------------------------------------------------------------------------
# 3. Dividend Yield Play (proxy: RSI<35 + price>$50)
# ---------------------------------------------------------------------------

def dividend_yield_strategy(symbol, ctx=None, df=None,
                            rsi_threshold=35):
    """Blue chip oversold bounce proxy.

    BUY  -- RSI < rsi_threshold AND price > $50 (proxy for blue chip / dividend payer)
    EXIT -- RSI > 55
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])

    # BUY -- oversold blue chip
    if rsi < rsi_threshold and price > 50:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"Blue chip oversold: RSI {rsi:.1f} (< 35), "
                f"price ${price:.2f} (> $50) -- dividend yield play"
            ),
            "price": price,
            "rsi": rsi,
        }

    # SELL -- RSI recovered
    if rsi > 55 and price > 50:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": f"RSI recovered to {rsi:.1f} (> 55) -- exit dividend play",
            "price": price,
            "rsi": rsi,
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No dividend play trigger (RSI {rsi:.1f}, price ${price:.2f})"
        ),
        "price": price,
        "rsi": rsi,
    }


# ---------------------------------------------------------------------------
# 4. Moving Average Alignment
# ---------------------------------------------------------------------------

def ma_alignment_strategy(symbol, ctx=None, df=None):
    """All MAs aligned bullishly.

    BUY  -- price > EMA12 > SMA20 > SMA50 AND volume > avg
    EXIT -- price closes below SMA20
    """
    df, err = _prepare_df(symbol, df)
    if err is not None:
        return err

    # Need SMA50 and EMA12
    df = df.dropna(subset=["sma_50", "ema_12"])
    if df.empty:
        return {"symbol": symbol, "signal": "HOLD", "reason": "Not enough data for MA alignment"}

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    ema_12 = float(latest["ema_12"])
    sma_20 = float(latest["sma_20"])
    sma_50 = float(latest["sma_50"])
    volume = float(latest["volume"])
    vol_avg = float(latest["volume_sma_20"])
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0

    # BUY -- perfect alignment
    aligned = price > ema_12 > sma_20 > sma_50
    if aligned and vol_ratio > 1.0:
        return {
            "symbol": symbol,
            "signal": "BUY",
            "reason": (
                f"MA alignment: price ({price:.2f}) > EMA12 ({ema_12:.2f}) > "
                f"SMA20 ({sma_20:.2f}) > SMA50 ({sma_50:.2f}), vol {vol_ratio:.1f}x"
            ),
            "price": price,
            "rsi": rsi,
            "ema_12": ema_12,
            "sma_20": sma_20,
            "sma_50": sma_50,
            "volume_ratio": round(vol_ratio, 2),
        }

    # SELL -- price below SMA20
    if price < sma_20:
        return {
            "symbol": symbol,
            "signal": "SELL",
            "reason": (
                f"Price ({price:.2f}) below SMA20 ({sma_20:.2f}) -- "
                f"alignment broken"
            ),
            "price": price,
            "rsi": rsi,
            "ema_12": ema_12,
            "sma_20": sma_20,
            "sma_50": sma_50,
            "volume_ratio": round(vol_ratio, 2),
        }

    return {
        "symbol": symbol,
        "signal": "HOLD",
        "reason": (
            f"No MA alignment (P:{price:.2f} E12:{ema_12:.2f} "
            f"S20:{sma_20:.2f} S50:{sma_50:.2f}, vol {vol_ratio:.1f}x)"
        ),
        "price": price,
        "rsi": rsi,
        "ema_12": ema_12,
        "sma_20": sma_20,
        "sma_50": sma_50,
        "volume_ratio": round(vol_ratio, 2),
    }


# ---------------------------------------------------------------------------
# Combined Large Cap Strategy
# ---------------------------------------------------------------------------

def large_combined_strategy(symbol, ctx=None, df=None, params=None):
    """Run all four large-cap strategies, score them, and return the
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
    rsi_oversold = float(params.get("rsi_oversold", 35.0))
    rsi_overbought = float(params.get("rsi_overbought", 70.0))

    # Strategy toggles
    use_momentum = bool(params.get("strategy_momentum_breakout", True))
    use_volume_spike = bool(params.get("strategy_volume_spike", True))
    use_mean_reversion = bool(params.get("strategy_mean_reversion", True))

    # Fetch data once and share across strategies
    if df is None:
        df = get_bars(symbol, limit=200)

    strategies = {
        "index_correlation": lambda sym, ctx=ctx, df=None: (
            index_correlation_strategy(sym, ctx=ctx, df=df,
                                       spy_rsi_threshold=rsi_oversold,
                                       stock_rsi_threshold=rsi_oversold + 5)
        ),
        "relative_strength": lambda sym, ctx=ctx, df=None: (
            relative_strength_strategy(sym, ctx=ctx, df=df,
                                       rsi_ceiling=rsi_overbought)
        ),
        "dividend_yield": lambda sym, ctx=ctx, df=None: (
            dividend_yield_strategy(sym, ctx=ctx, df=df,
                                    rsi_threshold=rsi_oversold)
        ),
        "ma_alignment": ma_alignment_strategy,
    }

    # Strategy toggle map
    toggle_map = {
        "index_correlation": use_mean_reversion,
        "relative_strength": use_momentum,
        "dividend_yield": use_volume_spike,
        "ma_alignment": True,  # MA alignment has no user params, always active
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
