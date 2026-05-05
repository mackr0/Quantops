"""Alpaca-native options chain fetcher.

Replaces yfinance for `options_oracle._fetch_chain`. The user pays for
Alpaca; defaulting to yfinance for options data was wasting that
subscription and shipping decisions on 15-min-delayed quotes.

Two Alpaca endpoints combined:

  /v1beta1/options/snapshots/<underlying>
    Per-contract snapshot: latestQuote (bid/ask/sizes/timestamp),
    dailyBar (o/h/l/c/v/vw), latestTrade, minuteBar, prevDailyBar.
    Real-time NBBO. Paginated; fetch all pages.

  /v2/options/contracts?underlying_symbols=<sym>&expiration_date_gte=<today>
    Contract metadata: expiration_date, type, strike_price,
    open_interest, close_price. Filterable by expiration window.

What Alpaca does NOT provide directly: implied volatility. We compute
it ourselves via Black-Scholes inversion (Newton's method) using the
mid price (= (bid + ask) / 2). The existing compute_greeks function
gives us the forward price → IV; iterate to invert.

Output shape: pandas DataFrames matching what options_oracle's
downstream code (compute_iv_skew / compute_term_structure / etc.)
expects from yfinance:
  columns: strike, lastPrice, bid, ask, volume, openInterest,
           impliedVolatility
"""
from __future__ import annotations

import logging
from datetime import date as _date, datetime as _dt, timezone as _tz
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Black-Scholes inversion constants
_IV_SOLVER_MAX_ITER = 50
_IV_SOLVER_TOL = 1e-4
_IV_SOLVER_MIN = 0.001    # 0.1% floor
_IV_SOLVER_MAX = 5.0      # 500% ceiling — vol can spike pre-earnings


def _implied_vol_from_price(market_price: float,
                                 spot: float,
                                 strike: float,
                                 days_to_expiry: int,
                                 is_call: bool,
                                 risk_free_rate: float = 0.045
                                 ) -> Optional[float]:
    """Newton-Raphson invert Black-Scholes for implied volatility.

    Args:
        market_price: observed option mid price (per share, NOT total).
        spot, strike, days_to_expiry, is_call, risk_free_rate: BS inputs.

    Returns IV as decimal (0.25 = 25%) or None if no convergence.

    Edge cases:
        - market_price ≤ intrinsic value: option mispriced or stale;
            return None rather than fitting a degenerate IV.
        - Bisection fallback when Newton fails (vega near zero).
    """
    from options_trader import compute_greeks

    if market_price <= 0 or spot <= 0 or strike <= 0 or days_to_expiry <= 0:
        return None

    # Reject below-intrinsic prices (data is wrong)
    intrinsic = (max(0.0, spot - strike) if is_call
                 else max(0.0, strike - spot))
    if market_price < intrinsic:
        return None

    # Initial guess — typical equity IV ~25%
    sigma = 0.25

    for _ in range(_IV_SOLVER_MAX_ITER):
        g = compute_greeks(
            spot=spot, strike=strike, days_to_expiry=days_to_expiry,
            iv=sigma, is_call=is_call, risk_free_rate=risk_free_rate,
        )
        if g is None:
            break
        diff = g["price"] - market_price
        if abs(diff) < _IV_SOLVER_TOL:
            return max(_IV_SOLVER_MIN, min(_IV_SOLVER_MAX, sigma))
        # Vega is per 1% IV move; convert to per-unit derivative.
        # compute_greeks returns vega for a 1-percentage-point move,
        # so divide by 100 to get d(price)/d(sigma).
        vega_per_unit = g["vega"] / 0.01
        if abs(vega_per_unit) < 1e-9:
            break
        sigma = sigma - diff / vega_per_unit
        if sigma <= _IV_SOLVER_MIN or sigma >= _IV_SOLVER_MAX:
            # Newton blew up — use bisection on a coarse grid as fallback
            return _bisect_iv(market_price, spot, strike, days_to_expiry,
                              is_call, risk_free_rate)

    # Newton didn't converge — bisect
    return _bisect_iv(market_price, spot, strike, days_to_expiry,
                      is_call, risk_free_rate)


