"""Tier-4 alt-data — self-built from free public sources (2026-11-02,
docs/25 D12, docs/26 Experiment 3 "data expansion").

Operator rule: no paid data subscriptions — "we have been building our
own; it's better to control it long term." Every source here is a
public endpoint, fetched directly, parsed here, cached locally
(in-process TTL here, `alt_data_cache` at the aggregator), and shipped
with a deterministic checker plus a weightable signal so the
self-tuner measures its hit rate like every other signal.

Per-symbol:
  1. Reg SHO threshold list — Nasdaq + NYSE publish, every trading
     day, the securities with persistent fails-to-deliver (≥ 0.5% of
     shares outstanding for five settlement days). On the list = real
     delivery pressure: squeeze risk for a short, forced-buy-in flow
     for a long.
  2. SEC fails-to-deliver — half-month files of every CNS fail by
     symbol and settlement date. Level and trend of fails.
  3. Form 144 — an insider's notice of INTENT to sell restricted
     stock, filed before the sale (the Form 4 comes after). Count and
     recency per symbol from EDGAR.
  4. IPO lock-up proxy — a listing younger than ~18 months has a
     lock-up expiry about 180 days after its first bar; the window
     around it is a known supply event. Derived from our own bar
     history (first available bar), no external source.

Macro:
  5. VIX term structure — CBOE's daily index histories for VIX9D, VIX,
     VIX3M, VIX6M. Contango (VIX < VIX3M) is the normal state;
     backwardation is stress.
  (6. Market breadth lives in `market_breadth.py` — computed from the
     bars the screener already fetches.)

Fail-closed: a source that cannot be read returns {"has_data": False}
with a `reason`, never a fabricated neutral. Readers (prompt, rules,
features) treat has_data=False as "say nothing".
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# The SEC asks for a contact in the User-Agent; the other hosts accept any.
_USER_AGENT = "QuantOpsAI Research Bot (mack@mackenziesmith.com)"

_CACHE: Dict[str, Dict[str, Any]] = {}
TTL_DAILY = 6 * 60 * 60          # lists republished daily; 6h is plenty
TTL_HALF_MONTH = 24 * 60 * 60    # FTD files change twice a month


def _cached(key: str, ttl: int):
    e = _CACHE.get(key)
    if e and (time.time() - e["ts"]) < ttl:
        return e["data"]
    return None


def _cache_put(key: str, data: Any) -> None:
    _CACHE[key] = {"ts": time.time(), "data": data}


def _http_get_bytes(url: str, timeout: int = 30) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("tier4 GET failed %s: %s", url, exc)
        return None


def _http_get_text(url: str, timeout: int = 30) -> Optional[str]:
    b = _http_get_bytes(url, timeout)
    return b.decode("utf-8", "replace") if b is not None else None


def _business_days_back(n: int, today: Optional[date] = None) -> List[date]:
    d = today or datetime.utcnow().date()
    out: List[date] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


# ─────────────────────────────────────────────────────────────────────
# 1. Reg SHO threshold list (Nasdaq + NYSE)
# ─────────────────────────────────────────────────────────────────────

NASDAQ_THRESHOLD_URL = "https://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth{ymd}.txt"
NYSE_THRESHOLD_URL = ("https://www.nyse.com/api/regulatory/threshold-securities/download"
                      "?selectedDate={iso}")


def parse_threshold_file(text: str) -> Set[str]:
    """Both venues publish `Symbol|Security Name|Market Category|Reg SHO
    Threshold Flag|...`; a trailer line (`File Creation Time…`) and
    blank lines are skipped. Only rows flagged Y count."""
    out: Set[str] = set()
    for line in (text or "").splitlines():
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if parts[0].lower() == "symbol" or parts[0].lower().startswith("file creation"):
            continue
        if len(parts) >= 4 and parts[3].upper() == "Y" and parts[0]:
            out.add(parts[0].upper())
    return out


def load_threshold_lists(today: Optional[date] = None) -> Dict[str, Any]:
    """Union of the latest available Nasdaq + NYSE lists (tries the last
    five business days). Cached 6h."""
    cached = _cached("regsho_threshold_lists", TTL_DAILY)
    if cached is not None:
        return cached
    symbols: Dict[str, List[str]] = {}
    found_dates: Dict[str, str] = {}
    for venue, tmpl, fmt in (("nasdaq", NASDAQ_THRESHOLD_URL, "%Y%m%d"),
                             ("nyse", NYSE_THRESHOLD_URL, "%Y-%m-%d")):
        for d in _business_days_back(5, today):
            url = tmpl.format(ymd=d.strftime("%Y%m%d"), iso=d.isoformat())
            text = _http_get_text(url)
            if not text or "|" not in text:
                continue
            syms = parse_threshold_file(text)
            if not syms and "Symbol" not in text:
                continue
            for s in syms:
                symbols.setdefault(s, []).append(venue)
            found_dates[venue] = d.isoformat()
            break
    result = {"has_data": bool(found_dates), "symbols": symbols, "dates": found_dates,
              "count": len(symbols)}
    if result["has_data"]:
        _cache_put("regsho_threshold_lists", result)
    return result


def get_reg_sho_threshold(symbol: str) -> Dict[str, Any]:
    lists = load_threshold_lists()
    if not lists.get("has_data"):
        return {"has_data": False, "reason": "threshold lists unavailable"}
    venues = lists["symbols"].get((symbol or "").upper(), [])
    return {"has_data": True, "on_threshold_list": bool(venues), "venues": venues,
            "list_dates": lists["dates"], "list_size": lists["count"]}


# ─────────────────────────────────────────────────────────────────────
# 2. SEC fails-to-deliver
# ─────────────────────────────────────────────────────────────────────

FTD_URL_ROOTS = (
    "https://www.sec.gov/files/data/other/fails-deliver-data/",   # current files
    "https://www.sec.gov/files/data/fails-deliver-data/",          # older files
)
FTD_PERIODS_TO_TRY = 4       # half-months back from today
FTD_LARGE_FAILS_SHARES = 100_000


def _ftd_period_names(today: Optional[date] = None) -> List[str]:
    """Half-month file stems newest first: YYYYMM + 'a' (1st–15th) / 'b'."""
    d = today or datetime.utcnow().date()
    names: List[str] = []
    y, m, half = d.year, d.month, ("b" if d.day > 15 else "a")
    while len(names) < FTD_PERIODS_TO_TRY + 1:
        names.append(f"cnsfails{y:04d}{m:02d}{half}")
        if half == "b":
            half = "a"
        else:
            half = "b"
            m -= 1
            if m == 0:
                m, y = 12, y - 1
    return names


def parse_ftd_text(text: str) -> Dict[str, List[Tuple[str, int, Optional[float]]]]:
    """{SYMBOL: [(settlement_date_iso, fails, price), …]} from the
    pipe-delimited file. Header and trailer lines are skipped; a
    price of '.' means unavailable."""
    out: Dict[str, List[Tuple[str, int, Optional[float]]]] = {}
    for line in (text or "").splitlines():
        if not line or line.startswith("SETTLEMENT DATE") or line.startswith("Trailer"):
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        ymd, sym, qty, price = parts[0].strip(), parts[2].strip().upper(), parts[3].strip(), parts[5].strip()
        if len(ymd) != 8 or not sym:
            continue
        try:
            q = int(float(qty))
        except ValueError:
            continue
        try:
            p = float(price) if price not in ("", ".") else None
        except ValueError:
            p = None
        out.setdefault(sym, []).append((f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", q, p))
    return out


def load_ftd_period(stem: str) -> Optional[Dict[str, List[Tuple[str, int, Optional[float]]]]]:
    """One half-month file, parsed, cached 24h. None when the SEC has
    not published it yet (both URL roots 404)."""
    cached = _cached(f"ftd:{stem}", TTL_HALF_MONTH)
    if cached is not None:
        return cached
    for root in FTD_URL_ROOTS:
        raw = _http_get_bytes(root + stem + ".zip", timeout=60)
        if not raw:
            continue
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
            name = next((n for n in z.namelist() if n.lower().endswith(".txt")), None)
            if not name:
                continue
            parsed = parse_ftd_text(z.read(name).decode("latin-1"))
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            logger.warning("FTD file %s unreadable: %s", stem, exc)
            continue
        _cache_put(f"ftd:{stem}", parsed)
        return parsed
    _cache_put(f"ftd:{stem}:missing", True)
    return None


def _ftd_series(symbol: str, today: Optional[date] = None):
    series: List[Tuple[str, int, Optional[float]]] = []
    got = 0
    for stem in _ftd_period_names(today):
        if _cached(f"ftd:{stem}:missing", TTL_HALF_MONTH):
            continue
        data = load_ftd_period(stem)
        if data is None:
            continue
        got += 1
        series.extend(data.get((symbol or "").upper(), []))
        if got >= 2:          # two half-months ≈ 20 settlement days
            break
    series.sort()
    return series, got


def get_fails_to_deliver(symbol: str) -> Dict[str, Any]:
    series, files = _ftd_series(symbol)
    if files == 0:
        return {"has_data": False, "reason": "no fails-to-deliver file reachable"}
    if not series:
        return {"has_data": True, "reported": False, "latest_fails": 0, "max_fails": 0,
                "days_reported": 0, "trend": "none", "is_large": False}
    qtys = [q for _d, q, _p in series]
    latest_date, latest, _ = series[-1]
    half = max(1, len(qtys) // 2)
    first_avg = sum(qtys[:half]) / half
    last_avg = sum(qtys[-half:]) / half
    if last_avg > first_avg * 1.5 and latest >= FTD_LARGE_FAILS_SHARES / 10:
        trend = "rising"
    elif last_avg < first_avg * 0.67:
        trend = "falling"
    else:
        trend = "flat"
    return {"has_data": True, "reported": True, "latest_date": latest_date,
            "latest_fails": latest, "max_fails": max(qtys), "avg_fails": round(sum(qtys) / len(qtys)),
            "days_reported": len(qtys), "trend": trend,
            "is_large": max(qtys[-5:]) >= FTD_LARGE_FAILS_SHARES}


# ─────────────────────────────────────────────────────────────────────
# 3. Form 144 (insider intent to sell)
# ─────────────────────────────────────────────────────────────────────

def get_form144(symbol: str) -> Dict[str, Any]:
    try:
        from sec_filings import get_company_filings
    except ImportError as exc:
        return {"has_data": False, "reason": f"sec_filings unavailable: {exc}"}
    since = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        filings = get_company_filings(symbol, form_types=["144", "144/A"], since_date=since) or []
    except Exception as exc:
        return {"has_data": False, "reason": f"EDGAR lookup failed: {type(exc).__name__}"}
    dates: List[str] = []
    for f in filings:
        d = (f.get("filing_date") or f.get("filingDate") or f.get("date") or "")[:10]
        if d:
            dates.append(d)
    dates.sort()
    today = datetime.utcnow().date()
    cut30 = (today - timedelta(days=30)).isoformat()
    latest = dates[-1] if dates else None
    try:
        latest_days = (today - date.fromisoformat(latest)).days if latest else None
    except ValueError:
        latest_days = None
    return {"has_data": True, "count_90d": len(dates), "count_30d": sum(1 for d in dates if d >= cut30),
            "latest_date": latest, "latest_days_ago": latest_days,
            "is_cluster_30d": sum(1 for d in dates if d >= cut30) >= 2}


# ─────────────────────────────────────────────────────────────────────
# 4. IPO lock-up proxy (from our own bar history)
# ─────────────────────────────────────────────────────────────────────

LOCKUP_DAYS = 180
LOCKUP_WINDOW_DAYS = 10
RECENT_LISTING_MAX_DAYS = 540
_BARS_TO_PROBE = 600


def _lockup_from_first_bar(first_bar: date, n_bars: int, today: date) -> Dict[str, Any]:
    days_listed = (today - first_bar).days
    if n_bars >= _BARS_TO_PROBE or days_listed > RECENT_LISTING_MAX_DAYS:
        return {"has_data": True, "is_recent_listing": False, "days_listed": days_listed}
    expiry = first_bar + timedelta(days=LOCKUP_DAYS)
    to_exp = (expiry - today).days
    return {"has_data": True, "is_recent_listing": True, "first_bar_date": first_bar.isoformat(),
            "days_listed": days_listed, "lockup_expiry_est": expiry.isoformat(),
            "days_to_lockup_expiry": to_exp,
            "in_lockup_window": abs(to_exp) <= LOCKUP_WINDOW_DAYS}


def get_ipo_lockup(symbol: str) -> Dict[str, Any]:
    try:
        from market_data import get_bars
        df = get_bars(symbol, limit=_BARS_TO_PROBE)
    except Exception as exc:
        return {"has_data": False, "reason": f"bars unavailable: {type(exc).__name__}"}
    if df is None or getattr(df, "empty", True) or len(df) < 2:
        return {"has_data": False, "reason": "no bar history"}
    try:
        first = df.index[0]
        first_bar = first.date() if hasattr(first, "date") else date.fromisoformat(str(first)[:10])
    except (ValueError, AttributeError, TypeError) as exc:
        return {"has_data": False, "reason": f"bar index unreadable: {exc}"}
    return _lockup_from_first_bar(first_bar, len(df), datetime.utcnow().date())


# ─────────────────────────────────────────────────────────────────────
# 5. VIX term structure (CBOE daily index histories)
# ─────────────────────────────────────────────────────────────────────

CBOE_INDEX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{idx}_History.csv"
VIX_TERM_INDICES = ("VIX9D", "VIX", "VIX3M", "VIX6M")


def parse_cboe_last_close(text: str) -> Optional[Tuple[str, float]]:
    """(date_iso, close) of the last row of a CBOE `DATE,OPEN,HIGH,LOW,CLOSE` CSV."""
    last = None
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) < 5 or row[0].upper() == "DATE":
            continue
        try:
            close = float(row[4])
        except ValueError:
            continue
        last = (row[0], close)
    if not last:
        return None
    d = last[0]
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", d)
    iso = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else d[:10]
    return iso, last[1]


def get_vix_term_structure() -> Dict[str, Any]:
    cached = _cached("vix_term", TTL_DAILY)
    if cached is not None:
        return cached
    closes: Dict[str, float] = {}
    dates: Dict[str, str] = {}
    for idx in VIX_TERM_INDICES:
        text = _http_get_text(CBOE_INDEX_URL.format(idx=idx))
        parsed = parse_cboe_last_close(text) if text else None
        if parsed:
            dates[idx], closes[idx] = parsed
    if "VIX" not in closes or "VIX3M" not in closes:
        return {"has_data": False, "reason": "CBOE VIX/VIX3M history unavailable"}
    ratio_1m_3m = closes["VIX"] / closes["VIX3M"] if closes["VIX3M"] else None
    ratio_9d_1m = (closes["VIX9D"] / closes["VIX"]) if closes.get("VIX9D") and closes["VIX"] else None
    if ratio_1m_3m is None:
        structure = "unknown"
    elif ratio_1m_3m > 1.0:
        structure = "backwardation"
    elif ratio_1m_3m < 0.9:
        structure = "contango"
    else:
        structure = "flat"
    result = {"has_data": True, "as_of": dates.get("VIX"),
              "vix9d": closes.get("VIX9D"), "vix": closes["VIX"], "vix3m": closes["VIX3M"],
              "vix6m": closes.get("VIX6M"),
              "ratio_1m_3m": round(ratio_1m_3m, 3) if ratio_1m_3m else None,
              "ratio_9d_1m": round(ratio_9d_1m, 3) if ratio_9d_1m else None,
              "structure": structure,
              "stress": structure == "backwardation" or (ratio_9d_1m is not None and ratio_9d_1m > 1.1)}
    _cache_put("vix_term", result)
    return result
