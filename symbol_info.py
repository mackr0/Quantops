"""Symbol-info assembly for the tap-a-ticker modal (2026-07-08).

One public function: get_symbol_info(symbol) → dict | None. Returns the
standard company snapshot the UI modal renders — name, exchange, live
price and day change, 52-week range, average volume, market cap, sector,
industry.

Data sources, Alpaca-first (the load-bearing data rule):
  • name / exchange       — symbol_names cache → Alpaca get_asset
                            (asset.name is cached back for next time)
  • price / day change    — Alpaca snapshot (daily bar + previous
                            daily bar; no new fetch machinery)
  • 52-week range / avg   — Alpaca daily bars (market_data.get_bars)
  • market cap / sector / — sector_classifier.get_company_profile
    industry                (the ONE sanctioned yfinance touchpoint;
                             7-day cached; same payload the sector
                             path already fetches)

Per-source defensive: a hiccup in one source nulls that field, never
the whole modal. Returns None only when the symbol itself is
unresolvable (not a known Alpaca asset) so the route can 404.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# OCC option symbols (UNDERLYING + YYMMDD + C/P + strike) must never be
# looked up as companies — same heuristic family as the order guard.
_OCC_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,5}\d{6}[CP]\d{8}$")


def _valid_symbol(symbol: str) -> bool:
    s = (symbol or "").upper()
    return bool(_SYMBOL_RE.match(s)) and not _OCC_RE.match(s)


def _asset_fields(sym: str) -> Optional[dict]:
    """Name + exchange from cache/Alpaca. None => unknown symbol."""
    name = None
    try:
        from models import get_symbol_names
        name = (get_symbol_names([sym]) or {}).get(sym)
    except Exception as exc:
        logger.debug("symbol name cache read failed for %s: %s", sym, exc)
    exchange = None
    tradable = None
    try:
        from client import get_api
        asset = get_api(None).get_asset(sym)
        exchange = getattr(asset, "exchange", None)
        tradable = bool(getattr(asset, "tradable", False))
        a_name = getattr(asset, "name", None)
        if a_name and not name:
            name = a_name
            try:
                from models import cache_symbol_names
                cache_symbol_names({sym: a_name})
            except Exception as exc:
                logger.debug("symbol name cache write failed for %s: %s",
                             sym, exc)
    except Exception as exc:
        # 404 = genuinely unknown symbol; other errors = degraded broker
        msg = str(exc).lower()
        if "not found" in msg or "404" in msg or \
                getattr(exc, "status_code", None) == 404:
            if not name:
                return None  # unknown everywhere → modal 404s
        else:
            logger.debug("asset lookup degraded for %s: %s", sym, exc)
    return {"name": name, "exchange": exchange, "tradable": tradable}


def _price_fields(sym: str) -> dict:
    out = {"price": None, "day_change_pct": None, "volume": None,
           "prev_close": None}
    try:
        from market_data import _get_alpaca_data_client
        dc = _get_alpaca_data_client()
        if dc is None:
            return out
        snap = (dc.get_snapshots([sym]) or {}).get(sym)
        if snap is None:
            return out
        daily = getattr(snap, "daily_bar", None)
        prev = getattr(snap, "previous_daily_bar", None)
        latest = getattr(snap, "latest_trade", None)
        price = None
        if latest is not None:
            price = float(getattr(latest, "price", 0) or 0) or None
        if price is None and daily is not None:
            price = float(getattr(daily, "close", 0) or 0) or None
        prev_close = None
        if prev is not None:
            prev_close = float(getattr(prev, "close", 0) or 0) or None
        out["price"] = price
        out["prev_close"] = prev_close
        if daily is not None:
            out["volume"] = float(getattr(daily, "volume", 0) or 0) or None
        if price and prev_close:
            out["day_change_pct"] = (price / prev_close - 1.0) * 100.0
    except Exception as exc:
        logger.debug("snapshot degraded for %s: %s", sym, exc)
    return out


def _range_fields(sym: str) -> dict:
    out = {"week52_high": None, "week52_low": None, "avg_volume": None}
    try:
        from market_data import get_bars
        df = get_bars(sym, limit=252)
        if df is None or len(df) == 0:
            return out
        out["week52_high"] = float(df["high"].max())
        out["week52_low"] = float(df["low"].min())
        out["avg_volume"] = float(df["volume"].tail(30).mean())
    except Exception as exc:
        logger.debug("bars degraded for %s: %s", sym, exc)
    return out


def get_symbol_info(symbol: str) -> Optional[dict]:
    """Assemble the modal payload. None => unknown/invalid symbol."""
    sym = (symbol or "").strip().upper()
    if not _valid_symbol(sym):
        return None
    asset = _asset_fields(sym)
    if asset is None:
        return None
    info = {"symbol": sym}
    info.update(asset)
    info.update(_price_fields(sym))
    info.update(_range_fields(sym))
    try:
        from sector_classifier import get_company_profile
        prof = get_company_profile(sym)
        info["market_cap"] = prof.get("market_cap")
        info["industry"] = prof.get("industry")
        info["sector"] = prof.get("gics_sector")
        if prof.get("name") and not info.get("name"):
            info["name"] = prof["name"]
    except Exception as exc:
        logger.debug("company profile degraded for %s: %s", sym, exc)
        info.setdefault("market_cap", None)
        info.setdefault("industry", None)
        info.setdefault("sector", None)
    return info
