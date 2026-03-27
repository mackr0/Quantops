"""Fetch and analyze market data from Alpaca."""

import pandas as pd
import ta
from client import get_api


def get_bars(symbol, timeframe="1Day", limit=200, api=None):
    """Fetch historical bars for a symbol and return as a DataFrame."""
    from datetime import datetime, timedelta
    api = api or get_api()
    # Use date range instead of limit — more reliable on free tier
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(limit * 1.5))).strftime("%Y-%m-%d")
    bars = api.get_bars(symbol, timeframe, start=start, end=end, feed="iex").df
    if not bars.empty:
        bars.index = bars.index.tz_convert("US/Eastern")
    return bars


def get_bars_daterange(symbol, start, end, timeframe="1Day", api=None):
    """
    Fetch historical bars for a symbol within a specific date range.

    Args:
        symbol: Ticker symbol (e.g. 'AAPL').
        start: Start date as ISO-8601 string (e.g. '2025-01-01').
        end: End date as ISO-8601 string (e.g. '2025-12-31').
        timeframe: Bar timeframe (default '1Day').
        api: Optional pre-authenticated Alpaca API client.

    Returns:
        DataFrame with OHLCV data indexed by timestamp.
    """
    api = api or get_api()
    bars = api.get_bars(symbol, timeframe, start=start, end=end, feed="iex").df
    bars.index = bars.index.tz_convert("US/Eastern")
    return bars


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
    """Get the latest quote/trade snapshot for a symbol."""
    api = api or get_api()
    snapshot = api.get_snapshot(symbol)
    return {
        "latest_trade_price": float(snapshot.latest_trade.p),
        "latest_bid": float(snapshot.latest_quote.bp),
        "latest_ask": float(snapshot.latest_quote.ap),
        "daily_bar_close": float(snapshot.daily_bar.c),
        "daily_bar_volume": int(snapshot.daily_bar.v),
    }
