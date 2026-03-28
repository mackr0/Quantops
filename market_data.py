"""Fetch and analyze market data using Yahoo Finance (yfinance)."""

import pandas as pd
import ta
import yfinance as yf


def get_bars(symbol, timeframe="1Day", limit=200, api=None):
    """Fetch historical bars for a symbol and return as a DataFrame.

    Uses yfinance instead of Alpaca. The ``api`` parameter is ignored
    (kept for backward compatibility).
    """
    # Map limit (trading days) to a yfinance period string
    if limit <= 5:
        period = "5d"
    elif limit <= 22:
        period = "1mo"
    elif limit <= 66:
        period = "3mo"
    elif limit <= 132:
        period = "6mo"
    elif limit <= 252:
        period = "1y"
    elif limit <= 504:
        period = "2y"
    else:
        period = "5y"

    # Convert crypto symbols: "BTC/USD" -> "BTC-USD" for yfinance
    yf_symbol = symbol.replace("/", "-") if "/" in symbol else symbol
    ticker = yf.Ticker(yf_symbol)
    df = ticker.history(period=period, auto_adjust=True)

    if df.empty:
        return df

    # Rename columns to lowercase to match the rest of the codebase
    df.columns = [c.lower() for c in df.columns]

    # Keep only OHLCV columns
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep]

    # Ensure timezone-aware index (yfinance usually returns tz-aware already)
    if df.index.tz is None:
        df.index = df.index.tz_localize("US/Eastern")
    else:
        df.index = df.index.tz_convert("US/Eastern")

    return df


def get_bars_daterange(symbol, start, end, timeframe="1Day", api=None):
    """Fetch historical bars for a symbol within a specific date range.

    Args:
        symbol: Ticker symbol (e.g. 'AAPL').
        start: Start date as ISO-8601 string (e.g. '2025-01-01').
        end: End date as ISO-8601 string (e.g. '2025-12-31').
        timeframe: Bar timeframe (default '1Day') — currently ignored; daily only.
        api: Ignored (kept for backward compatibility).

    Returns:
        DataFrame with OHLCV data indexed by timestamp.
    """
    yf_symbol = symbol.replace("/", "-") if "/" in symbol else symbol
    ticker = yf.Ticker(yf_symbol)
    df = ticker.history(start=start, end=end, auto_adjust=True)

    if df.empty:
        return df

    df.columns = [c.lower() for c in df.columns]

    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep]

    if df.index.tz is None:
        df.index = df.index.tz_localize("US/Eastern")
    else:
        df.index = df.index.tz_convert("US/Eastern")

    return df


def add_indicators(df):
    """Add common technical indicators to a price DataFrame."""
    # Moving averages
    df["sma_20"] = ta.trend.sma_indicator(df["close"], window=20)
    df["sma_50"] = ta.trend.sma_indicator(df["close"], window=50)
    df["ema_12"] = ta.trend.ema_indicator(df["close"], window=12)

    # RSI
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    # MACD
    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_histogram"] = macd.macd_diff()

    # Bollinger Bands
    bollinger = ta.volatility.BollingerBands(df["close"])
    df["bb_upper"] = bollinger.bollinger_hband()
    df["bb_lower"] = bollinger.bollinger_lband()
    df["bb_middle"] = bollinger.bollinger_mavg()

    # Volume moving average
    df["volume_sma_20"] = ta.trend.sma_indicator(df["volume"].astype(float), window=20)

    return df


def get_snapshot(symbol, api=None):
    """Get the latest quote/trade snapshot for a symbol using yfinance.

    The ``api`` parameter is ignored (kept for backward compatibility).
    """
    yf_symbol = symbol.replace("/", "-") if "/" in symbol else symbol
    ticker = yf.Ticker(yf_symbol)
    info = ticker.fast_info

    # Get the most recent 1-day bar for volume
    hist = ticker.history(period="2d", auto_adjust=True)

    latest_price = float(info.last_price) if hasattr(info, "last_price") else 0.0
    prev_close = float(info.previous_close) if hasattr(info, "previous_close") else latest_price

    daily_volume = 0
    if not hist.empty:
        daily_volume = int(hist["Volume"].iloc[-1])

    return {
        "latest_trade_price": latest_price,
        "latest_bid": latest_price,   # yfinance free data doesn't provide live bid/ask
        "latest_ask": latest_price,
        "daily_bar_close": prev_close,
        "daily_bar_volume": daily_volume,
    }
