"""Fetch and analyze market data.

Primary source: Alpaca's Market Data API (reliable SIP feed, full volume,
authenticated rate limits up to 10k req/min on Algo Trader Plus).

Fallback: yfinance for crypto (no Alpaca crypto bars via the equity
market data endpoint) and any symbol Alpaca returns empty for.

Previously this module was 100% yfinance, which hung the screener for
30+ minutes at market open due to Yahoo throttling. 2026-04-15 migration.
"""

import logging
import os
from datetime import datetime, timedelta

import pandas as pd
import ta
import yfinance as yf

logger = logging.getLogger(__name__)

# Module-level cache of one Alpaca client. We intentionally use the main
# .env creds for market data — the subscription is shared across all
# paper accounts under the same Alpaca login.
_alpaca_data_client = None


def _get_alpaca_data_client():
    """Return a cached REST client for Alpaca market data. None on failure."""
    global _alpaca_data_client
    if _alpaca_data_client is not None:
        return _alpaca_data_client
    try:
        from alpaca_trade_api import REST
        key = os.getenv("ALPACA_API_KEY", "")
        secret = os.getenv("ALPACA_SECRET_KEY", "")
        base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        if not key or not secret:
            return None
        _alpaca_data_client = REST(key, secret, base_url)
        return _alpaca_data_client
    except Exception as exc:
        logger.debug("Alpaca data client init failed: %s", exc)
        return None


def _limit_to_days(limit):
    """Bar-count → calendar-day lookback. Over-request a bit to account for
    weekends / holidays so we always return at least `limit` rows."""
    if limit <= 5:
        return 10
    if limit <= 22:
        return 35
    if limit <= 66:
        return 100
    if limit <= 132:
        return 200
    if limit <= 252:
        return 370
    if limit <= 504:
        return 730
    return 1825


def _fetch_via_alpaca(symbol, limit):
    """Try to fetch daily bars from Alpaca. Returns DataFrame or None."""
    if "/" in symbol:
        # Crypto symbols use the equity endpoint only as "BTCUSD"; easier
        # to let yfinance handle crypto.
        return None
    client = _get_alpaca_data_client()
    if client is None:
        return None
    try:
        days = _limit_to_days(limit)
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        end = datetime.utcnow().strftime("%Y-%m-%d")
        bars = client.get_bars(symbol, "1Day", start=start, end=end,
                               adjustment="all").df
        if bars is None or bars.empty:
            return None
        # Alpaca returns columns: open, high, low, close, volume, trade_count, vwap
        keep = [c for c in ("open", "high", "low", "close", "volume")
                if c in bars.columns]
        bars = bars[keep]
        # Ensure US/Eastern tz index (matches yfinance convention)
        if bars.index.tz is None:
            bars.index = bars.index.tz_localize("UTC")
        bars.index = bars.index.tz_convert("US/Eastern")
        # Only keep the last N bars (over-fetched for safety)
        return bars.tail(limit)
    except Exception as exc:
        logger.debug("Alpaca bar fetch failed for %s: %s", symbol, exc)
        return None


def _fetch_via_yfinance(symbol, limit):
    """Fallback: fetch via yfinance. Same DataFrame shape as _fetch_via_alpaca."""
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
    yf_symbol = symbol.replace("/", "-") if "/" in symbol else symbol
    try:
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period=period, auto_adjust=True)
    except Exception:
        return pd.DataFrame()
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


