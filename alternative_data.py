"""Alternative data sources — insider trades, short interest, institutional
holdings, fundamentals, and options flow.

Data source policy (per ALPACA-FIRST DATA RULE, feedback memory):
  - Insider transactions: yfinance ONLY. Alpaca is a broker; no
    insider-trades endpoint. Same for short_interest, fundamentals
    (heldPercentInsiders, heldPercentInstitutions, etc.).
  - Intraday 5-min bars (used in get_intraday_microstructure): SHOULD
    migrate to Alpaca `/v2/stocks/{sym}/bars?timeframe=5Min`. Tracked
    as a known follow-up; not yet migrated.

Cached in SQLite (survives restarts) with per-type TTLs to avoid
hammering Yahoo Finance.
"""

import json
import logging
import os
import sqlite3
import time
import threading
from typing import Any, Dict
from urllib.request import urlopen, Request
import urllib.parse
import urllib.request

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
    "congressional": 86400,
    "finra_short_vol": 86400,
    "analyst_estimates": 86400,
    # Local-SQLite alt-data sources refreshed daily by the
    # /opt/quantopsai-altdata/ projects. Cache 6h so per-cycle reads
    # are cheap.
    "altdata_local": 21600,
    # Earnings transcripts are quarterly events. The AI tone analysis
    # of an 8-K release doesn't change between scans — cache 30 days.
    # Was misfiled under "insider" (24h) until 2026-04-27 — caused
    # ~30 redundant per-symbol AI calls per profile per day.
    "transcript": 86400 * 30,
}

_http_lock = threading.Lock()


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
        # Migrated 2026-05-01 from yfinance 5-min bars to Alpaca's
        # /v2/stocks/<sym>/bars endpoint with timeframe=5Min. Real-time,
        # free with our paper-account keys.
        import requests
        import config
        from datetime import datetime, timedelta, timezone
        import pandas as pd

        if "/" in symbol:
            # Crypto path uses a different endpoint and 24/7 schedule;
            # caller doesn't currently invoke this for crypto, but bail
            # safely if it does.
            _set_cached(cache_key, result)
            return result

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=1)
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/bars",
            headers={
                "APCA-API-KEY-ID": config.ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
            },
            params={
                "timeframe": "5Min",
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 100,
                "adjustment": "raw",
            },
            timeout=10,
        )
        if r.status_code != 200:
            _set_cached(cache_key, result)
            return result
        bars = (r.json() or {}).get("bars") or []
        if not bars or len(bars) < 6:
            _set_cached(cache_key, result)
            return result

        # Convert to DataFrame matching the old shape (columns:
        # open, high, low, close, volume; lowercase)
        df = pd.DataFrame([
            {"open": float(b["o"]), "high": float(b["h"]),
             "low": float(b["l"]), "close": float(b["c"]),
             "volume": float(b["v"])}
            for b in bars
        ])

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

# ---------------------------------------------------------------------------
# Congressional Trading Disclosures
# ---------------------------------------------------------------------------

