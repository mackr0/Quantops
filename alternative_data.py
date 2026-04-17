"""Alternative data sources — insider trades, short interest, institutional
holdings, fundamentals, and options flow.

All data is FREE from yfinance. No API keys or subscriptions needed.
Cached in SQLite (survives restarts) with per-type TTLs to avoid
hammering Yahoo Finance. Previous in-memory cache was lost on every
deploy, causing 200+ yfinance calls that triggered rate limiting.
"""

import json
import logging
import sqlite3
import time
import threading

import yfinance as yf

logger = logging.getLogger(__name__)

_DB_PATH = "quantopsai.db"
_yf_lock = threading.Lock()

_CACHE_TTL = {
    "insider": 86400,
    "fundamentals": 86400,
    "short_interest": 3600,
    "institutional": 86400,
    "options_flow": 1800,
}


def _ensure_cache_table():
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alt_data_cache (
                cache_key TEXT PRIMARY KEY,
                data_json TEXT,
                fetched_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass


_table_ensured = False


def _get_cached(key, ttl_type="insider"):
    global _table_ensured
    if not _table_ensured:
        _ensure_cache_table()
        _table_ensured = True
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            "SELECT data_json, fetched_at FROM alt_data_cache WHERE cache_key=?",
            (key,)
        ).fetchone()
        conn.close()
        if row and (time.time() - row[1]) < _CACHE_TTL.get(ttl_type, 3600):
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _set_cached(key, value):
    global _table_ensured
    if not _table_ensured:
        _ensure_cache_table()
        _table_ensured = True
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO alt_data_cache (cache_key, data_json, fetched_at) "
            "VALUES (?, ?, ?)",
            (key, json.dumps(value, default=str), time.time())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Insider Transactions
# ---------------------------------------------------------------------------