def get_bars(symbol, timeframe="1Day", limit=200, api=None):
    """Fetch historical bars for a symbol and return as a DataFrame.

    Tries Alpaca's reliable SIP feed first, falls back to yfinance for
    crypto + any symbol Alpaca returns empty. Returns DataFrame with
    lowercase OHLCV columns and US/Eastern tz-aware index.

    The ``timeframe`` and ``api`` parameters are kept for backward
    compatibility; we only support daily bars at the moment.
    """
    # Crypto → straight to yfinance (the equity data endpoint doesn't
    # serve crypto; Alpaca has a separate crypto endpoint we could wire
    # up later if needed)
    if "/" in symbol:
        return _fetch_via_yfinance(symbol, limit)

    # Primary: Alpaca
    bars = _fetch_via_alpaca(symbol, limit)
    if bars is not None and not bars.empty:
        return bars

    # Fallback: yfinance
    return _fetch_via_yfinance(symbol, limit)


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

    # --- Advanced indicators (feed richer data to AI) ---

    # ATR — Average True Range (volatility measure)
    df["atr_14"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)

    # ADX — Average Directional Index (trend strength, >25 = trending)
    df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)

    # OBV — On-Balance Volume (accumulation/distribution)
    df["obv"] = ta.volume.on_balance_volume(df["close"], df["volume"])

    # Stochastic RSI (0-100, more sensitive than RSI)
    stoch = ta.momentum.StochRSIIndicator(df["close"], window=14)
    df["stoch_rsi"] = stoch.stochrsi() * 100

    # Rate of Change 10-period (momentum %)
    df["roc_10"] = ta.momentum.roc(df["close"], window=10)

    # 52-week context (using available data)
    high_252 = df["high"].rolling(min(252, len(df))).max()
    low_252 = df["low"].rolling(min(252, len(df))).min()
    df["pct_from_52w_high"] = ((df["close"] - high_252) / high_252 * 100)
    df["pct_from_52w_low"] = ((df["close"] - low_252) / low_252 * 100)

    # --- Institutional Money Flow ---

    # MFI — Money Flow Index (volume-weighted RSI, shows institutional buying/selling)
    df["mfi"] = ta.volume.money_flow_index(df["high"], df["low"], df["close"],
                                            df["volume"], window=14)

    # CMF — Chaikin Money Flow (positive = accumulation, negative = distribution)
    df["cmf"] = ta.volume.chaikin_money_flow(df["high"], df["low"], df["close"],
                                              df["volume"], window=20)

    # A/D Line — Accumulation/Distribution (running total of money flow)
    df["ad_line"] = ta.volume.acc_dist_index(df["high"], df["low"], df["close"],
                                              df["volume"])

    # --- Volatility Squeeze (Bollinger inside Keltner = big move coming) ---

    keltner_high = ta.volatility.keltner_channel_hband(df["high"], df["low"],
                                                        df["close"], window=20)
    keltner_low = ta.volatility.keltner_channel_lband(df["high"], df["low"],
                                                       df["close"], window=20)
    # Squeeze = BBands inside Keltner Channels
    df["squeeze"] = ((df["bb_upper"] < keltner_high) &
                     (df["bb_lower"] > keltner_low)).astype(int)

    # --- Support / Resistance (Pivot Points) ---
    prev_high = df["high"].shift(1)
    prev_low = df["low"].shift(1)
    prev_close = df["close"].shift(1)
    pivot = (prev_high + prev_low + prev_close) / 3
    df["pivot"] = pivot
    df["resistance_1"] = 2 * pivot - prev_low
    df["support_1"] = 2 * pivot - prev_high

    # --- Fibonacci Retracement (from recent 20-day swing) ---
    swing_high = df["high"].rolling(20).max()
    swing_low = df["low"].rolling(20).min()
    swing_range = swing_high - swing_low
    df["fib_382"] = swing_high - swing_range * 0.382
    df["fib_500"] = swing_high - swing_range * 0.500
    df["fib_618"] = swing_high - swing_range * 0.618

    # Distance from nearest fib level
    last_close = df["close"]
    fib_dist_382 = abs(last_close - df["fib_382"]) / last_close * 100
    fib_dist_500 = abs(last_close - df["fib_500"]) / last_close * 100
    fib_dist_618 = abs(last_close - df["fib_618"]) / last_close * 100
    df["nearest_fib_dist"] = pd.concat([fib_dist_382, fib_dist_500, fib_dist_618],
                                        axis=1).min(axis=1)

    # --- Gap Analysis (unfilled gaps from last 5 days) ---
    df["gap_pct"] = ((df["open"] - df["close"].shift(1)) / df["close"].shift(1) * 100)

    # --- VWAP (intraday proxy using daily data) ---
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_tp_vol = (typical_price * df["volume"]).rolling(20).sum()
    cumulative_vol = df["volume"].rolling(20).sum()
    df["vwap_20"] = cumulative_tp_vol / cumulative_vol
    df["pct_from_vwap"] = ((df["close"] - df["vwap_20"]) / df["vwap_20"] * 100)

    return df


# ---------------------------------------------------------------------------
# Sector rotation tracking
# ---------------------------------------------------------------------------

import time as _time
import logging as _md_logger

_sector_cache = {}
_SECTOR_TTL = 1800  # 30 minutes

