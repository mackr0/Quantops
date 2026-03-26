"""Trading strategies for paper trading."""

import pandas as pd
from market_data import get_bars, add_indicators


def sma_crossover_strategy(symbol, short_window=20, long_window=50, limit=100):
    """
    Simple Moving Average crossover strategy.

    Buy signal: short SMA crosses above long SMA
    Sell signal: short SMA crosses below long SMA
    """
    df = get_bars(symbol, limit=limit)
    df[f"sma_{short_window}"] = df["close"].rolling(window=short_window).mean()
    df[f"sma_{long_window}"] = df["close"].rolling(window=long_window).mean()

    df = df.dropna()
    if df.empty:
        return {"symbol": symbol, "signal": "HOLD", "reason": "Not enough data"}

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    short_col = f"sma_{short_window}"
    long_col = f"sma_{long_window}"

    # Crossover detection
    if previous[short_col] <= previous[long_col] and latest[short_col] > latest[long_col]:
        signal = "BUY"
        reason = f"SMA{short_window} crossed above SMA{long_window}"
    elif previous[short_col] >= previous[long_col] and latest[short_col] < latest[long_col]:
        signal = "SELL"
        reason = f"SMA{short_window} crossed below SMA{long_window}"
    else:
        signal = "HOLD"
        if latest[short_col] > latest[long_col]:
            reason = f"SMA{short_window} above SMA{long_window} (bullish trend)"
        else:
            reason = f"SMA{short_window} below SMA{long_window} (bearish trend)"

    return {
        "symbol": symbol,
        "signal": signal,
        "reason": reason,
        "price": float(latest["close"]),
        "sma_short": float(latest[short_col]),
        "sma_long": float(latest[long_col]),
    }


def rsi_strategy(symbol, period=14, oversold=30, overbought=70, limit=100):
    """
    RSI-based mean reversion strategy.

    Buy signal: RSI drops below oversold threshold
    Sell signal: RSI rises above overbought threshold
    """
    df = get_bars(symbol, limit=limit)
    df = add_indicators(df)
    df = df.dropna()

    if df.empty:
        return {"symbol": symbol, "signal": "HOLD", "reason": "Not enough data"}

    latest = df.iloc[-1]
    rsi_value = float(latest["rsi"])

    if rsi_value < oversold:
        signal = "BUY"
        reason = f"RSI ({rsi_value:.1f}) below oversold level ({oversold})"
    elif rsi_value > overbought:
        signal = "SELL"
        reason = f"RSI ({rsi_value:.1f}) above overbought level ({overbought})"
    else:
        signal = "HOLD"
        reason = f"RSI ({rsi_value:.1f}) in neutral zone ({oversold}-{overbought})"

    return {
        "symbol": symbol,
        "signal": signal,
        "reason": reason,
        "price": float(latest["close"]),
        "rsi": rsi_value,
    }


def combined_strategy(symbol, limit=100):
    """
    Combines SMA crossover and RSI for stronger signals.

    Strong BUY: both SMA and RSI say BUY
    Strong SELL: both SMA and RSI say SELL
    Otherwise: HOLD
    """
    sma_result = sma_crossover_strategy(symbol, limit=limit)
    rsi_result = rsi_strategy(symbol, limit=limit)

    if sma_result["signal"] == "BUY" and rsi_result["signal"] == "BUY":
        signal = "STRONG_BUY"
        reason = f"Both signals agree: {sma_result['reason']} + {rsi_result['reason']}"
    elif sma_result["signal"] == "SELL" and rsi_result["signal"] == "SELL":
        signal = "STRONG_SELL"
        reason = f"Both signals agree: {sma_result['reason']} + {rsi_result['reason']}"
    elif sma_result["signal"] == "BUY" or rsi_result["signal"] == "BUY":
        signal = "WEAK_BUY"
        reason = f"SMA: {sma_result['signal']} ({sma_result['reason']}) | RSI: {rsi_result['signal']} ({rsi_result['reason']})"
    elif sma_result["signal"] == "SELL" or rsi_result["signal"] == "SELL":
        signal = "WEAK_SELL"
        reason = f"SMA: {sma_result['signal']} ({sma_result['reason']}) | RSI: {rsi_result['signal']} ({rsi_result['reason']})"
    else:
        signal = "HOLD"
        reason = f"SMA: {sma_result['reason']} | RSI: {rsi_result['reason']}"

    return {
        "symbol": symbol,
        "signal": signal,
        "reason": reason,
        "price": sma_result.get("price"),
        "sma_short": sma_result.get("sma_short"),
        "sma_long": sma_result.get("sma_long"),
        "rsi": rsi_result.get("rsi"),
    }