def get_insider_activity(symbol):
    """Get recent insider transactions for a symbol.

    Returns dict with:
        recent_buys: int (count in last 90 days)
        recent_sells: int
        net_direction: "buying" | "selling" | "neutral"
        notable: str or None (e.g., "CEO bought $2.1M on Apr 5")
        total_buy_value: float
        total_sell_value: float
    """
    cache_key = f"insider_{symbol}"
    cached = _get_cached(cache_key, "insider")
    if cached is not None:
        return cached

    result = {
        "recent_buys": 0, "recent_sells": 0,
        "net_direction": "neutral", "notable": None,
        "total_buy_value": 0, "total_sell_value": 0,
    }

    try:
        yf_sym = symbol.replace("/", "-") if "/" in symbol else symbol
        with _yf_lock:
            ticker = yf.Ticker(yf_sym)

        # Try insider_transactions first
        txns = getattr(ticker, "insider_transactions", None)
        if txns is None or (hasattr(txns, "empty") and txns.empty):
            _set_cached(cache_key, result)
            return result

        # Count buys and sells
        for _, row in txns.iterrows():
            text = str(row.get("Text", "")).lower()
            shares = abs(float(row.get("Shares", 0) or 0))
            value = abs(float(row.get("Value", 0) or 0))

            if "purchase" in text or "buy" in text:
                result["recent_buys"] += 1
                result["total_buy_value"] += value
            elif "sale" in text or "sell" in text:
                result["recent_sells"] += 1
                result["total_sell_value"] += value

        # Determine direction
        if result["recent_buys"] > result["recent_sells"] + 1:
            result["net_direction"] = "buying"
        elif result["recent_sells"] > result["recent_buys"] + 1:
            result["net_direction"] = "selling"

        # Notable transaction (largest buy)
        if result["total_buy_value"] > 100_000:
            result["notable"] = (
                f"{result['recent_buys']} insider buys "
                f"(${result['total_buy_value']:,.0f} total)"
            )

    except Exception as exc:
        logger.debug("Insider data failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Short Interest
# ---------------------------------------------------------------------------

def get_short_interest(symbol):
    """Get short interest data from yfinance ticker.info.

    Returns dict with:
        short_pct_float: float (% of float sold short)
        short_ratio: float (days to cover)
        squeeze_risk: "high" | "medium" | "low"
    """
    cache_key = f"short_{symbol}"
    cached = _get_cached(cache_key, "short_interest")
    if cached is not None:
        return cached

    result = {
        "short_pct_float": 0, "short_ratio": 0,
        "squeeze_risk": "low",
    }

    try:
        yf_sym = symbol.replace("/", "-") if "/" in symbol else symbol
        with _yf_lock:
            info = yf.Ticker(yf_sym).info or {}

        result["short_pct_float"] = float(info.get("shortPercentOfFloat", 0) or 0) * 100
        result["short_ratio"] = float(info.get("shortRatio", 0) or 0)

        # Squeeze risk assessment
        if result["short_pct_float"] > 20 and result["short_ratio"] > 5:
            result["squeeze_risk"] = "high"
        elif result["short_pct_float"] > 10 or result["short_ratio"] > 3:
            result["squeeze_risk"] = "medium"

    except Exception as exc:
        logger.debug("Short interest failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Fundamental Data
# ---------------------------------------------------------------------------

def get_fundamentals(symbol):
    """Get key fundamental data from yfinance ticker.info.

    Returns dict with valuation, ownership, and sector data.
    """
    cache_key = f"fund_{symbol}"
    cached = _get_cached(cache_key, "fundamentals")
    if cached is not None:
        return cached

    result = {
        "market_cap": 0, "pe_trailing": 0, "pe_forward": 0,
        "beta": 0, "dividend_yield": 0,
        "sector": "", "industry": "",
        "insider_pct": 0, "institutional_pct": 0,
    }

    try:
        yf_sym = symbol.replace("/", "-") if "/" in symbol else symbol
        with _yf_lock:
            info = yf.Ticker(yf_sym).info or {}

        result["market_cap"] = float(info.get("marketCap", 0) or 0)
        result["pe_trailing"] = float(info.get("trailingPE", 0) or 0)
        result["pe_forward"] = float(info.get("forwardPE", 0) or 0)
        result["beta"] = float(info.get("beta", 0) or 0)
        result["dividend_yield"] = float(info.get("dividendYield", 0) or 0) * 100
        result["sector"] = info.get("sector", "")
        result["industry"] = info.get("industry", "")
        result["insider_pct"] = float(info.get("heldPercentInsiders", 0) or 0) * 100
        result["institutional_pct"] = float(info.get("heldPercentInstitutions", 0) or 0) * 100

    except Exception as exc:
        logger.debug("Fundamentals failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Options Unusual Activity
# ---------------------------------------------------------------------------

def get_options_unusual(symbol):
    """Detect unusual options activity — high call/put volume or skew.

    Returns dict with:
        has_options: bool
        total_call_volume: int
        total_put_volume: int
        put_call_ratio: float
        unusual: bool (True if volume is very high relative to open interest)
        signal: "bullish_flow" | "bearish_flow" | "neutral"
        notable: str or None
    """
    cache_key = f"opts_{symbol}"
    cached = _get_cached(cache_key, "options_flow")
    if cached is not None:
        return cached

    result = {
        "has_options": False,
        "total_call_volume": 0, "total_put_volume": 0,
        "put_call_ratio": 0, "unusual": False,
        "signal": "neutral", "notable": None,
    }

    try:
        yf_sym = symbol.replace("/", "-") if "/" in symbol else symbol
        with _yf_lock:
            ticker = yf.Ticker(yf_sym)

        # Get nearest expiration
        expirations = ticker.options
        if not expirations:
            _set_cached(cache_key, result)
            return result

        result["has_options"] = True
        chain = ticker.option_chain(expirations[0])

        # Sum up call and put volumes
        call_vol = int(chain.calls["volume"].sum()) if "volume" in chain.calls else 0
        put_vol = int(chain.puts["volume"].sum()) if "volume" in chain.puts else 0
        call_oi = int(chain.calls["openInterest"].sum()) if "openInterest" in chain.calls else 1
        put_oi = int(chain.puts["openInterest"].sum()) if "openInterest" in chain.puts else 1

        result["total_call_volume"] = call_vol
        result["total_put_volume"] = put_vol

        total_vol = call_vol + put_vol
        if total_vol > 0:
            result["put_call_ratio"] = round(put_vol / max(call_vol, 1), 2)

        # Detect unusual activity (volume > 2x open interest)
        if call_vol > call_oi * 2 or put_vol > put_oi * 2:
            result["unusual"] = True

        # Signal direction
        if call_vol > put_vol * 2:
            result["signal"] = "bullish_flow"
            result["notable"] = f"Heavy call buying: {call_vol:,} calls vs {put_vol:,} puts"
        elif put_vol > call_vol * 2:
            result["signal"] = "bearish_flow"
            result["notable"] = f"Heavy put buying: {put_vol:,} puts vs {call_vol:,} calls"

    except Exception as exc:
        logger.debug("Options data failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Intraday Patterns (5-minute bars)
# ---------------------------------------------------------------------------

def get_intraday_patterns(symbol):
    """Analyze intraday price patterns from 5-minute bars.

    Returns dict with:
        vwap_position: "above" | "below" | "at" (relative to intraday VWAP)
        opening_range_breakout: bool (price above first 30-min high)
        intraday_trend: "up" | "down" | "flat"
        intraday_change_pct: float
        volume_profile: "front_loaded" | "back_loaded" | "even"
    """
    cache_key = f"intra_{symbol}"
    cached = _get_cached(cache_key, "options_flow")  # 30 min cache
    if cached is not None:
        return cached

    result = {
        "vwap_position": "at",
        "opening_range_breakout": False,
        "intraday_trend": "flat",
        "intraday_change_pct": 0,
        "volume_profile": "even",
    }

    try:
        yf_sym = symbol.replace("/", "-") if "/" in symbol else symbol
        with _yf_lock:
            df = yf.Ticker(yf_sym).history(period="1d", interval="5m")

        if df.empty or len(df) < 6:
            _set_cached(cache_key, result)
            return result

        df.columns = [c.lower() for c in df.columns]

        # Intraday VWAP
        typical = (df["high"] + df["low"] + df["close"]) / 3
        cumulative_tpv = (typical * df["volume"]).cumsum()
        cumulative_vol = df["volume"].cumsum()
        vwap = cumulative_tpv / cumulative_vol
        last_close = float(df["close"].iloc[-1])
        last_vwap = float(vwap.iloc[-1])

        if last_close > last_vwap * 1.002:
            result["vwap_position"] = "above"
        elif last_close < last_vwap * 0.998:
            result["vwap_position"] = "below"

        # Opening range breakout (first 30 min = first 6 5-min bars)
        opening_high = float(df["high"].iloc[:6].max())
        if last_close > opening_high:
            result["opening_range_breakout"] = True

        # Intraday trend
        first_close = float(df["close"].iloc[0])
        change = (last_close - first_close) / first_close * 100
        result["intraday_change_pct"] = round(change, 2)
        if change > 0.5:
            result["intraday_trend"] = "up"
        elif change < -0.5:
            result["intraday_trend"] = "down"

        # Volume profile (front vs back loaded)
        mid = len(df) // 2
        front_vol = float(df["volume"].iloc[:mid].sum())
        back_vol = float(df["volume"].iloc[mid:].sum())
        if front_vol > back_vol * 1.5:
            result["volume_profile"] = "front_loaded"
        elif back_vol > front_vol * 1.5:
            result["volume_profile"] = "back_loaded"

    except Exception as exc:
        logger.debug("Intraday data failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Batch: get all alternative data for a symbol
# ---------------------------------------------------------------------------

def get_all_alternative_data(symbol):
    """Fetch all alternative data for a symbol in one call.

    Returns dict combining insider, short interest, fundamentals,
    options flow, and intraday patterns. All free from yfinance.
    """
    # Skip for crypto (no insider/options data)
    if "/" in symbol:
        return {"is_crypto": True}

    return {
        "insider": get_insider_activity(symbol),
        "short": get_short_interest(symbol),
        "fundamentals": get_fundamentals(symbol),
        "options": get_options_unusual(symbol),
        "intraday": get_intraday_patterns(symbol),
    }