# Sector ETFs mapped to sectors
SECTOR_ETFS = {
    "tech": "XLK",
    "finance": "XLF",
    "energy": "XLE",
    "healthcare": "XLV",
    "industrial": "XLI",
    "consumer_disc": "XLY",
    "consumer_staples": "XLP",
    "utilities": "XLU",
    "materials": "XLB",
    "real_estate": "XLRE",
    "comm_services": "XLC",
}


def get_sector_rotation():
    """Track sector rotation — which sectors are money flowing into/out of.

    Returns dict: {sector: {etf, return_5d, return_20d, trend}}
    Cached 30 minutes.
    """
    now = _time.time()
    if _sector_cache.get("data") and (now - _sector_cache.get("ts", 0)) < _SECTOR_TTL:
        return _sector_cache["data"]

    try:
        symbols = list(SECTOR_ETFS.values())
        data = yf.download(" ".join(symbols), period="1mo", progress=False,
                           auto_adjust=True, threads=True)
        if data.empty:
            return {}

        result = {}
        for sector, etf in SECTOR_ETFS.items():
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    close = data["Close"][etf].dropna()
                else:
                    close = data["Close"].dropna()

                if len(close) < 20:
                    continue

                ret_5d = float((close.iloc[-1] / close.iloc[-5] - 1) * 100)
                ret_20d = float((close.iloc[-1] / close.iloc[0] - 1) * 100)
                trend = "inflow" if ret_5d > 1 else "outflow" if ret_5d < -1 else "flat"

                result[sector] = {
                    "etf": etf,
                    "return_5d": round(ret_5d, 2),
                    "return_20d": round(ret_20d, 2),
                    "trend": trend,
                }
            except Exception:
                continue

        _sector_cache["data"] = result
        _sector_cache["ts"] = now
        return result

    except Exception as exc:
        _md_logger.getLogger(__name__).warning("Sector rotation fetch failed: %s", exc)
        return {}


def get_relative_strength_vs_sector(symbol, sector=None):
    """Compare a stock's 5-day return to its sector ETF.

    Returns dict: {sector, stock_5d, sector_5d, relative_strength}
    """
    # Auto-detect sector from a rough mapping
    if sector is None:
        sector = _guess_sector(symbol)

    rotation = get_sector_rotation()
    sector_data = rotation.get(sector, {})
    if not sector_data:
        return None

    try:
        yf_sym = symbol.replace("/", "-") if "/" in symbol else symbol
        hist = yf.Ticker(yf_sym).history(period="5d", auto_adjust=True)
        if hist.empty or len(hist) < 2:
            return None

        stock_5d = float((hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100)
        sector_5d = sector_data["return_5d"]

        return {
            "sector": sector,
            "stock_5d": round(stock_5d, 2),
            "sector_5d": sector_5d,
            "relative_strength": round(stock_5d - sector_5d, 2),
            "sector_trend": sector_data["trend"],
        }
    except Exception:
        return None


def _guess_sector(symbol):
    """Rough sector guess based on common stocks. Falls back to 'tech'."""
    _SECTOR_MAP = {
        "finance": {"SOFI", "HOOD", "AFRM", "UPST", "COIN", "SQ", "V", "MA", "JPM",
                     "BAC", "GS", "MS", "WFC", "AXP", "BLK", "SCHW", "ALLY", "LC"},
        "tech": {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AMD", "INTC",
                 "CRM", "ORCL", "ADBE", "NOW", "DDOG", "NET", "ZS", "SNOW", "MDB"},
        "energy": {"RIG", "ET", "AR", "CNX", "BTU", "KOS", "BTE", "OVV", "PLUG",
                   "FCEL", "BE", "RUN"},
        "healthcare": {"UNH", "JNJ", "PFE", "MRK", "LLY", "AMGN", "GILD", "ISRG",
                       "HIMS", "DNA", "WVE", "CRSP", "NTLA", "BEAM"},
        "consumer_disc": {"TSLA", "NKE", "SBUX", "MCD", "LULU", "DECK", "RIVN",
                          "LCID", "NIO", "CVNA", "ETSY", "CHWY"},
        "industrial": {"BA", "RTX", "LMT", "GE", "HON", "CAT", "DE", "JOBY"},
        "comm_services": {"NFLX", "DIS", "ROKU", "SNAP", "PINS", "RBLX", "DKNG"},
    }
    for sector, symbols in _SECTOR_MAP.items():
        if symbol.upper() in symbols:
            return sector
    return "tech"  # default fallback


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
