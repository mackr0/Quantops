"""Market-wide macro data — yield curve, ETF flows, CBOE skew, economic indicators.

All data is FREE. FRED API uses DEMO_KEY (120 req/min). CBOE Skew via
yfinance. ETF flows computed from existing Alpaca bar data.

Cached in SQLite (same alt_data_cache table as alternative_data.py)
with per-type TTLs. These are market-wide, not per-symbol — they
provide macro context for the AI prompt and crisis detector.
"""

import json
import logging
import os
import sqlite3
import time
import threading
from contextlib import closing
from typing import Any, Dict
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger(__name__)

_DB_PATH = "quantopsai.db"
_http_lock = threading.Lock()

_CACHE_TTL = {
    "yield_curve": 86400,       # 24h
    "etf_flows": 86400,         # 24h
    "cboe_skew": 3600,          # 1h (intraday indicator)
    "fred_macro": 604800,       # 7d (monthly/weekly data)
    "cross_asset_vol": 3600,    # 1h (MOVE / OVX / GVZ — intraday)
}

_FRED_API_KEY = os.environ.get("FRED_API_KEY", "DEMO_KEY")
_USER_AGENT = "QuantOpsAI Research Bot"


# ---------------------------------------------------------------------------
# SQLite cache (same table as alternative_data.py)
# ---------------------------------------------------------------------------

_table_ensured = False


