"""Fetch and analyze market data from Alpaca."""

import pandas as pd
import ta
from client import get_api


def get_bars(symbol, timeframe="1Day", limit=100, api=None):
    """Fetch historical bars for a symbol and return as a DataFrame."""
    api = api or get_api()
    bars = api.get_bars(symbol, timeframe, limit=limit).df
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