def _bisect_iv(market_price, spot, strike, days, is_call,
                  risk_free_rate) -> Optional[float]:
    """Bisection fallback when Newton fails. Slower but always converges."""
    from options_trader import compute_greeks
    lo, hi = _IV_SOLVER_MIN, _IV_SOLVER_MAX
    for _ in range(80):
        mid = (lo + hi) / 2
        g = compute_greeks(spot=spot, strike=strike,
                            days_to_expiry=days, iv=mid,
                            is_call=is_call,
                            risk_free_rate=risk_free_rate)
        if g is None:
            return None
        if g["price"] < market_price:
            lo = mid
        else:
            hi = mid
        if hi - lo < _IV_SOLVER_TOL:
            return mid
    return mid


# ---------------------------------------------------------------------------
# Alpaca REST helpers
# ---------------------------------------------------------------------------

_ALPACA_DATA_BASE = "https://data.alpaca.markets"
_ALPACA_TRADING_BASE_PAPER = "https://paper-api.alpaca.markets"


def _alpaca_headers() -> Dict[str, str]:
    import config
    return {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
    }


def _fetch_contracts(underlying: str,
                          max_expirations: int = 6) -> List[Dict[str, Any]]:
    """Fetch contract metadata via the trading API.

    Returns list of {symbol, expiration_date, type, strike, open_interest,
    close_price}.

    Filters: expiration_date >= today; tradable=True; status=active.
    Pages until we have contracts spanning at least `max_expirations`
    distinct expiration dates (so term-structure analysis has data).
    """
    import requests
    from datetime import date as _d

    today = _d.today().isoformat()
    contracts: List[Dict[str, Any]] = []
    seen_expirations = set()
    next_page_token = None

    for _ in range(20):  # safety cap on pagination
        params = {
            "underlying_symbols": underlying.upper(),
            "expiration_date_gte": today,
            "status": "active",
            "limit": 1000,
        }
        if next_page_token:
            params["page_token"] = next_page_token
        try:
            r = requests.get(
                f"{_ALPACA_TRADING_BASE_PAPER}/v2/options/contracts",
                headers=_alpaca_headers(), params=params, timeout=10,
            )
        except Exception as exc:
            logger.debug("Alpaca contracts fetch failed for %s: %s",
                         underlying, exc)
            break
        if r.status_code != 200:
            logger.debug("Alpaca contracts %s: %s %s",
                         underlying, r.status_code, r.text[:200])
            break
        data = r.json()
        for c in data.get("option_contracts", []):
            try:
                contracts.append({
                    "symbol": c["symbol"],
                    "expiration_date": c["expiration_date"],
                    "type": c["type"],  # "call" or "put"
                    "strike": float(c["strike_price"]),
                    "open_interest": int(c.get("open_interest") or 0),
                    "close_price": float(c.get("close_price") or 0),
                })
                seen_expirations.add(c["expiration_date"])
            except (KeyError, ValueError):
                continue
        next_page_token = data.get("next_page_token")
        if not next_page_token or len(seen_expirations) >= max_expirations:
            break
    return contracts


def _fetch_snapshots(underlying: str) -> Dict[str, Dict[str, Any]]:
    """Fetch real-time snapshots for all options on `underlying`.

    Paginates through the full chain. Returns dict keyed by OCC symbol:
      {occ: {latestQuote: {ap, bp, ...}, dailyBar: {...}, ...}}
    """
    import requests
    snapshots: Dict[str, Dict[str, Any]] = {}
    next_page_token = None

    for _ in range(20):
        params = {"limit": 1000}
        if next_page_token:
            params["page_token"] = next_page_token
        try:
            r = requests.get(
                f"{_ALPACA_DATA_BASE}/v1beta1/options/snapshots/"
                f"{underlying.upper()}",
                headers=_alpaca_headers(), params=params, timeout=10,
            )
        except Exception as exc:
            logger.debug("Alpaca snapshots fetch failed for %s: %s",
                         underlying, exc)
            break
        if r.status_code != 200:
            logger.debug("Alpaca snapshots %s: %s %s",
                         underlying, r.status_code, r.text[:200])
            break
        data = r.json()
        for occ, snap in (data.get("snapshots") or {}).items():
            snapshots[occ] = snap
        next_page_token = data.get("next_page_token")
        if not next_page_token:
            break
    return snapshots


