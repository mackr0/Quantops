"""Per-symbol sector classification with SQLite cache.

DYNAMIC_UNIVERSE_PLAN.md Step 1 — replaces the hardcoded ~50-symbol
`_SECTOR_MAP` in `market_data._guess_sector()`.

WHY yfinance, not Alpaca
------------------------
Verified 2026-04-27 via direct curl to `/v2/assets/AAPL`: Alpaca's
asset endpoint exposes only:

    id, class, exchange, symbol, name, status, tradable, marginable,
    shortable, easy_to_borrow, fractionable,
    maintenance_margin_requirement, margin_requirement_long/short,
    attributes (fractional_eh_enabled, has_options, overnight_tradable)

NO `sector`, NO `industry`, NO GICS classification. Alpaca is a
brokerage API — fundamentals data like sector is out of scope for
their paper / Algo Trader Plus tiers. yfinance's
`Ticker(sym).info["sector"]` is the documented free path with the
GICS taxonomy that the sector-momentum strategy expects.

Same well-justified yfinance use as the other 3 call sites in the
codebase (alt-data fundamentals, earnings calendar, backtest fallback).

Lookup order
------------
1. **Cache-first** in master `quantopsai.db` table
   `sector_cache(symbol, sector, fetched_at)`. 7-day TTL — sector
   classification rarely changes for stable names. Most lookups
   hit cache and never touch yfinance.
2. **yfinance** on cache miss — `Ticker(sym).info["sector"]` returns
   a GICS sector string we map to our 11 internal keys.
3. **Static fallback map** for the top ~100 symbols when yfinance is
   unreachable or returns nothing useful (offline mode safety net).
4. **`"unclassified"` default** when nothing else lands — honest
   unknown, negative-cached so unmappable names don't re-fetch on
   every call (the old `"tech"` default silently inflated the
   perceived tech share of the universe).

Cold yfinance lookups happen only on brand-new tickers — 1-3/week as
the universe rotates. Production load is negligible.

Live `market_data._guess_sector` becomes a thin wrapper around
`get_sector(symbol)` from this module.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# 7-day TTL — sector classification rarely changes for a stable name.
_TTL_SECONDS = 7 * 24 * 3600

MASTER_DB = os.environ.get("QUANTOPSAI_MASTER_DB", "quantopsai.db")

_schema_lock = threading.Lock()
_schema_initialized: set = set()

# Internal taxonomy — the full 11 GICS sectors (2026-07-08, universe-
# funnel rework). The old 7-bucket map folded utilities/staples/
# materials/real-estate into industrial/consumer_disc/finance, which
# made defensive sectors literally unrepresentable in the candidate
# funnel: the AI was told "rotate into utilities" by its own market
# context while no utilities name could ever reach its menu. Keys
# deliberately match market_data.SECTOR_ETFS exactly, so the sector-
# momentum rotation signal joins against these with no mapping layer.
INTERNAL_SECTORS = {
    "tech", "finance", "energy", "healthcare",
    "consumer_disc", "industrial", "comm_services",
    "consumer_staples", "utilities", "materials", "real_estate",
}

# GICS / yfinance sector strings → internal key.
_GICS_TO_INTERNAL = {
    "Technology": "tech",
    "Communication Services": "comm_services",
    "Communications": "comm_services",
    "Financial Services": "finance",
    "Financial": "finance",
    "Energy": "energy",
    "Healthcare": "healthcare",
    "Consumer Cyclical": "consumer_disc",
    "Consumer Discretionary": "consumer_disc",
    "Consumer Defensive": "consumer_staples",
    "Consumer Staples": "consumer_staples",
    "Industrials": "industrial",
    "Basic Materials": "materials",
    "Materials": "materials",
    "Real Estate": "real_estate",
    "Utilities": "utilities",
}

# Hand-curated fallback for the top ~100 symbols. Used only when
# yfinance is unreachable AND the cache is empty. Same shape as the
# old _SECTOR_MAP that lived in market_data — preserved here so
# offline/firewalled environments keep classifying the canonical
# names correctly.
_FALLBACK_MAP = {
    "tech": {
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AMD", "INTC",
        "CRM", "ORCL", "ADBE", "NOW", "DDOG", "NET", "ZS", "SNOW", "MDB",
        "AVGO", "TXN", "MU", "QCOM", "ASML", "AMAT", "LRCX", "KLAC",
        "PLTR", "PANW", "FTNT", "CRWD", "OKTA", "TEAM", "INTU", "WDAY",
    },
    "finance": {
        "SOFI", "HOOD", "AFRM", "UPST", "COIN", "SQ", "V", "MA", "JPM",
        "BAC", "GS", "MS", "WFC", "AXP", "BLK", "SCHW", "ALLY", "LC",
        "C", "USB", "PNC", "TFC", "PYPL", "FIS", "FISV",
    },
    "energy": {
        "RIG", "ET", "AR", "CNX", "BTU", "KOS", "BTE", "OVV", "PLUG",
        "FCEL", "BE", "RUN", "XOM", "CVX", "COP", "OXY", "EOG", "MPC",
        "PSX", "VLO", "SLB",
    },
    "healthcare": {
        "UNH", "JNJ", "PFE", "MRK", "LLY", "AMGN", "GILD", "ISRG",
        "HIMS", "DNA", "WVE", "CRSP", "NTLA", "BEAM", "MRNA", "BNTX",
        "REGN", "VRTX", "BIIB", "BMY", "ABBV",
    },
    "consumer_disc": {
        "TSLA", "NKE", "SBUX", "MCD", "LULU", "DECK", "RIVN",
        "LCID", "NIO", "CVNA", "ETSY", "CHWY", "HD", "LOW", "TJX",
        "BKNG", "MAR", "HLT", "ABNB",
    },
    "industrial": {
        "BA", "RTX", "LMT", "GE", "HON", "CAT", "DE", "JOBY", "GD",
        "NOC", "MMM", "UPS", "FDX", "UNP", "EMR", "ETN", "ROK",
    },
    "comm_services": {
        "NFLX", "DIS", "ROKU", "SNAP", "PINS", "RBLX", "DKNG", "TMUS",
        "T", "VZ", "CMCSA", "WBD", "PARA", "LYV",
    },
    "consumer_staples": {
        "KO", "PEP", "PG", "WMT", "COST", "CL", "KMB", "GIS", "K",
        "MO", "PM", "STZ", "TGT", "DG", "DLTR", "KR", "SYY", "KDP",
        "MDLZ", "HSY", "TSN",
    },
    "utilities": {
        "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL", "ED",
        "PCG", "WEC", "ES", "PEG", "CEG", "VST", "NRG", "AES",
    },
    "materials": {
        "LIN", "APD", "SHW", "FCX", "NEM", "NUE", "ECL", "DOW", "DD",
        "VALE", "AA", "CLF", "X", "STLD", "MP", "ALB", "GOLD", "SCCO",
    },
    "real_estate": {
        "PLD", "AMT", "EQIX", "SPG", "O", "PSA", "CCI", "DLR", "WELL",
        "AVB", "EQR", "VTR", "IRM", "VICI",
    },
}

# Unknown symbols are honestly UNKNOWN — the old "tech" default
# silently inflated the perceived tech share of the universe (31 of
# today's 100 universe slots were uncached and all read as tech).
# "unclassified" is deliberately NOT in INTERNAL_SECTORS: it never
# gets a stratification floor and the concentration counters skip it
# (an unknown shared by two holdings says nothing). It IS written to
# sector_cache as a NEGATIVE entry (funnel M5) so unmappable names
# don't re-fetch on every stratifier refresh.
_DEFAULT_SECTOR = "unclassified"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _init_schema(db_path: str = MASTER_DB) -> None:
    if not db_path:
        return
    with _schema_lock:
        if db_path in _schema_initialized:
            return
        try:
            with closing(sqlite3.connect(db_path, timeout=15)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sector_cache (
                        symbol     TEXT PRIMARY KEY,
                        sector     TEXT NOT NULL,
                        fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                conn.commit()
            _schema_initialized.add(db_path)
        except Exception as exc:
            logger.warning("Failed to init sector_cache: %s", exc)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _read_cache(symbol: str, db_path: str = MASTER_DB) -> Optional[str]:
    """Return cached sector for `symbol` if present and not stale.
    None on miss / stale / error."""
    if not symbol or not db_path:
        return None
    _init_schema(db_path)
    try:
        with closing(sqlite3.connect(db_path, timeout=15)) as conn:
            row = conn.execute(
                "SELECT sector, fetched_at FROM sector_cache WHERE symbol = ?",
                (symbol.upper(),),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    sector, fetched_at = row
    try:
        ts = datetime.fromisoformat(fetched_at)
        if datetime.utcnow() - ts > timedelta(seconds=_TTL_SECONDS):
            return None
    except Exception:
        # If timestamp is malformed, treat as stale.
        return None
    if sector in INTERNAL_SECTORS or sector == _DEFAULT_SECTOR:
        return sector  # negative entries are hits too (funnel M5)
    return None


def _write_cache(symbol: str, sector: str, db_path: str = MASTER_DB) -> None:
    # 'unclassified' is a valid NEGATIVE entry (funnel-review M5): an
    # unmappable name would otherwise burn a live lookup on every
    # stratifier refresh, forever. Same 7-day TTL — a later listing/
    # classification change heals within a week.
    if not symbol or not sector or (
            sector not in INTERNAL_SECTORS and sector != _DEFAULT_SECTOR):
        return
    if not db_path:
        return
    _init_schema(db_path)
    try:
        with closing(sqlite3.connect(db_path, timeout=15)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sector_cache "
                "(symbol, sector, fetched_at) VALUES (?, ?, datetime('now'))",
                (symbol.upper(), sector),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("Failed to write sector_cache for %s: %s", symbol, exc)


# ---------------------------------------------------------------------------
# yfinance lookup
# ---------------------------------------------------------------------------

def _yfinance_sector(symbol: str) -> Optional[str]:
    """Return the internal sector key from yfinance's GICS string, or
    None on any failure / unmappable result.

    Skip yfinance entirely for symbols Alpaca doesn't carry — they
    will return "possibly delisted" errors and pollute the logs.
    Per the Alpaca-first data-source rule.
    """
    try:
        from screener import is_alpaca_active
        if not is_alpaca_active(symbol):
            return None
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
        gics = info.get("sector")
        if not gics:
            return None
        return _GICS_TO_INTERNAL.get(gics)
    except Exception:
        return None


def _fallback_sector(symbol: str) -> Optional[str]:
    """Look up `symbol` in the in-module hardcoded fallback. None on miss."""
    sym = symbol.upper()
    for sector, names in _FALLBACK_MAP.items():
        if sym in names:
            return sector
    return None


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def get_sector(symbol: str, db_path: str = MASTER_DB) -> str:
    """Return the internal sector key for `symbol`. Order:

    1. Cache hit (≤ 7 days old)
    2. yfinance GICS lookup (writes cache on success)
    3. Fallback map (writes cache on success)
    4. Default "unclassified" (negative-cached so unmappable names
       don't re-fetch on every call)
    """
    if not symbol:
        return _DEFAULT_SECTOR
    sym = symbol.upper()

    cached = _read_cache(sym, db_path)
    if cached:
        return cached

    sector = _yfinance_sector(sym)
    if sector:
        _write_cache(sym, sector, db_path)
        return sector

    fb = _fallback_sector(sym)
    if fb:
        _write_cache(sym, fb, db_path)
        return fb

    _write_cache(sym, _DEFAULT_SECTOR, db_path)  # negative cache (M5)
    return _DEFAULT_SECTOR


def get_sectors_cached_bulk(symbols, db_path: str = MASTER_DB) -> dict:
    """{symbol: sector} for CACHE HITS ONLY — one SELECT, zero network.

    Built for the universe stratifier, which classifies hundreds of
    candidates per refresh: calling get_sector() per symbol there
    would stampede the fundamentals source on cache misses. Misses
    are simply absent from the result (callers treat them as
    unclassified); the bounded full-lookup pass that follows the
    stratifier's provisional cut fills the cache over time. Respects
    the same TTL as get_sector; falls back to the hardcoded map for
    misses (offline parity). Fail-open: errors return {}."""
    out = {}
    syms = [s.upper() for s in (symbols or []) if s]
    if not syms:
        return out
    _init_schema(db_path)
    try:
        with closing(sqlite3.connect(db_path, timeout=15)) as conn:
            placeholders = ",".join("?" for _ in syms)
            rows = conn.execute(
                f"SELECT symbol, sector, fetched_at FROM sector_cache "
                f"WHERE symbol IN ({placeholders})", syms,
            ).fetchall()
        cutoff = datetime.utcnow() - timedelta(seconds=_TTL_SECONDS)
        for sym, sector, fetched_at in rows:
            try:
                if datetime.fromisoformat(fetched_at) >= cutoff and (
                        sector in INTERNAL_SECTORS or
                        sector == _DEFAULT_SECTOR):
                    out[sym] = sector
            except Exception as exc:
                logger.debug("bulk sector cache row malformed for %s: %s",
                             sym, exc)
    except Exception as exc:
        logger.warning("bulk sector cache read failed: %s — stratifier "
                       "will treat all symbols as cache misses this "
                       "refresh", exc)
    for sym in syms:
        if sym not in out:
            fb = _fallback_sector(sym)
            if fb:
                out[sym] = fb
    return out


# ---------------------------------------------------------------------------
# Company profile (name / industry / market cap) — same yfinance payload
# ---------------------------------------------------------------------------
# The symbol-info modal needs fundamentals Alpaca does not carry (market
# cap, industry). This module is the ONE sanctioned yfinance touchpoint
# (Alpaca-first rule), and `yf.Ticker(sym).info` — already fetched above
# for the sector — carries them in the same payload, so the profile
# cache adds ZERO new yfinance call sites and zero extra HTTP when a
# lookup also warms the sector cache. Own table (company_profile_cache),
# same 7-day TTL; sector_cache's shape and readers are untouched.

_PROFILE_TTL_SECONDS = _TTL_SECONDS


def _init_profile_schema(db_path: str) -> None:
    if not db_path:
        return
    with _schema_lock:
        if ("profile", db_path) in _schema_initialized:
            return
        try:
            with closing(sqlite3.connect(db_path, timeout=15)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS company_profile_cache (
                        symbol     TEXT PRIMARY KEY,
                        name       TEXT,
                        industry   TEXT,
                        market_cap REAL,
                        gics_sector TEXT,
                        fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                conn.commit()
            _schema_initialized.add(("profile", db_path))
        except Exception as exc:
            logger.warning("Failed to init company_profile_cache: %s", exc)


def get_company_profile(symbol: str,
                        db_path: str = MASTER_DB) -> dict:
    """Company fundamentals for the symbol-info modal:
    {name, industry, market_cap, gics_sector} — any field may be None.

    Cache-first (7-day TTL). On miss, ONE yf.Ticker().info fetch
    (guarded by is_alpaca_active, like the sector path) fills this
    cache AND warms sector_cache when the GICS sector maps — the
    modal and the trading path share the payload. Negative results
    are cached too (all-None row) so an unknown symbol doesn't
    re-fetch on every tap. Fail-open: any error returns all-None."""
    empty = {"name": None, "industry": None, "market_cap": None,
             "gics_sector": None}
    if not symbol or not db_path:
        return dict(empty)
    sym = symbol.upper()
    _init_profile_schema(db_path)
    try:
        with closing(sqlite3.connect(db_path, timeout=15)) as conn:
            row = conn.execute(
                "SELECT name, industry, market_cap, gics_sector, "
                "fetched_at FROM company_profile_cache WHERE symbol = ?",
                (sym,),
            ).fetchone()
        if row:
            try:
                ts = datetime.fromisoformat(row[4])
                if datetime.utcnow() - ts <= timedelta(
                        seconds=_PROFILE_TTL_SECONDS):
                    return {"name": row[0], "industry": row[1],
                            "market_cap": row[2], "gics_sector": row[3]}
            except Exception as exc:
                logger.debug("company profile cache timestamp malformed "
                             "for %s (%s) — refetching", sym, exc)
    except Exception as exc:
        logger.debug("company profile cache read failed for %s: %s",
                     sym, exc)

    result = dict(empty)
    try:
        from screener import is_alpaca_active
        if is_alpaca_active(sym):
            import yfinance as yf
            info = yf.Ticker(sym).info or {}
            result = {
                "name": info.get("longName") or info.get("shortName"),
                "industry": info.get("industry"),
                "market_cap": info.get("marketCap"),
                "gics_sector": info.get("sector"),
            }
            # same payload warms the trading path's sector cache
            mapped = _GICS_TO_INTERNAL.get(info.get("sector") or "")
            if mapped:
                _write_cache(sym, mapped, db_path)
    except Exception as exc:
        logger.debug("company profile fetch failed for %s: %s", sym, exc)

    try:
        with closing(sqlite3.connect(db_path, timeout=15)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO company_profile_cache "
                "(symbol, name, industry, market_cap, gics_sector, "
                " fetched_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
                (sym, result["name"], result["industry"],
                 result["market_cap"], result["gics_sector"]),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("company profile cache write failed for %s: %s",
                     sym, exc)
    return result