def _ensure_cache_table():
    try:
        with closing(sqlite3.connect(_DB_PATH)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alt_data_cache (
                    cache_key TEXT PRIMARY KEY,
                    data_json TEXT,
                    fetched_at REAL NOT NULL
                )
            """)
            conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _ci_exc:
        # Cache schema init; cache writes that fail leave callers
        # in non-cached path. Surface for follow-up.
        logger.warning(
            "macro_data cache schema init failed: %s: %s",
            type(_ci_exc).__name__, _ci_exc,
        )


def _get_cached(key, ttl_type):
    global _table_ensured
    if not _table_ensured:
        _ensure_cache_table()
        _table_ensured = True
    try:
        with closing(sqlite3.connect(_DB_PATH)) as conn:
            row = conn.execute(
                "SELECT data_json, fetched_at FROM alt_data_cache WHERE cache_key=?",
                (key,)
            ).fetchone()
        if row and (time.time() - row[1]) < _CACHE_TTL.get(ttl_type, 3600):
            return json.loads(row[0])
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            json.JSONDecodeError, TypeError, ValueError, OSError) as _cr_exc:
        # Cache read fallback; caller fetches from source on miss.
        # Surface for follow-up.
        logger.debug(
            "macro_data cache read failed for %s: %s: %s",
            key, type(_cr_exc).__name__, _cr_exc,
        )
    return None


def _set_cached(key, value):
    global _table_ensured
    if not _table_ensured:
        _ensure_cache_table()
        _table_ensured = True
    try:
        with closing(sqlite3.connect(_DB_PATH)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO alt_data_cache (cache_key, data_json, fetched_at) "
                "VALUES (?, ?, ?)",
                (key, json.dumps(value, default=str), time.time())
            )
            conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            TypeError, ValueError, OSError) as _cw_exc:
        # Cache write fallback; cache miss is acceptable next time.
        # Surface for follow-up.
        logger.debug(
            "macro_data cache write failed for %s: %s: %s",
            key, type(_cw_exc).__name__, _cw_exc,
        )


def _fred_fetch(series_id, limit=5):
    """Fetch latest observations from FRED API. Returns list of floats."""
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={_FRED_API_KEY}"
        f"&file_type=json&limit={limit}&sort_order=desc"
    )
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with _http_lock:
        resp = urlopen(req, timeout=15)
        raw = resp.read()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "FRED returned non-JSON for series_id=%s (len=%d); "
            "treating as no observations",
            series_id, len(raw or b""),
        )
        return []
    values = []
    for obs in data.get("observations", []):
        try:
            values.append(float(obs["value"]))
        except (ValueError, KeyError):
            pass
    return values


# ---------------------------------------------------------------------------
# 1. Treasury Yield Curve
# ---------------------------------------------------------------------------

def get_yield_curve() -> Dict[str, Any]:
    """Fetch Treasury yield curve from FRED.

    Returns dict with:
        rate_2y: float — 2-year Treasury rate
        rate_10y: float — 10-year Treasury rate
        rate_30y: float — 30-year Treasury rate
        fed_funds_upper: float — Fed funds upper target
        spread_10y_2y: float — 10y minus 2y (negative = inverted)
        curve_status: str — 'normal', 'flat', or 'inverted'
    """
    cached = _get_cached("yield_curve", "yield_curve")
    if cached is not None:
        return cached

    result = {
        "rate_2y": 0, "rate_10y": 0, "rate_30y": 0,
        "fed_funds_upper": 0, "spread_10y_2y": 0,
        "curve_status": "normal",
    }

    try:
        series = {"rate_2y": "DGS2", "rate_10y": "DGS10",
                  "rate_30y": "DGS30", "fed_funds_upper": "DFEDTARU"}
        for key, sid in series.items():
            try:
                vals = _fred_fetch(sid, limit=1)
                if vals:
                    result[key] = round(vals[0], 2)
            except (URLError, json.JSONDecodeError, KeyError, ValueError,
                    TypeError, OSError) as _yf_exc:
                # Per-series FRED fetch loop; one bad series
                # shouldn't kill the loop. Surface for follow-up.
                logger.debug(
                    "FRED yield-curve fetch failed for series %s: %s: %s",
                    sid, type(_yf_exc).__name__, _yf_exc,
                )

        if result["rate_10y"] > 0 and result["rate_2y"] > 0:
            spread = result["rate_10y"] - result["rate_2y"]
            result["spread_10y_2y"] = round(spread, 2)
            if spread < 0:
                result["curve_status"] = "inverted"
            elif abs(spread) < 0.25:
                result["curve_status"] = "flat"
            else:
                result["curve_status"] = "normal"
    except Exception as exc:
        logger.debug("Yield curve fetch failed: %s", exc)

    _set_cached("yield_curve", result)
    return result


# ---------------------------------------------------------------------------
# 2. ETF Sector Flow Estimates
# ---------------------------------------------------------------------------

def get_etf_flows() -> Dict[str, Dict[str, Any]]:
    """Estimate weekly sector ETF flows from volume and price changes.

    Returns dict keyed by sector name, each with:
        estimated_weekly_flow: float — dollar flow estimate
        flow_direction: str — 'inflow' or 'outflow'
        magnitude: str — 'strong', 'moderate', or 'weak'
    """
    cached = _get_cached("etf_flows", "etf_flows")
    if cached is not None:
        return cached

    result = {}

    try:
        from market_data import get_bars, SECTOR_ETFS

        for sector, etf in SECTOR_ETFS.items():
            try:
                df = get_bars(etf, limit=10)
                if df is None or df.empty or len(df) < 5:
                    continue

                last_5 = df.tail(5)
                daily_flows = []
                for i in range(1, len(last_5)):
                    vol = float(last_5["volume"].iloc[i])
                    price_change = float(last_5["close"].iloc[i] - last_5["close"].iloc[i - 1])
                    daily_flows.append(vol * price_change)

                weekly_flow = sum(daily_flows)
                abs_flow = abs(weekly_flow)

                if abs_flow > 1_000_000_000:
                    magnitude = "strong"
                elif abs_flow > 200_000_000:
                    magnitude = "moderate"
                else:
                    magnitude = "weak"

                result[sector] = {
                    "estimated_weekly_flow": round(weekly_flow),
                    "flow_direction": "inflow" if weekly_flow > 0 else "outflow",
                    "magnitude": magnitude,
                }
            except (KeyError, ValueError, AttributeError, TypeError,
                    IndexError, OSError) as _ef_exc:
                # Per-sector ETF flow loop; one bad sector shouldn't
                # kill the loop. Surface for follow-up.
                logger.debug(
                    "ETF-flow fetch failed for sector %s (%s): %s: %s",
                    sector, etf, type(_ef_exc).__name__, _ef_exc,
                )
                continue
    except Exception as exc:
        logger.debug("ETF flows fetch failed: %s", exc)

    _set_cached("etf_flows", result)
    return result


# ---------------------------------------------------------------------------
# 3. CBOE Skew Index
# ---------------------------------------------------------------------------

def get_cboe_skew() -> Dict[str, Any]:
    """Fetch CBOE Skew Index — measures institutional tail-risk hedging.

    Returns dict with:
        skew_value: float — current CBOE Skew reading
        skew_signal: str — 'normal', 'elevated', or 'extreme'
        skew_5d_avg: float — 5-day average
    """
    cached = _get_cached("cboe_skew", "cboe_skew")
    if cached is not None:
        return cached

    result = {"skew_value": 0, "skew_signal": "normal", "skew_5d_avg": 0}

    try:
        import yfinance as yf
        import yf_lock as _yfl
        with _yfl._lock:
            ticker = yf.Ticker("^SKEW")
            hist = ticker.history(period="10d")

        if hist is not None and not hist.empty:
            # Column may be "Close" (yfinance) — normalize
            close_col = "Close" if "Close" in hist.columns else "close"
            if close_col in hist.columns:
                closes = hist[close_col].dropna()
                if len(closes) > 0:
                    result["skew_value"] = round(float(closes.iloc[-1]), 1)
                if len(closes) >= 5:
                    result["skew_5d_avg"] = round(float(closes.tail(5).mean()), 1)

                skew = result["skew_value"]
                if skew >= 150:
                    result["skew_signal"] = "extreme"
                elif skew >= 140:
                    result["skew_signal"] = "elevated"
                else:
                    result["skew_signal"] = "normal"
    except Exception as exc:
        logger.debug("CBOE skew fetch failed: %s", exc)

    _set_cached("cboe_skew", result)
    return result


# ---------------------------------------------------------------------------
# 4. FRED Leading Economic Indicators
# ---------------------------------------------------------------------------

def get_fred_macro() -> Dict[str, Any]:
    """Fetch leading economic indicators from FRED.

    Returns dict with:
        unemployment_rate: float
        unemployment_trend: str — 'rising', 'falling', 'stable'
        cpi_yoy: float — CPI year-over-year %
        consumer_sentiment: float — U of Michigan index
        consumer_sentiment_trend: str
        initial_claims_4wk_avg: float
    """
    cached = _get_cached("fred_macro", "fred_macro")
    if cached is not None:
        return cached

    result = {
        "unemployment_rate": 0, "unemployment_trend": "stable",
        "cpi_yoy": 0,
        "consumer_sentiment": 0, "consumer_sentiment_trend": "stable",
        "initial_claims_4wk_avg": 0,
    }

    try:
        # Unemployment (UNRATE) — latest 3 observations
        try:
            vals = _fred_fetch("UNRATE", limit=3)
            if vals:
                result["unemployment_rate"] = vals[0]
                if len(vals) >= 3:
                    if vals[0] > vals[-1] + 0.2:
                        result["unemployment_trend"] = "rising"
                    elif vals[0] < vals[-1] - 0.2:
                        result["unemployment_trend"] = "falling"
        except (URLError, json.JSONDecodeError, KeyError, ValueError,
                TypeError, IndexError, OSError) as _ur_exc:
            # Unemployment annotation; rest of macro dict still
            # returned. Surface for follow-up.
            logger.debug(
                "FRED unemployment annotation failed: %s: %s",
                type(_ur_exc).__name__, _ur_exc,
            )

        # CPI (CPIAUCSL) — compute YoY from 13-month span
        try:
            vals = _fred_fetch("CPIAUCSL", limit=13)
            if vals and len(vals) >= 13:
                result["cpi_yoy"] = round((vals[0] / vals[12] - 1) * 100, 1)
        except (URLError, json.JSONDecodeError, KeyError, ValueError,
                TypeError, IndexError, ZeroDivisionError, OSError) as _cpi_exc:
            # CPI annotation; rest of macro dict still returned.
            # Surface for follow-up.
            logger.debug(
                "FRED CPI annotation failed: %s: %s",
                type(_cpi_exc).__name__, _cpi_exc,
            )

        # Consumer Sentiment (UMCSENT) — latest 3
        try:
            vals = _fred_fetch("UMCSENT", limit=3)
            if vals:
                result["consumer_sentiment"] = round(vals[0], 1)
                if len(vals) >= 3:
                    if vals[0] > vals[-1] + 2:
                        result["consumer_sentiment_trend"] = "rising"
                    elif vals[0] < vals[-1] - 2:
                        result["consumer_sentiment_trend"] = "falling"
        except (URLError, json.JSONDecodeError, KeyError, ValueError,
                TypeError, IndexError, OSError) as _cs_exc:
            # Consumer-sentiment annotation; rest of macro dict
            # still returned. Surface for follow-up.
            logger.debug(
                "FRED consumer-sentiment annotation failed: %s: %s",
                type(_cs_exc).__name__, _cs_exc,
            )

        # Initial Jobless Claims (ICSA) — 4-week avg
        try:
            vals = _fred_fetch("ICSA", limit=4)
            if vals:
                result["initial_claims_4wk_avg"] = round(sum(vals) / len(vals))
        except (URLError, json.JSONDecodeError, KeyError, ValueError,
                TypeError, IndexError, ZeroDivisionError, OSError) as _cl_exc:
            # Claims annotation; rest of macro dict still returned.
            # Surface for follow-up.
            logger.debug(
                "FRED initial-claims annotation failed: %s: %s",
                type(_cl_exc).__name__, _cl_exc,
            )

    except Exception as exc:
        logger.debug("FRED macro fetch failed: %s", exc)

    _set_cached("fred_macro", result)
    return result


# ---------------------------------------------------------------------------
# 5. Sector Momentum Ranking
# ---------------------------------------------------------------------------

def get_sector_momentum_ranking() -> Dict[str, Any]:
    """Rank 11 sector ETFs by 5-day momentum. Detect risk-on vs risk-off.

    Uses existing get_sector_rotation() from market_data.py — no new
    external calls.

    Returns dict with:
        rankings: list of {sector, return_5d, rank} sorted by rank
        top_3: list of sector names
        bottom_3: list of sector names
        rotation_phase: str — 'risk_on', 'risk_off', or 'mixed'
    """
    cached = _get_cached("sector_momentum", "etf_flows")  # same 24h TTL
    if cached is not None:
        return cached

    result = {
        "rankings": [],
        "top_3": [],
        "bottom_3": [],
        "rotation_phase": "mixed",
    }

    try:
        from market_data import get_sector_rotation
        rotation = get_sector_rotation()
        if not rotation:
            _set_cached("sector_momentum", result)
            return result

        # Sort sectors by 5-day return descending
        ranked = sorted(
            [(sector, data.get("return_5d", 0)) for sector, data in rotation.items()],
            key=lambda x: x[1],
            reverse=True,
        )

        rankings = []
        for i, (sector, ret) in enumerate(ranked):
            rankings.append({"sector": sector, "return_5d": ret, "rank": i + 1})

        result["rankings"] = rankings
        result["top_3"] = [r["sector"] for r in rankings[:3]]
        result["bottom_3"] = [r["sector"] for r in rankings[-3:]]

        # Detect rotation phase
        risk_on_sectors = {"tech", "consumer_disc", "finance"}
        risk_off_sectors = {"utilities", "consumer_staples", "healthcare"}
        top_set = set(result["top_3"])
        bottom_set = set(result["bottom_3"])

        risk_on_score = len(top_set & risk_on_sectors) - len(bottom_set & risk_on_sectors)
        risk_off_score = len(top_set & risk_off_sectors) - len(bottom_set & risk_off_sectors)

        if risk_on_score >= 2:
            result["rotation_phase"] = "risk_on"
        elif risk_off_score >= 2:
            result["rotation_phase"] = "risk_off"
        else:
            result["rotation_phase"] = "mixed"

    except Exception as exc:
        logger.debug("Sector momentum ranking failed: %s", exc)

    _set_cached("sector_momentum", result)
    return result


# ---------------------------------------------------------------------------
# 5. Cross-asset volatility (MOVE / OVX / GVZ)  —  2026-05-17 #3 Tier-1
# ---------------------------------------------------------------------------

# Three CBOE vol indices that complement VIX. Each measures implied vol
# in a different asset class — useful for distinguishing "equity vol
# spike alone" from "broad cross-asset stress."
#   ^MOVE  Merrill Lynch Treasury Bond Option Volatility (1-month
#          implied vol of US Treasury options)
#   ^OVX   CBOE Crude Oil Volatility Index (1-month implied vol on
#          USO front-month options)
#   ^GVZ   CBOE Gold ETF Volatility Index (1-month implied vol on
#          GLD options)
#
# Why 30d percentile vs raw value: each index has its own typical
# range (MOVE typically 70-160; OVX typically 30-80; GVZ typically
# 12-30). Percentile-rank is the comparable cross-asset signal.

_VOL_INDEX_TICKERS = {
    "move": "^MOVE",
    "ovx": "^OVX",
    "gvz": "^GVZ",
}


def get_cross_asset_vol() -> Dict[str, Any]:
    """Pull MOVE / OVX / GVZ from yfinance, compute current value
    + 30-day percentile rank. Returns:
      {
        move: {current, p30d, p30d_label}, ...
        # p30d_label: 'low' (<25), 'normal', 'elevated' (>75), 'extreme' (>95)
      }
    Returns empty {} on full failure; per-index missing data is `None`.
    """
    cached = _get_cached("cross_asset_vol", "cross_asset_vol")
    if cached is not None:
        return cached

    result: Dict[str, Any] = {}
    try:
        import yfinance as yf
        import yf_lock as _yfl
        for key, ticker_symbol in _VOL_INDEX_TICKERS.items():
            try:
                with _yfl._lock:
                    t = yf.Ticker(ticker_symbol)
                    hist = t.history(period="35d")
                if hist is None or hist.empty:
                    result[key] = {
                        "current": None, "p30d": None,
                        "p30d_label": "unavailable",
                    }
                    continue
                col = "Close" if "Close" in hist.columns else "close"
                closes = hist[col].dropna()
                if len(closes) == 0:
                    result[key] = {
                        "current": None, "p30d": None,
                        "p30d_label": "unavailable",
                    }
                    continue
                cur = float(closes.iloc[-1])
                window = closes.tail(30) if len(closes) >= 30 else closes
                # Percentile rank: what fraction of recent observations
                # were at or below the current value?
                rank = float((window <= cur).sum()) / float(len(window))
                pct = round(rank * 100, 1)
                if pct >= 95:
                    label = "extreme"
                elif pct >= 75:
                    label = "elevated"
                elif pct <= 25:
                    label = "low"
                else:
                    label = "normal"
                result[key] = {
                    "current": round(cur, 1),
                    "p30d": pct,
                    "p30d_label": label,
                }
            except Exception as exc:
                logger.debug(
                    "cross-asset vol fetch failed for %s: %s",
                    ticker_symbol, exc,
                )
                result[key] = {
                    "current": None, "p30d": None,
                    "p30d_label": "unavailable",
                }
    except ImportError:
        logger.warning(
            "cross_asset_vol: yfinance not available — vol indices unfetched"
        )

    _set_cached("cross_asset_vol", result)
    return result


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def get_all_macro_data() -> Dict[str, Any]:
    """Fetch all market-wide macro data in one call.

    Returns dict combining yield curve, ETF flows, CBOE skew,
    FRED economic indicators, sector momentum ranking, and the
    MOVE/OVX/GVZ cross-asset vol indices.
    """
    return {
        "yield_curve": get_yield_curve(),
        "etf_flows": get_etf_flows(),
        "cboe_skew": get_cboe_skew(),
        "fred_macro": get_fred_macro(),
        "sector_momentum": get_sector_momentum_ranking(),
        "market_gex": get_market_gex_aggregate(),
        # 2026-05-17 #3 Tier-1: MOVE (bond vol), OVX (oil vol), GVZ
        # (gold vol). Differentiates equity-only stress from broad
        # cross-asset stress. Each carries 30d percentile rank.
        "cross_asset_vol": get_cross_asset_vol(),
    }


# ---------------------------------------------------------------------------
# 6. Market-Wide Gamma Exposure Aggregate
# ---------------------------------------------------------------------------

def get_market_gex_aggregate() -> Dict[str, Any]:
    """Aggregate GEX regime from recent AI predictions across all profiles.

    Rather than making expensive options API calls for 20+ symbols,
    reads the most recent predictions' features_json which already
    contains per-stock GEX data from the last scan cycle.

    Returns dict with:
        net_regime: str — 'pinning', 'expansion', or 'balanced'
        pct_positive: float — % of recent stocks with positive GEX
        sample_size: int
    """
    cached = _get_cached("market_gex", "cboe_skew")  # 1h TTL
    if cached is not None:
        return cached

    result = {
        "net_regime": "balanced",
        "pct_positive": 0.5,
        "sample_size": 0,
    }

    try:
        import sqlite3 as _sq
        import glob

        positive_count = 0
        total_count = 0

        for db_path in glob.glob("quantopsai_profile_*.db"):
            try:
                conn = _sq.connect(db_path)
                rows = conn.execute(
                    "SELECT features_json FROM ai_predictions "
                    "WHERE features_json IS NOT NULL "
                    "ORDER BY timestamp DESC LIMIT 30"
                ).fetchall()
                conn.close()

                for row in rows:
                    try:
                        features = json.loads(row[0])
                        oracle = features.get("options_oracle", {})
                        if isinstance(oracle, dict):
                            gex = oracle.get("gex", {})
                            if isinstance(gex, dict) and gex.get("gex_sign"):
                                total_count += 1
                                if gex["gex_sign"] == "positive":
                                    positive_count += 1
                    except (json.JSONDecodeError, TypeError):
                        continue
            except (_sq.OperationalError, _sq.DatabaseError, OSError) as _gex_exc:
                # Per-DB GEX aggregation loop; one bad DB shouldn't
                # kill the loop. Surface for follow-up.
                logger.debug(
                    "GEX aggregation failed for %s: %s: %s",
                    db_path, type(_gex_exc).__name__, _gex_exc,
                )
                continue

        if total_count >= 5:
            pct = positive_count / total_count
            result["pct_positive"] = round(pct, 2)
            result["sample_size"] = total_count
            if pct >= 0.65:
                result["net_regime"] = "pinning"
            elif pct <= 0.35:
                result["net_regime"] = "expansion"

    except Exception as exc:
        logger.debug("Market GEX aggregate failed: %s", exc)

    _set_cached("market_gex", result)
    return result