def _underlying_spot(underlying: str) -> Optional[float]:
    """Latest stock price via Alpaca. Falls back to last close if real-
    time quote unavailable."""
    import requests
    try:
        r = requests.get(
            f"{_ALPACA_DATA_BASE}/v2/stocks/{underlying.upper()}/"
            f"snapshot",
            headers=_alpaca_headers(), timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            quote = data.get("latestQuote") or {}
            ap = float(quote.get("ap") or 0)
            bp = float(quote.get("bp") or 0)
            if ap > 0 and bp > 0:
                return (ap + bp) / 2
            # Fall through to last trade
            trade = data.get("latestTrade") or {}
            tp = float(trade.get("p") or 0)
            if tp > 0:
                return tp
            # Fall through to daily bar close
            bar = data.get("dailyBar") or {}
            cp = float(bar.get("c") or 0)
            if cp > 0:
                return cp
    except Exception as exc:
        logger.debug("Alpaca stock snapshot failed for %s: %s",
                     underlying, exc)
    return None


# ---------------------------------------------------------------------------
# Combined chain → DataFrame
# ---------------------------------------------------------------------------

def _build_chain_dataframes(underlying: str, contracts: List[Dict[str, Any]],
                                snapshots: Dict[str, Dict[str, Any]],
                                spot: float,
                                today: Optional[_date] = None
                                ) -> Dict[str, Dict[str, Any]]:
    """Group contracts by expiration; for each, build calls/puts
    DataFrames with the columns yfinance returned.

    Output shape:
      {
        expiration_iso: {
          "expiration": str,
          "calls": pd.DataFrame,
          "puts":  pd.DataFrame,
        },
        ...
      }
    """
    import pandas as pd
    today = today or _date.today()
    by_expiration: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    for c in contracts:
        snap = snapshots.get(c["symbol"], {}) or {}
        quote = snap.get("latestQuote") or {}
        bar = snap.get("dailyBar") or {}
        trade = snap.get("latestTrade") or {}

        bid = float(quote.get("bp") or 0)
        ask = float(quote.get("ap") or 0)
        last_price = float(trade.get("p") or bar.get("c") or 0)
        volume = int(bar.get("v") or 0)
        open_interest = int(c.get("open_interest") or 0)

        # Mid for IV inversion. Fall back to last_price if quote missing.
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last_price
        try:
            expiry_d = _date.fromisoformat(c["expiration_date"])
        except (ValueError, KeyError):
            continue
        days = (expiry_d - today).days
        if days <= 0:
            continue

        is_call = c["type"] == "call"
        iv = _implied_vol_from_price(
            market_price=mid, spot=spot, strike=c["strike"],
            days_to_expiry=days, is_call=is_call,
        ) or 0.0

        row = {
            "strike": c["strike"],
            "lastPrice": last_price,
            "bid": bid,
            "ask": ask,
            "volume": volume,
            "openInterest": open_interest,
            "impliedVolatility": iv,
            "contractSymbol": c["symbol"],
        }
        bucket = by_expiration.setdefault(c["expiration_date"], {
            "expiration": c["expiration_date"],
            "calls": [], "puts": [],
        })
        if is_call:
            bucket["calls"].append(row)
        else:
            bucket["puts"].append(row)

    # Convert lists to DataFrames sorted by strike
    for exp_iso, bucket in by_expiration.items():
        bucket["calls"] = (pd.DataFrame(bucket["calls"])
                           .sort_values("strike").reset_index(drop=True)
                           if bucket["calls"]
                           else pd.DataFrame(columns=[
                               "strike", "lastPrice", "bid", "ask",
                               "volume", "openInterest",
                               "impliedVolatility", "contractSymbol",
                           ]))
        bucket["puts"] = (pd.DataFrame(bucket["puts"])
                          .sort_values("strike").reset_index(drop=True)
                          if bucket["puts"]
                          else pd.DataFrame(columns=[
                              "strike", "lastPrice", "bid", "ask",
                              "volume", "openInterest",
                              "impliedVolatility", "contractSymbol",
                          ]))
    return by_expiration


def list_available_contracts(symbol: str) -> List[Dict[str, Any]]:
    """Return raw list of currently-listed Alpaca option contracts for
    `symbol`. Each item: {symbol, expiration_date, type, strike,
    open_interest, close_price}.

    Used by `snap_to_listed_contract` to round AI-proposed strikes /
    expiries to actual contracts that exist. Returns [] on failure
    so callers can fall back gracefully (e.g. submit anyway and let
    Alpaca reject — same behavior as before this helper existed)."""
    try:
        return _fetch_contracts(symbol, max_expirations=20)
    except Exception as exc:
        logger.debug("list_available_contracts %s failed: %s", symbol, exc)
        return []


def snap_to_listed_contract(
    symbol: str,
    target_expiry: str,
    target_strike: float,
    option_type: str,           # 'C' or 'P' (or 'call'/'put')
    contracts: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Find the listed contract closest to (target_expiry, target_strike,
    option_type) for `symbol`.

    Returns {symbol, expiration_date, type, strike} of the closest
    listed contract, or None if no contracts at all.

    Snapping rules:
      1. Pick the listed expiration_date closest to target_expiry
         (smallest abs date diff). If the closest is more than 30
         days off, return None — the AI's pick is too far from any
         listed expiry, better to fail than silently substitute.
      2. Among contracts at that expiry of the requested type, pick
         the listed strike closest to target_strike (smallest abs $
         diff). If the closest is more than 5% off the target, return
         None — same rationale.
    """
    from datetime import date as _date

    contracts = contracts if contracts is not None else list_available_contracts(symbol)
    if not contracts:
        return None

    # Normalize the type
    t = (option_type or "").lower()
    if t in ("c", "call"):
        wanted_type = "call"
    elif t in ("p", "put"):
        wanted_type = "put"
    else:
        return None

    # Filter to type
    typed = [c for c in contracts if (c.get("type") or "").lower() == wanted_type]
    if not typed:
        return None

    # Group by expiration; find closest expiry to target.
    try:
        target_dt = _date.fromisoformat(target_expiry)
    except Exception:
        return None
    by_exp: Dict[str, List[Dict[str, Any]]] = {}
    for c in typed:
        exp = c.get("expiration_date")
        if not exp:
            continue
        by_exp.setdefault(exp, []).append(c)
    if not by_exp:
        return None

    def _exp_diff(exp_str: str) -> int:
        try:
            return abs((_date.fromisoformat(exp_str) - target_dt).days)
        except Exception:
            return 10**9

    closest_exp = min(by_exp.keys(), key=_exp_diff)
    if _exp_diff(closest_exp) > 30:
        return None

    # Within that expiry, find closest strike
    candidates = by_exp[closest_exp]
    def _strike_diff(c: Dict[str, Any]) -> float:
        try:
            return abs(float(c.get("strike", 0)) - float(target_strike))
        except Exception:
            return 10**9
    closest_contract = min(candidates, key=_strike_diff)
    closest_strike = float(closest_contract.get("strike", 0))
    if target_strike > 0 and abs(closest_strike - target_strike) / target_strike > 0.05:
        # >5% off target — refuse rather than silently substitute
        return None

    return {
        "symbol": closest_contract.get("symbol"),
        "expiration_date": closest_exp,
        "type": wanted_type,
        "strike": closest_strike,
    }


def fetch_chain_alpaca(symbol: str,
                          today: Optional[_date] = None) -> Optional[Dict[str, Any]]:
    """Drop-in replacement for the yfinance-based _fetch_chain.

    Returns the same shape options_oracle's downstream code expects:
      {
        "current_price": float,
        "expirations": [iso_date, ...],   # sorted, ascending
        "near_term": {"expiration": str, "calls": DataFrame, "puts": DataFrame},
        "chains": [{"expiration", "calls", "puts"}, ...]  # near 3 expirations
      }

    Returns None when the symbol has no options on Alpaca (most crypto,
    some illiquid names) or fetch fails.
    """
    # Crypto bypasses (Alpaca options are equities-only)
    if "/" in symbol:
        return None

    try:
        spot = _underlying_spot(symbol)
        if spot is None or spot <= 0:
            logger.debug("No spot for %s — skipping options chain", symbol)
            return None

        contracts = _fetch_contracts(symbol, max_expirations=6)
        if not contracts:
            return None

        snapshots = _fetch_snapshots(symbol)
        # Snapshots may be empty for very illiquid contracts — that's
        # OK; build_chain just leaves bid/ask/last as 0 for those rows.

        chains_by_exp = _build_chain_dataframes(
            symbol, contracts, snapshots, spot, today=today,
        )
        if not chains_by_exp:
            return None

        sorted_exps = sorted(chains_by_exp.keys())
        # Take the first 3 expirations for term-structure work
        near_chains = [chains_by_exp[exp] for exp in sorted_exps[:3]]

        return {
            "current_price": spot,
            "expirations": sorted_exps,
            "near_term": near_chains[0],
            "chains": near_chains,
        }
    except Exception as exc:
        logger.exception("Alpaca chain fetch failed for %s: %s",
                         symbol, exc)
        return None