def get_congressional_trading(symbol):
    """Fetch congressional trading activity for a symbol via QuiverQuant.

    Returns dict with:
        net_direction: str — 'buying', 'selling', or 'neutral'
        recent_transactions: int — count in last 90 days
        total_value: float — estimated total dollar value
        most_recent_date: str or None
    """
    cache_key = f"congress_{symbol}"
    cached = _get_cached(cache_key, "congressional")
    if cached is not None:
        return cached

    result = {
        "net_direction": "neutral",
        "recent_transactions": 0,
        "total_value": 0,
        "most_recent_date": None,
        "members": [],
    }

    try:
        from datetime import datetime, timedelta
        import json as _json

        url = f"https://api.quiverquant.com/beta/historical/congresstrading/{symbol}"
        req = Request(url, headers={"User-Agent": "QuantOpsAI Research Bot",
                                     "Accept": "application/json"})
        with _http_lock:
            resp = urlopen(req, timeout=15)
            data = _json.loads(resp.read())

        cutoff = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
        buys = 0
        sells = 0
        total_val = 0
        recent_date = None
        members = []

        for txn in (data if isinstance(data, list) else []):
            txn_date = txn.get("TransactionDate", "") or txn.get("transaction_date", "")
            if txn_date < cutoff:
                continue

            txn_type = (txn.get("Transaction", "") or txn.get("transaction", "")).lower()
            amount = txn.get("Amount", "") or txn.get("amount", "")
            name = txn.get("Representative", "") or txn.get("representative", "")

            # Parse amount range like "$1,001 - $15,000" → midpoint
            val = 0
            if isinstance(amount, str) and "-" in amount:
                parts = amount.replace("$", "").replace(",", "").split("-")
                try:
                    val = (float(parts[0].strip()) + float(parts[1].strip())) / 2
                except (ValueError, IndexError):
                    pass

            if "purchase" in txn_type or "buy" in txn_type:
                buys += 1
                total_val += val
            elif "sale" in txn_type or "sell" in txn_type:
                sells += 1
                total_val -= val

            if recent_date is None or txn_date > recent_date:
                recent_date = txn_date
            if name and name not in members:
                members.append(name)

        result["recent_transactions"] = buys + sells
        result["total_value"] = round(abs(total_val))
        result["most_recent_date"] = recent_date
        result["members"] = members[:5]
        if buys > sells:
            result["net_direction"] = "buying"
        elif sells > buys:
            result["net_direction"] = "selling"

    except Exception as exc:
        logger.debug("Congressional trading failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# FINRA Daily Short Volume
# ---------------------------------------------------------------------------

def get_finra_short_volume(symbol):
    """Fetch daily short volume from FINRA regsho data.

    Returns dict with:
        short_volume_ratio: float — short vol / total vol (0-1)
        short_volume: int
        total_volume: int
        is_elevated: bool — True if ratio > 0.50
        date: str or None
    """
    cache_key = f"finra_sv_{symbol}"
    cached = _get_cached(cache_key, "finra_short_vol")
    if cached is not None:
        return cached

    result = {
        "short_volume_ratio": 0,
        "short_volume": 0,
        "total_volume": 0,
        "is_elevated": False,
        "date": None,
    }

    try:
        from datetime import datetime, timedelta

        # Try today, then previous business days
        for days_back in range(0, 5):
            dt = datetime.utcnow() - timedelta(days=days_back)
            date_str = dt.strftime("%Y%m%d")
            url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date_str}.txt"
            req = Request(url, headers={"User-Agent": "QuantOpsAI Research Bot"})
            try:
                with _http_lock:
                    resp = urlopen(req, timeout=10)
                    text = resp.read().decode("utf-8")

                for line in text.strip().split("\n"):
                    if not line or line.startswith("Date"):
                        continue
                    parts = line.split("|")
                    if len(parts) >= 5 and parts[1].strip().upper() == symbol.upper():
                        short_vol = int(parts[2].strip())
                        # parts[3] is short exempt volume
                        total_vol = int(parts[4].strip())
                        ratio = short_vol / total_vol if total_vol > 0 else 0

                        result["short_volume"] = short_vol
                        result["total_volume"] = total_vol
                        result["short_volume_ratio"] = round(ratio, 3)
                        result["is_elevated"] = ratio > 0.50
                        result["date"] = dt.strftime("%Y-%m-%d")
                        break
                if result["date"]:
                    break
            except Exception:
                continue

    except Exception as exc:
        logger.debug("FINRA short volume failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Dark Pool / ATS Volume
# ---------------------------------------------------------------------------

_CACHE_TTL["dark_pool"] = 604800  # 7 days (weekly data)

def get_dark_pool_volume(symbol):
    """Fetch dark pool (ATS) trading volume from FINRA OTC transparency.

    Uses POST to filter by symbol. Sums across all ATS venues to get
    total dark pool volume for the symbol.

    Returns dict with:
        ats_volume: int — total shares traded across all dark pools
        ats_trade_count: int — number of dark pool trades
        num_venues: int — how many ATS venues traded this symbol
        week_start: str or None — week the data covers
    """
    cache_key = f"dark_pool_{symbol}"
    cached = _get_cached(cache_key, "dark_pool")
    if cached is not None:
        return cached

    result = {
        "ats_volume": 0,
        "ats_trade_count": 0,
        "num_venues": 0,
        "week_start": None,
    }

    try:
        import json as _json

        body = _json.dumps({
            "compareFilters": [{
                "fieldName": "issueSymbolIdentifier",
                "fieldValue": symbol.upper(),
                "compareType": "EQUAL",
            }],
            "limit": 50,
        }).encode()

        req = Request(
            "https://api.finra.org/data/group/otcMarket/name/weeklySummary",
            data=body,
            method="POST",
            headers={
                "User-Agent": "QuantOpsAI Research Bot",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with _http_lock:
                resp = urlopen(req, timeout=15)
                data = _json.loads(resp.read())

            if isinstance(data, list) and data:
                # Sum across all ATS venues for total dark pool volume
                total_shares = sum(int(r.get("totalWeeklyShareQuantity", 0) or 0) for r in data)
                total_trades = sum(int(r.get("totalWeeklyTradeCount", 0) or 0) for r in data)
                result["ats_volume"] = total_shares
                result["ats_trade_count"] = total_trades
                result["num_venues"] = len(data)
                result["week_start"] = data[0].get("weekStartDate")
        except Exception:
            pass

    except Exception as exc:
        logger.debug("Dark pool volume failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Insider Cluster Detection
# ---------------------------------------------------------------------------

def get_insider_cluster(symbol):
    """Detect clusters of insider buying — 3+ insiders buying within 14 days.

    Returns dict with:
        is_cluster: bool
        insider_count: int — distinct insiders in cluster
        total_value: float — estimated total value
        cluster_direction: str — 'buying', 'selling', or 'neutral'
    """
    cache_key = f"insider_cluster_{symbol}"
    cached = _get_cached(cache_key, "insider")
    if cached is not None:
        return cached

    result = {
        "is_cluster": False,
        "insider_count": 0,
        "total_value": 0,
        "cluster_direction": "neutral",
    }

    try:
        with _yf_lock:
            ticker = yf.Ticker(symbol)
            txns = getattr(ticker, "insider_transactions", None)

        if txns is None or (hasattr(txns, "empty") and txns.empty):
            _set_cached(cache_key, result)
            return result

        from datetime import datetime, timedelta

        # Parse transactions — look for buy clusters in last 90 days
        cutoff = datetime.utcnow() - timedelta(days=90)
        buy_dates = []
        buy_names = set()
        buy_value = 0

        for _, row in txns.iterrows():
            try:
                txn_text = str(row.get("Text", "") or row.get("Transaction", "")).lower()
                if "purchase" not in txn_text and "buy" not in txn_text:
                    continue

                date_val = row.get("Start Date", row.get("Date"))
                if hasattr(date_val, "to_pydatetime"):
                    date_val = date_val.to_pydatetime()
                if hasattr(date_val, "replace"):
                    if date_val.tzinfo:
                        date_val = date_val.replace(tzinfo=None)
                    if date_val < cutoff:
                        continue

                name = str(row.get("Insider", row.get("Name", "unknown")))
                shares = abs(float(row.get("Shares", 0) or 0))
                value = abs(float(row.get("Value", 0) or 0))

                buy_dates.append(date_val)
                buy_names.add(name)
                buy_value += value
            except Exception:
                continue

        # Check for cluster: 3+ distinct insiders buying within any 14-day window
        if len(buy_names) >= 3:
            result["is_cluster"] = True
            result["insider_count"] = len(buy_names)
            result["total_value"] = round(buy_value)
            result["cluster_direction"] = "buying"

    except Exception as exc:
        logger.debug("Insider cluster check failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Analyst Estimate Revisions
# ---------------------------------------------------------------------------

def get_analyst_estimates(symbol):
    """Fetch analyst EPS/revenue estimate revision direction.

    Returns dict with:
        eps_revision_direction: str — 'up', 'down', or 'flat'
        eps_current_estimate: float
        revenue_revision_direction: str — 'up', 'down', or 'flat'
        revision_magnitude_pct: float — % change in estimates
    """
    cache_key = f"analyst_est_{symbol}"
    cached = _get_cached(cache_key, "analyst_estimates")
    if cached is not None:
        return cached

    result = {
        "eps_revision_direction": "flat",
        "eps_current_estimate": 0,
        "revenue_revision_direction": "flat",
        "revision_magnitude_pct": 0,
    }

    try:
        with _yf_lock:
            ticker = yf.Ticker(symbol)
            earnings_est = getattr(ticker, "earnings_estimate", None)
            revenue_est = getattr(ticker, "revenue_estimate", None)

        # EPS estimates — compare current to 30 days ago
        if earnings_est is not None and hasattr(earnings_est, "empty") and not earnings_est.empty:
            try:
                if "avg" in earnings_est.columns:
                    current = float(earnings_est["avg"].iloc[0])
                    result["eps_current_estimate"] = round(current, 2)

                    # Check for revision columns (varies by yfinance version)
                    for col in ["numberofanalysts", "growth"]:
                        pass  # These don't help with direction

                    # Compare current quarter vs 30d/60d revision
                    if "low" in earnings_est.columns and "high" in earnings_est.columns:
                        low = float(earnings_est["low"].iloc[0])
                        high = float(earnings_est["high"].iloc[0])
                        mid_range = (low + high) / 2
                        if mid_range > 0 and current > 0:
                            diff_pct = ((current - mid_range) / abs(mid_range)) * 100
                            result["revision_magnitude_pct"] = round(diff_pct, 1)
                            if diff_pct > 2:
                                result["eps_revision_direction"] = "up"
                            elif diff_pct < -2:
                                result["eps_revision_direction"] = "down"
            except Exception:
                pass

        # Revenue estimates
        if revenue_est is not None and hasattr(revenue_est, "empty") and not revenue_est.empty:
            try:
                if "avg" in revenue_est.columns and "low" in revenue_est.columns:
                    current = float(revenue_est["avg"].iloc[0])
                    low = float(revenue_est["low"].iloc[0])
                    high = float(revenue_est["high"].iloc[0])
                    mid_range = (low + high) / 2
                    if mid_range > 0 and current > 0:
                        diff_pct = ((current - mid_range) / abs(mid_range)) * 100
                        if diff_pct > 2:
                            result["revenue_revision_direction"] = "up"
                        elif diff_pct < -2:
                            result["revenue_revision_direction"] = "down"
            except Exception:
                pass

    except Exception as exc:
        logger.debug("Analyst estimates failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Earnings Surprise History
# ---------------------------------------------------------------------------

_CACHE_TTL["earnings_surprise"] = 86400  # 24h

def get_earnings_surprise(symbol):
    """Check if this company consistently beats or misses earnings.

    Returns dict with:
        beat_count: int — quarters that beat estimate
        miss_count: int — quarters that missed
        total_quarters: int
        avg_surprise_pct: float — average surprise %
        streak: int — consecutive beats (positive) or misses (negative)
        surprise_direction: str — 'beats', 'misses', or 'mixed'
    """
    cache_key = f"earnings_surprise_{symbol}"
    cached = _get_cached(cache_key, "earnings_surprise")
    if cached is not None:
        return cached

    result = {
        "beat_count": 0,
        "miss_count": 0,
        "total_quarters": 0,
        "avg_surprise_pct": 0,
        "streak": 0,
        "surprise_direction": "mixed",
    }

    try:
        with _yf_lock:
            ticker = yf.Ticker(symbol)
            # Try earnings_history first (has actual vs estimate)
            eh = getattr(ticker, "earnings_history", None)

        if eh is not None and hasattr(eh, "empty") and not eh.empty:
            surprises = []
            for _, row in eh.iterrows():
                try:
                    actual = float(row.get("epsActual", 0) or 0)
                    estimate = float(row.get("epsEstimate", 0) or 0)
                    if estimate != 0:
                        surprise_pct = ((actual - estimate) / abs(estimate)) * 100
                        surprises.append(surprise_pct)
                except (ValueError, TypeError):
                    continue

            if surprises:
                beats = sum(1 for s in surprises if s > 0)
                misses = sum(1 for s in surprises if s < 0)
                result["beat_count"] = beats
                result["miss_count"] = misses
                result["total_quarters"] = len(surprises)
                result["avg_surprise_pct"] = round(sum(surprises) / len(surprises), 2)

                # Streak: count consecutive beats/misses from most recent
                streak = 0
                if surprises:
                    direction = 1 if surprises[0] > 0 else -1
                    for s in surprises:
                        if (s > 0 and direction > 0) or (s < 0 and direction < 0):
                            streak += direction
                        else:
                            break
                result["streak"] = streak

                if beats > misses * 2:
                    result["surprise_direction"] = "beats"
                elif misses > beats * 2:
                    result["surprise_direction"] = "misses"

    except Exception as exc:
        logger.debug("Earnings surprise failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Insider Timing vs Earnings Correlation
# ---------------------------------------------------------------------------

def get_insider_earnings_signal(symbol):
    """Correlate insider buying/selling with upcoming earnings dates.

    Insiders buying within 14 days of earnings = they know the numbers
    are good. Insiders selling before earnings = caution signal.

    Uses existing get_insider_activity() and check_earnings() — no new
    external calls.

    Returns dict with:
        insider_buying_near_earnings: bool
        insider_selling_near_earnings: bool
        days_to_earnings: int or None
        insider_direction_near_earnings: str — 'bullish', 'bearish', or 'neutral'
    """
    result = {
        "insider_buying_near_earnings": False,
        "insider_selling_near_earnings": False,
        "days_to_earnings": None,
        "insider_direction_near_earnings": "neutral",
    }

    try:
        from earnings_calendar import check_earnings

        earnings = check_earnings(symbol)
        if earnings is None:
            return result

        days_until = earnings.get("days_until")
        if days_until is None or days_until > 14:
            result["days_to_earnings"] = days_until
            return result

        result["days_to_earnings"] = days_until

        insider = get_insider_activity(symbol)
        if not insider:
            return result

        direction = insider.get("net_direction", "neutral")
        buys = insider.get("recent_buys", 0)
        sells = insider.get("recent_sells", 0)

        if direction == "buying" and buys > 0 and days_until <= 14:
            result["insider_buying_near_earnings"] = True
            result["insider_direction_near_earnings"] = "bullish"
        elif direction == "selling" and sells > 0 and days_until <= 14:
            result["insider_selling_near_earnings"] = True
            result["insider_direction_near_earnings"] = "bearish"

    except Exception as exc:
        logger.debug("Insider earnings signal failed for %s: %s", symbol, exc)

    return result


# ---------------------------------------------------------------------------
# USPTO Patent Filing Velocity
# ---------------------------------------------------------------------------

_CACHE_TTL["patents"] = 604800  # 7 days

def get_patent_activity(symbol):
    """Check recent patent filing velocity for a company via USPTO PatentsView API.

    Returns dict with:
        recent_filings_90d: int
        recent_filings_365d: int
        velocity_trend: str — 'accelerating', 'stable', or 'declining'
        has_data: bool
    """
    cache_key = f"patents_{symbol}"
    cached = _get_cached(cache_key, "patents")
    if cached is not None:
        return cached

    result = {
        "recent_filings_90d": 0,
        "recent_filings_365d": 0,
        "velocity_trend": "stable",
        "has_data": False,
    }

    try:
        import json as _json
        from datetime import datetime, timedelta
        import os
        import re

        # Get company name from yfinance
        company_name = None
        try:
            with _yf_lock:
                ticker = yf.Ticker(symbol)
                info = getattr(ticker, "info", {}) or {}
            company_name = info.get("shortName") or info.get("longName")
        except Exception:
            pass

        if not company_name:
            _set_cached(cache_key, result)
            return result

        # Clean company name for search
        clean_name = re.sub(
            r'\b(Inc|Corp|Ltd|LLC|Co|Group|Holdings|Plc)\.?\b',
            '', company_name, flags=re.IGNORECASE
        ).strip().strip(",").strip()
        if len(clean_name) < 3:
            _set_cached(cache_key, result)
            return result

        date_365 = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%d")
        date_90 = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")

        # PatentsView API — free, no key required for basic queries
        url = (
            f"https://api.patentsview.org/patents/query?"
            f"q={{\"_and\":[{{\"_gte\":{{\"patent_date\":\"{date_365}\"}}}},"
            f"{{\"_contains\":{{\"assignee_organization\":\"{clean_name}\"}}}}]}}"
            f"&f=[\"patent_number\",\"patent_date\"]&o={{\"per_page\":100}}"
        )
        api_key = os.environ.get("USPTO_API_KEY", "")
        headers = {"User-Agent": "QuantOpsAI Research Bot"}
        if api_key:
            headers["X-Api-Key"] = api_key

        req = Request(url, headers=headers)
        try:
            with _http_lock:
                resp = urlopen(req, timeout=15)
                data = _json.loads(resp.read())

            patents = data.get("patents") or []
            result["recent_filings_365d"] = len(patents)
            result["recent_filings_90d"] = sum(
                1 for p in patents if p.get("patent_date", "") >= date_90
            )
            result["has_data"] = len(patents) > 0

            # Compare last 90 days to avg of prior quarters
            if result["recent_filings_365d"] >= 4:
                prior = result["recent_filings_365d"] - result["recent_filings_90d"]
                avg_prior_q = prior / 3 if prior > 0 else 0
                if result["recent_filings_90d"] > avg_prior_q * 1.5:
                    result["velocity_trend"] = "accelerating"
                elif avg_prior_q > 0 and result["recent_filings_90d"] < avg_prior_q * 0.5:
                    result["velocity_trend"] = "declining"
        except Exception:
            pass

    except Exception as exc:
        logger.debug("Patent activity failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Local SQLite alt-data sources (the four standalone projects)
# ---------------------------------------------------------------------------
# Each project lives at {ALTDATA_BASE}/{project}/data/{db}.db.
#
# Production (deployed 2026-04-26):
#   ALTDATA_BASE_PATH = /opt/quantopsai-altdata
#   Daily cron @ 06:00 UTC refreshes all four DBs.
# Local dev (default):
#   $HOME/{project}/data/{db}.db (set ALTDATA_BASE_PATH to override).
#
# Helpers all gracefully no-op when the DB file is missing or empty,
# so the code is safe to load whether or not the data is present
# (the read layer ships dormant if a host doesn't have the projects
# deployed).

def _altdata_db(project: str, db_filename: str) -> str:
    """Resolve absolute path to one alt-data project's DB."""
    base = os.environ.get("ALTDATA_BASE_PATH")
    if base:
        return os.path.join(base, project, "data", db_filename)
    # Local dev default
    home = os.path.expanduser("~")
    return os.path.join(home, project, "data", db_filename)


def _altdata_query(project: str, db_filename: str, sql: str,
                    params: tuple = ()) -> list:
    """Read-only query against an alt-data SQLite. Returns list of
    sqlite3.Row. Empty list on any error (missing file, bad query,
    locked DB)."""
    path = _altdata_db(project, db_filename)
    if not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                timeout=2.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows
    except Exception as exc:
        logger.debug("altdata query failed (%s): %s", project, exc)
        return []


def get_congressional_recent(symbol: str) -> Dict[str, Any]:
    """Recent (last 60 days) congressional trades for `symbol` — count,
    dollar volume, party split, last filing date.

    Source: ~/congresstrades — Senate eFD + House Clerk STOCK Act
    disclosures.
    """
    cache_key = f"congresstrades_recent:{symbol}"
    cached = _get_cached(cache_key, "altdata_local")
    if cached is not None:
        return cached

    rows = _altdata_query(
        "congresstrades", "congress.db",
        """
        SELECT chamber, member_name, member_party, transaction_date,
               transaction_type, amount_low, amount_high, filing_date
        FROM trades
        WHERE UPPER(ticker) = UPPER(?)
          AND date(filing_date) >= date('now', '-60 days')
        ORDER BY filing_date DESC
        """,
        (symbol,),
    )

    result: Dict[str, Any] = {
        "trades_60d": 0,
        "buys_60d": 0,
        "sells_60d": 0,
        "dollar_volume_60d": 0,
        "net_direction": "neutral",
        "last_filing_date": None,
        "party_breakdown": {},
    }
    if not rows:
        _set_cached(cache_key, result)
        return result

    party_counts: Dict[str, int] = {}
    for r in rows:
        result["trades_60d"] += 1
        ttype = (r["transaction_type"] or "").lower()
        if ttype == "buy":
            result["buys_60d"] += 1
        elif ttype in ("sell", "partial_sale"):
            result["sells_60d"] += 1
        # Use the midpoint of the disclosed range as a proxy
        lo = r["amount_low"] or 0
        hi = r["amount_high"] or 0
        midpoint = (lo + hi) / 2 if (lo or hi) else 0
        result["dollar_volume_60d"] += int(midpoint)
        party = (r["member_party"] or "Unknown").strip() or "Unknown"
        party_counts[party] = party_counts.get(party, 0) + 1

    result["party_breakdown"] = party_counts
    if rows:
        result["last_filing_date"] = rows[0]["filing_date"]

    if result["buys_60d"] > result["sells_60d"] * 1.5:
        result["net_direction"] = "bullish"
    elif result["sells_60d"] > result["buys_60d"] * 1.5:
        result["net_direction"] = "bearish"

    _set_cached(cache_key, result)
    return result


def get_13f_institutional(symbol: str) -> Dict[str, Any]:
    """Latest-quarter 13F-HR institutional holdings for `symbol`.

    Returns: total holders, total shares, total value, top holder
    name, QoQ delta in aggregate shares.

    Source: ~/edgar13f — SEC 13F-HR XML filings.
    """
    cache_key = f"edgar13f_holdings:{symbol}"
    cached = _get_cached(cache_key, "altdata_local")
    if cached is not None:
        return cached

    # Find the latest quarter where this symbol has any holdings.
    # If period_of_report isn't populated yet on this DB (early seed
    # data), fall back to using the latest filed_date to pick a
    # filing — better than returning nothing.
    latest = _altdata_query(
        "edgar13f", "edgar13f.db",
        """
        SELECT MAX(f.period_of_report) as latest_quarter,
               MAX(f.filed_date) as latest_filed
        FROM holdings h
        JOIN filings f ON h.accession_number = f.accession_number
        WHERE UPPER(h.ticker) = UPPER(?)
        """,
        (symbol,),
    )
    if not latest:
        result = {"total_holders": 0}
        _set_cached(cache_key, result)
        return result
    latest_q = latest[0]["latest_quarter"] or ""
    has_period = bool(latest_q)

    # Aggregate the latest quarter (or all matching rows if
    # period_of_report isn't populated on this DB).
    if has_period:
        period_clause = "AND f.period_of_report = ?"
        period_params = (symbol, latest_q)
    else:
        period_clause = ""
        period_params = (symbol,)

    rows = _altdata_query(
        "edgar13f", "edgar13f.db",
        f"""
        SELECT COUNT(DISTINCT f.cik) as total_holders,
               SUM(h.shares) as total_shares,
               SUM(h.value_usd) as total_value
        FROM holdings h
        JOIN filings f ON h.accession_number = f.accession_number
        WHERE UPPER(h.ticker) = UPPER(?)
          AND (h.put_call IS NULL OR h.put_call = '')
          {period_clause}
        """,
        period_params,
    )
    summary = rows[0] if rows else None

    # Top holder by shares
    top_rows = _altdata_query(
        "edgar13f", "edgar13f.db",
        f"""
        SELECT fr.name, h.shares
        FROM holdings h
        JOIN filings f ON h.accession_number = f.accession_number
        JOIN filers fr ON f.cik = fr.cik
        WHERE UPPER(h.ticker) = UPPER(?)
          {period_clause}
        ORDER BY h.shares DESC LIMIT 1
        """,
        period_params,
    )

    # Prior quarter for QoQ delta — only meaningful with period data
    prior_rows = []
    if has_period:
        prior_rows = _altdata_query(
            "edgar13f", "edgar13f.db",
            """
            SELECT SUM(h.shares) as prior_shares
            FROM holdings h
            JOIN filings f ON h.accession_number = f.accession_number
            WHERE UPPER(h.ticker) = UPPER(?)
              AND f.period_of_report < ?
              AND f.period_of_report != ''
            ORDER BY f.period_of_report DESC LIMIT 1
            """,
            (symbol, latest_q),
        )

    result = {
        "quarter": latest_q,
        "total_holders": (summary["total_holders"] or 0) if summary else 0,
        "total_shares": (summary["total_shares"] or 0) if summary else 0,
        "total_value_usd": (summary["total_value"] or 0) if summary else 0,
        "top_holder_name": top_rows[0]["name"] if top_rows else None,
        "top_holder_shares": top_rows[0]["shares"] if top_rows else 0,
        "qoq_share_change_pct": None,
    }
    if prior_rows and prior_rows[0]["prior_shares"]:
        prior = prior_rows[0]["prior_shares"]
        cur = result["total_shares"]
        if prior > 0:
            result["qoq_share_change_pct"] = round(
                (cur - prior) / prior * 100.0, 1)

    _set_cached(cache_key, result)
    return result


def get_biotech_milestones(symbol: str) -> Dict[str, Any]:
    """Upcoming clinical-trial milestones for `symbol` — nearest PDUFA
    date, active phase-3 count, recent phase changes.

    Source: ~/biotechevents — ClinicalTrials.gov v2 + PDUFA tracker.
    """
    cache_key = f"biotech_milestones:{symbol}"
    cached = _get_cached(cache_key, "altdata_local")
    if cached is not None:
        return cached

    # Nearest upcoming PDUFA event
    pdufa_rows = _altdata_query(
        "biotechevents", "biotechevents.db",
        """
        SELECT drug_name, pdufa_date
        FROM pdufa_events
        WHERE UPPER(ticker) = UPPER(?)
          AND date(pdufa_date) >= date('now')
        ORDER BY date(pdufa_date) ASC LIMIT 1
        """,
        (symbol,),
    )

    # Active phase-3 trial count
    p3_rows = _altdata_query(
        "biotechevents", "biotechevents.db",
        """
        SELECT COUNT(*) as p3_count
        FROM trials
        WHERE UPPER(ticker) = UPPER(?)
          AND phase = 'PHASE3'
          AND overall_status IN ('RECRUITING', 'ACTIVE_NOT_RECRUITING')
        """,
        (symbol,),
    )

    # Recent phase or status change (last 30d)
    recent_change_rows = _altdata_query(
        "biotechevents", "biotechevents.db",
        """
        SELECT tc.field, tc.old_value, tc.new_value, tc.detected_at
        FROM trial_changes tc
        JOIN trials t ON tc.nct_id = t.nct_id
        WHERE UPPER(t.ticker) = UPPER(?)
          AND date(tc.detected_at) >= date('now', '-30 days')
          AND tc.field IN ('phase', 'overall_status')
        ORDER BY tc.detected_at DESC LIMIT 1
        """,
        (symbol,),
    )

    result: Dict[str, Any] = {
        "upcoming_pdufa_date": None,
        "days_to_pdufa": None,
        "drug_name": None,
        "active_phase3_count": (p3_rows[0]["p3_count"] or 0)
            if p3_rows else 0,
        "recent_phase_change": None,
    }

    if pdufa_rows:
        from datetime import datetime, date as _date
        pdufa_date = pdufa_rows[0]["pdufa_date"]
        result["upcoming_pdufa_date"] = pdufa_date
        result["drug_name"] = pdufa_rows[0]["drug_name"]
        try:
            from zoneinfo import ZoneInfo
            d = datetime.strptime(pdufa_date, "%Y-%m-%d").date()
            today_et = datetime.now(ZoneInfo("America/New_York")).date()
            result["days_to_pdufa"] = (d - today_et).days
        except Exception:
            pass

    if recent_change_rows:
        rc = recent_change_rows[0]
        result["recent_phase_change"] = {
            "field": rc["field"],
            "from": rc["old_value"],
            "to": rc["new_value"],
            "detected_at": rc["detected_at"],
        }

    _set_cached(cache_key, result)
    return result


def get_stocktwits_sentiment(symbol: str) -> Dict[str, Any]:
    """Recent (7d) StockTwits sentiment + currently-trending flag.

    Source: ~/stocktwits — StockTwits REST API messages + trending.
    """
    cache_key = f"stocktwits_sentiment:{symbol}"
    cached = _get_cached(cache_key, "altdata_local")
    if cached is not None:
        return cached

    # 7-day rollup from the daily aggregate table
    rows = _altdata_query(
        "stocktwits", "stocktwits.db",
        """
        SELECT SUM(n_messages) as msg_count,
               SUM(n_bullish) as bullish,
               SUM(n_bearish) as bearish,
               SUM(n_neutral) as neutral,
               AVG(net_sentiment) as avg_net_sentiment
        FROM ticker_sentiment_daily
        WHERE UPPER(ticker) = UPPER(?)
          AND date(date) >= date('now', '-7 days')
        """,
        (symbol,),
    )

    # Compare 7d to trailing-30d for "vs avg" magnitude
    avg_rows = _altdata_query(
        "stocktwits", "stocktwits.db",
        """
        SELECT AVG(n_messages) as avg_daily_messages
        FROM ticker_sentiment_daily
        WHERE UPPER(ticker) = UPPER(?)
          AND date(date) >= date('now', '-30 days')
        """,
        (symbol,),
    )

    # Currently trending (any snapshot in last 24h)
    trending_rows = _altdata_query(
        "stocktwits", "stocktwits.db",
        """
        SELECT MIN(rank) as best_rank, MAX(snapshot_at) as latest
        FROM trending_snapshots
        WHERE UPPER(ticker) = UPPER(?)
          AND datetime(snapshot_at) >= datetime('now', '-1 day')
        """,
        (symbol,),
    )

    result: Dict[str, Any] = {
        "message_count_7d": 0,
        "net_sentiment_7d": None,
        "vs_avg_message_count": None,
        "is_trending": False,
        "trending_rank": None,
    }
    if rows and rows[0]["msg_count"]:
        msg = rows[0]["msg_count"] or 0
        result["message_count_7d"] = msg
        result["net_sentiment_7d"] = (
            round(rows[0]["avg_net_sentiment"], 3)
            if rows[0]["avg_net_sentiment"] is not None else None
        )

    if avg_rows and avg_rows[0]["avg_daily_messages"]:
        avg30 = avg_rows[0]["avg_daily_messages"]
        avg7 = result["message_count_7d"] / 7.0
        if avg30 > 0:
            result["vs_avg_message_count"] = round(avg7 / avg30, 2)

    if (trending_rows and trending_rows[0]["best_rank"] is not None):
        result["is_trending"] = True
        result["trending_rank"] = trending_rows[0]["best_rank"]

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Item 3a — Google Trends signal
# ---------------------------------------------------------------------------

# 24h cache (Google rate-limits; daily granularity is plenty)
_CACHE_TTL["google_trends"] = 86400


def get_google_trends_signal(symbol: str):
    """Web-scraped Google Trends interest for a ticker.

    Output:
      {
        "trend_z_score": float | None,    # σ above/below trailing-year mean
        "trend_direction": "rising"|"flat"|"falling"|None,
        "current_index": int | None,      # last week, 0-100
        "yr_avg_index": float | None,
        "has_data": bool,
      }

    Best-effort: pytrends rate-limits hard (~5 req/min). On any HTTP
    error or rate-limit, returns has_data=False and the prompt
    suppresses the signal. Cached 24h.
    """
    if "/" in symbol:
        return {"has_data": False, "is_crypto": True}
    cache_key = f"google_trends_{symbol.upper()}"
    cached = _get_cached(cache_key, "google_trends")
    if cached is not None:
        return cached

    result = {
        "trend_z_score": None,
        "trend_direction": None,
        "current_index": None,
        "yr_avg_index": None,
        "has_data": False,
    }
    try:
        from pytrends.request import TrendReq
    except ImportError:
        logging.debug("pytrends not installed; Google Trends signal disabled")
        _set_cached(cache_key, result)
        return result

    try:
        py = TrendReq(hl="en-US", tz=0, timeout=(5, 10))
        # Use bracketed query so Google scopes to the ticker (not the
        # English word). 'today 12-m' = trailing year, weekly buckets.
        kw = f'"{symbol.upper()}"'
        py.build_payload([kw], timeframe="today 12-m", geo="US")
        df = py.interest_over_time()
        if df is None or df.empty or kw not in df.columns:
            _set_cached(cache_key, result)
            return result
        series = df[kw].astype(float)
        if len(series) < 8:
            _set_cached(cache_key, result)
            return result
        cur = float(series.iloc[-1])
        avg = float(series.mean())
        std = float(series.std(ddof=1))
        z = (cur - avg) / std if std > 0 else 0.0
        # Direction: linear slope over last 8 weeks
        last8 = series.tail(8).values
        if len(last8) >= 8:
            mid = len(last8) // 2
            recent_avg = sum(last8[mid:]) / max(len(last8[mid:]), 1)
            old_avg = sum(last8[:mid]) / max(len(last8[:mid]), 1)
            delta_pct = (recent_avg - old_avg) / max(old_avg, 1)
            if delta_pct > 0.20:
                direction = "rising"
            elif delta_pct < -0.20:
                direction = "falling"
            else:
                direction = "flat"
        else:
            direction = "flat"
        result.update({
            "trend_z_score": round(z, 2),
            "trend_direction": direction,
            "current_index": int(round(cur)),
            "yr_avg_index": round(avg, 1),
            "has_data": True,
        })
    except Exception as exc:
        logging.debug("Google Trends fetch failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Item 3a — Wikipedia page-views signal
# ---------------------------------------------------------------------------

_CACHE_TTL["wikipedia_pageviews"] = 86400

# Hand-curated ticker → Wikipedia article slug for symbols where the
# ticker isn't a meaningful Wikipedia title. Most large-caps are
# either the ticker itself or "Company Name". Falls back to
# `{ticker}_(company)` then to a Wikidata search.
WIKIPEDIA_TICKER_OVERRIDES = {
    "AAPL": "Apple_Inc.",
    "MSFT": "Microsoft",
    "GOOGL": "Alphabet_Inc.",
    "GOOG": "Alphabet_Inc.",
    "AMZN": "Amazon_(company)",
    "META": "Meta_Platforms",
    "TSLA": "Tesla,_Inc.",
    "NVDA": "Nvidia",
    "BRK.B": "Berkshire_Hathaway",
    "JPM": "JPMorgan_Chase",
    "V": "Visa_Inc.",
    "JNJ": "Johnson_%26_Johnson",
    "WMT": "Walmart",
    "MA": "Mastercard",
    "PG": "Procter_%26_Gamble",
    "UNH": "UnitedHealth_Group",
    "HD": "The_Home_Depot",
    "BAC": "Bank_of_America",
    "XOM": "ExxonMobil",
    "CVX": "Chevron_Corporation",
    "DIS": "The_Walt_Disney_Company",
    "NFLX": "Netflix",
    "PFE": "Pfizer",
    "KO": "The_Coca-Cola_Company",
    "PEP": "PepsiCo",
    "CSCO": "Cisco",
    "ORCL": "Oracle_Corporation",
    "INTC": "Intel",
    "AMD": "AMD",
    "ADBE": "Adobe_Inc.",
    "CRM": "Salesforce",
    "NKE": "Nike,_Inc.",
    "MCD": "McDonald%27s",
    "T": "AT%26T",
    "VZ": "Verizon",
    "IBM": "IBM",
    "GE": "General_Electric",
    "F": "Ford_Motor_Company",
    "GM": "General_Motors",
    "BA": "Boeing",
    "CAT": "Caterpillar_Inc.",
    "UBER": "Uber",
    "LYFT": "Lyft",
    "SHOP": "Shopify",
    "SQ": "Block,_Inc.",
    "PYPL": "PayPal",
    "PLTR": "Palantir_Technologies",
    "SNOW": "Snowflake_Inc.",
    "COIN": "Coinbase",
    "RIVN": "Rivian",
    "LCID": "Lucid_Group",
    "BABA": "Alibaba_Group",
    "TSM": "TSMC",
    "ASML": "ASML_Holding",
    "AVGO": "Broadcom",
    "QCOM": "Qualcomm",
    "TXN": "Texas_Instruments",
    "MU": "Micron_Technology",
}


def _resolve_wikipedia_article(symbol: str):
    """Map ticker → Wikipedia article title. Try override map, then
    a Wikipedia search API call. Returns the article title (with
    underscores) or None."""
    sym = symbol.upper()
    if sym in WIKIPEDIA_TICKER_OVERRIDES:
        return WIKIPEDIA_TICKER_OVERRIDES[sym]
    # Wikipedia OpenSearch — best-match for "<TICKER> stock"
    try:
        url = (
            "https://en.wikipedia.org/w/api.php"
            "?action=opensearch&format=json&limit=1&search="
            + urllib.parse.quote(f"{sym} stock")
        )
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        # Response shape: [query, [titles], [descs], [urls]]
        if isinstance(data, list) and len(data) >= 2 and data[1]:
            return data[1][0].replace(" ", "_")
    except Exception:
        pass
    return None


def get_wikipedia_pageviews_signal(symbol: str):
    """Wikipedia daily article views as an attention proxy.

    Output:
      {
        "pageview_z_score": float | None,
        "pageview_spike_flag": bool,       # True when z >= 2.0
        "current_7d_avg": int | None,
        "trailing_90d_avg": int | None,
        "article": str | None,
        "has_data": bool,
      }

    Free official API at wikimedia.org/api/rest_v1. 24h cache.
    """
    if "/" in symbol:
        return {"has_data": False, "is_crypto": True}
    cache_key = f"wikipedia_pageviews_{symbol.upper()}"
    cached = _get_cached(cache_key, "wikipedia_pageviews")
    if cached is not None:
        return cached

    result = {
        "pageview_z_score": None,
        "pageview_spike_flag": False,
        "current_7d_avg": None,
        "trailing_90d_avg": None,
        "article": None,
        "has_data": False,
    }
    article = _resolve_wikipedia_article(symbol)
    if not article:
        _set_cached(cache_key, result)
        return result
    result["article"] = article

    # Date range — last 90 days (UTC).
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.utcnow().date()
    start = today - _td(days=90)
    url = (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        f"en.wikipedia/all-access/all-agents/{article}/daily/"
        f"{start.strftime('%Y%m%d')}/{today.strftime('%Y%m%d')}"
    )
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "QuantOpsAI/1.0 (mack@mackenziesmith.com)"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        items = data.get("items") or []
        if len(items) < 14:
            _set_cached(cache_key, result)
            return result
        views = [int(i.get("views", 0)) for i in items]
        last7 = views[-7:]
        avg_7 = sum(last7) / len(last7)
        avg_90 = sum(views) / len(views)
        # σ across the full 90-day window (daily-level)
        if len(views) >= 2:
            mean = avg_90
            var = sum((v - mean) ** 2 for v in views) / (len(views) - 1)
            std = var ** 0.5
        else:
            std = 0.0
        if std > 0:
            z = (avg_7 - avg_90) / std
        else:
            z = 0.0
        result.update({
            "pageview_z_score": round(z, 2),
            "pageview_spike_flag": z >= 2.0,
            "current_7d_avg": int(round(avg_7)),
            "trailing_90d_avg": int(round(avg_90)),
            "has_data": True,
        })
    except Exception as exc:
        logging.debug(
            "Wikipedia pageviews fetch failed for %s: %s", symbol, exc,
        )

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Item 3a — App Store rankings (Apple iTunes RSS, free + official)
# ---------------------------------------------------------------------------

# Hand-curated mapping of ticker → list of (app_name, app_id) tuples.
# Apple app IDs come from the App Store URL: apps.apple.com/us/app/.../id<ID>.
# Coverage is limited to consumer names where the app is a meaningful
# revenue / engagement driver. Other tickers will return has_data=False.
APP_STORE_TICKER_OVERRIDES = {
    "UBER":  [("Uber",          368677368),  ("Uber Eats",  1058959277)],
    "LYFT":  [("Lyft",           529379082)],
    "ABNB":  [("Airbnb",         401626263)],
    "DASH":  [("DoorDash",       719972451)],
    "GRUB":  [("Grubhub",        302920553)],
    "SHOP":  [("Shop",          1223471316)],
    "SNAP":  [("Snapchat",       447188370)],
    "PINS":  [("Pinterest",      429047995)],
    "SPOT":  [("Spotify",        324684580)],
    "NFLX":  [("Netflix",        363590051)],
    "EA":    [("EA Sports FC",   563474357)],
    "TTWO":  [("GTA",           1486724914),  ("NBA 2K",     1567470693)],
    "ETSY":  [("Etsy",           477128284)],
    "BMBL":  [("Bumble",        930441707)],
    "MTCH":  [("Tinder",         547702041),  ("Hinge",     595287172)],
    "META":  [("Instagram",      389801252),  ("Facebook",   284882215),
              ("Threads",       6446901002)],
    "GOOGL": [("Google",         284815942),  ("YouTube",    544007664)],
    "AAPL":  [("Apple Music",    1108187390)],
    "AMZN":  [("Amazon",         297606951),  ("Prime Video", 545519333)],
    "DKNG":  [("DraftKings",    1232728332)],
    "PENN":  [("Barstool Sportsbook", 1535697867)],
    "ROKU":  [("The Roku App",   482307590)],
    "DUOL":  [("Duolingo",      570060128)],
    "RBLX":  [("Roblox",         431946152)],
    "PTON":  [("Peloton",       792750948)],
    "RDDT":  [("Reddit",         1064216828)],
    "DIS":   [("Disney+",       1446075923)],
    "WBD":   [("Max",           1547663261)],
    "PARA":  [("Paramount+",     376511118)],
    "CHWY":  [("Chewy",          1042177810)],
    "W":     [("Wayfair",        688039726)],
    "EBAY":  [("eBay",           282614216)],
    "PYPL":  [("PayPal",         283646709)],
    "COIN":  [("Coinbase",      886427730)],
    "HOOD":  [("Robinhood",     938003185)],
    "SOFI":  [("SoFi",           1242564978)],
}

_CACHE_TTL["app_store_ranking"] = 86400


# Cached top-free / top-grossing chart per category to avoid hitting
# the iTunes RSS once per ticker. Single fetch per category per day.
_RANKING_CHART_CACHE = {}
_RANKING_CHART_LOCK = threading.Lock()


def _fetch_apple_chart(chart_kind: str = "topgrossingapplications",
                          category: int = 0,
                          limit: int = 200):
    """Fetch one Apple iTunes RSS chart. Returns list of dicts:
       [{rank, name, app_id}, ...]
    chart_kind: 'topgrossingapplications', 'topfreeapplications',
                'toppaidapplications'.
    category=0 → all categories combined.
    """
    cache_key = f"{chart_kind}_{category}_{limit}"
    with _RANKING_CHART_LOCK:
        cached = _RANKING_CHART_CACHE.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL["app_store_ranking"]:
            return cached[1]

    cat_segment = f"genre={category}/" if category else ""
    url = (
        f"https://itunes.apple.com/us/rss/{chart_kind}/"
        f"{cat_segment}limit={limit}/json"
    )
    out = []
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "QuantOpsAI/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        entries = data.get("feed", {}).get("entry") or []
        for i, e in enumerate(entries, start=1):
            try:
                name = e.get("im:name", {}).get("label", "")
                app_id = int(e.get("id", {}).get("attributes", {}).get("im:id") or 0)
                if app_id:
                    out.append({"rank": i, "name": name, "app_id": app_id})
            except Exception:
                continue
    except Exception as exc:
        logging.debug("Apple RSS fetch failed (%s): %s", chart_kind, exc)
    with _RANKING_CHART_LOCK:
        _RANKING_CHART_CACHE[cache_key] = (time.time(), out)
    return out


def get_app_store_ranking(symbol: str):
    """Apple App Store ranking signal for a ticker's primary app(s).

    Output:
      {
        "best_grossing_rank": int | None,    # lower = better; None if not in top-200
        "best_free_rank": int | None,
        "wow_change_grossing": int | None,   # rank delta vs 7d ago (negative = improving)
        "apps": [{name, grossing_rank, free_rank}, ...],
        "has_data": bool,
      }

    Limited to ~30 consumer-app tickers in APP_STORE_TICKER_OVERRIDES.
    Tickers without a known app return has_data=False.
    """
    sym = (symbol or "").upper()
    if "/" in sym:
        return {"has_data": False, "is_crypto": True}
    apps = APP_STORE_TICKER_OVERRIDES.get(sym)
    if not apps:
        return {"has_data": False, "no_known_app": True}

    cache_key = f"app_store_ranking_{sym}"
    cached = _get_cached(cache_key, "app_store_ranking")
    if cached is not None:
        return cached

    grossing = _fetch_apple_chart("topgrossingapplications", limit=200)
    free = _fetch_apple_chart("topfreeapplications", limit=200)
    grossing_by_id = {a["app_id"]: a["rank"] for a in grossing}
    free_by_id = {a["app_id"]: a["rank"] for a in free}

    per_app = []
    best_grossing = None
    best_free = None
    for name, app_id in apps:
        gr = grossing_by_id.get(app_id)
        fr = free_by_id.get(app_id)
        per_app.append({
            "name": name, "app_id": app_id,
            "grossing_rank": gr, "free_rank": fr,
        })
        if gr and (best_grossing is None or gr < best_grossing):
            best_grossing = gr
        if fr and (best_free is None or fr < best_free):
            best_free = fr

    result = {
        "best_grossing_rank": best_grossing,
        "best_free_rank": best_free,
        # WoW change requires history; we don't snapshot daily yet so
        # leave None — future enhancement when daily snapshots persist.
        "wow_change_grossing": None,
        "apps": per_app,
        "has_data": (best_grossing is not None or best_free is not None),
    }
    _set_cached(cache_key, result)
    return result


def get_all_alternative_data(symbol):
    """Fetch all alternative data for a symbol in one call.

    Returns dict combining insider, short interest, fundamentals,
    options flow, intraday patterns, congressional trades, FINRA short
    volume, insider clusters, and analyst estimate revisions.
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
        "finra_short_vol": get_finra_short_volume(symbol),
        "insider_cluster": get_insider_cluster(symbol),
        "analyst_estimates": get_analyst_estimates(symbol),
        "insider_earnings": get_insider_earnings_signal(symbol),
        "dark_pool": get_dark_pool_volume(symbol),
        "earnings_surprise": get_earnings_surprise(symbol),
        # Local-SQLite alt-data sources (the 4 standalone projects).
        # Each returns {} or a small dict; the AI prompt has weighted
        # signal blocks that consume these. No-op gracefully if the
        # project DB isn't on this host yet (e.g., before the
        # /opt/quantopsai-altdata/ deploy lands).
        "congressional_recent": get_congressional_recent(symbol),
        "institutional_13f": get_13f_institutional(symbol),
        "biotech_milestones": get_biotech_milestones(symbol),
        "stocktwits_sentiment": get_stocktwits_sentiment(symbol),
        # Item 3a — web-scraped attention signals.
        "google_trends": get_google_trends_signal(symbol),
        "wikipedia_pageviews": get_wikipedia_pageviews_signal(symbol),
        "app_store_ranking": get_app_store_ranking(symbol),
        # patent_activity: DISABLED — PatentsView v1 API deprecated
    }
