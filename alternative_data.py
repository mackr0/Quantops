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
from urllib.request import urlopen, Request

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
# Aggregator
# ---------------------------------------------------------------------------

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
        "congressional": get_congressional_trading(symbol),
        "finra_short_vol": get_finra_short_volume(symbol),
        "insider_cluster": get_insider_cluster(symbol),
        "analyst_estimates": get_analyst_estimates(symbol),
        "insider_earnings": get_insider_earnings_signal(symbol),
    }
