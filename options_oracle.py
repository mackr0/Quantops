"""Options Chain Oracle — what the smartest money in the world expects.

Phase 5 of the Quant Fund Evolution roadmap (see ROADMAP.md).

Options markets are the forward-expectation layer on top of the spot market.
They reveal what institutional traders — who pay real money for optionality —
actually believe will happen. Most retail systems ignore this. Free from
yfinance; we extract:

  IV Skew                — put IV vs call IV asymmetry (fear vs greed)
  IV Term Structure      — IV across expirations (event-driven inversions)
  Implied Move           — market-implied one-standard-deviation move
  Put/Call Ratio         — positioning from open interest + volume
  Gamma Exposure (GEX)   — dealer hedging regime (expansion vs pinning)
  Max Pain               — strike at which option holders lose the most
  IV Rank                — current IV percentile across recent history

The combined signal tells the AI:
  - Is fear/greed extreme? (contrarian signal)
  - Is a catalyst imminent? (term structure inversion)
  - Are dealers long or short gamma? (volatility regime)
  - Where will price pin at expiration? (max pain anchor)
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: Dict[str, tuple] = {}
_CACHE_TTL = 1800   # 30 minutes — options data changes during the session


def _get_cached(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _set_cached(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# Chain fetch
# ---------------------------------------------------------------------------

def _fetch_chain(symbol: str) -> Optional[Dict[str, Any]]:
    """Download the options expirations + full chain for the nearest expiration.

    Returns dict with:
        current_price: float
        expirations: list[str]
        near_term: dict with 'calls' DataFrame, 'puts' DataFrame, 'expiration' str
    """
    cache_key = f"chain_{symbol}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        import yfinance as yf
        yf_sym = symbol.replace("/", "-") if "/" in symbol else symbol
        ticker = yf.Ticker(yf_sym)

        expirations = ticker.options
        if not expirations:
            _set_cached(cache_key, None)
            return None

        # Current price
        info = ticker.fast_info
        current_price = float(info.last_price) if hasattr(info, "last_price") else 0.0
        if current_price <= 0:
            hist = ticker.history(period="2d")
            if not hist.empty:
                current_price = float(hist["Close"].iloc[-1])

        # Pull the nearest 1-3 expirations for term structure work
        near_chains = []
        for exp in expirations[:3]:
            try:
                chain = ticker.option_chain(exp)
                near_chains.append({
                    "expiration": exp,
                    "calls": chain.calls,
                    "puts": chain.puts,
                })
            except Exception:
                continue

        if not near_chains:
            _set_cached(cache_key, None)
            return None

        result = {
            "current_price": current_price,
            "expirations": list(expirations),
            "near_term": near_chains[0],
            "chains": near_chains,
        }
        _set_cached(cache_key, result)
        return result
    except Exception as exc:
        logger.debug("Options chain fetch failed for %s: %s", symbol, exc)
        _set_cached(cache_key, None)
        return None


# ---------------------------------------------------------------------------
# Individual signals
# ---------------------------------------------------------------------------

def compute_iv_skew(chain_data: Dict[str, Any]) -> Dict[str, Any]:
    """Compare put IV to call IV at equidistant strikes.

    Skew > 1.0 = puts more expensive than calls = market expects downside.
    Skew > 1.5 = extreme fear. Skew < 1.0 = calls more expensive = greed.

    Returns:
        skew: float (put_iv / call_iv)
        put_iv: float
        call_iv: float
        signal: 'fear' | 'greed' | 'neutral'
    """
    try:
        import pandas as pd
        current = chain_data["current_price"]
        near = chain_data["near_term"]
        calls = near["calls"]
        puts = near["puts"]

        if calls is None or puts is None or calls.empty or puts.empty:
            return {"skew": 1.0, "put_iv": 0.0, "call_iv": 0.0, "signal": "neutral"}

        # Target ±5% from ATM for equidistant comparison
        target_call_strike = current * 1.05
        target_put_strike = current * 0.95

        calls_with_iv = calls[calls["impliedVolatility"] > 0].copy()
        puts_with_iv = puts[puts["impliedVolatility"] > 0].copy()

        if calls_with_iv.empty or puts_with_iv.empty:
            return {"skew": 1.0, "put_iv": 0.0, "call_iv": 0.0, "signal": "neutral"}

        # Find the strikes closest to targets
        call_idx = (calls_with_iv["strike"] - target_call_strike).abs().idxmin()
        put_idx = (puts_with_iv["strike"] - target_put_strike).abs().idxmin()

        call_iv = float(calls_with_iv.loc[call_idx, "impliedVolatility"])
        put_iv = float(puts_with_iv.loc[put_idx, "impliedVolatility"])

        skew = put_iv / call_iv if call_iv > 0 else 1.0

        if skew >= 1.3:
            signal = "fear"
        elif skew <= 0.85:
            signal = "greed"
        else:
            signal = "neutral"

        return {
            "skew": round(skew, 3),
            "put_iv": round(put_iv, 4),
            "call_iv": round(call_iv, 4),
            "signal": signal,
        }
    except Exception as exc:
        logger.debug("IV skew calculation failed: %s", exc)
        return {"skew": 1.0, "put_iv": 0.0, "call_iv": 0.0, "signal": "neutral"}


def compute_term_structure(chain_data: Dict[str, Any]) -> Dict[str, Any]:
    """Compute IV by expiration to detect event-driven inversions.

    Normal: IV rises with time (longer-dated more expensive).
    Inverted: near-term IV > longer-term IV => imminent event expected.

    Returns:
        near_iv: float (ATM IV of nearest expiration)
        far_iv: float (ATM IV of farthest expiration we pulled)
        slope: float (far_iv - near_iv; negative = inverted)
        inverted: bool
        signal: 'event_expected' | 'normal'
    """
    try:
        chains = chain_data.get("chains", [])
        if len(chains) < 2:
            return {"near_iv": 0.0, "far_iv": 0.0, "slope": 0.0,
                    "inverted": False, "signal": "normal"}

        current = chain_data["current_price"]
        atm_ivs = []
        for chain in chains:
            calls = chain["calls"]
            if calls is None or calls.empty:
                continue
            calls_iv = calls[calls["impliedVolatility"] > 0]
            if calls_iv.empty:
                continue
            # Use the strike closest to current price
            idx = (calls_iv["strike"] - current).abs().idxmin()
            atm_ivs.append(float(calls_iv.loc[idx, "impliedVolatility"]))

        if len(atm_ivs) < 2:
            return {"near_iv": 0.0, "far_iv": 0.0, "slope": 0.0,
                    "inverted": False, "signal": "normal"}

        near_iv = atm_ivs[0]
        far_iv = atm_ivs[-1]
        slope = far_iv - near_iv

        # Inverted means near-term IV is meaningfully higher than far-term
        inverted = bool(near_iv > far_iv * 1.1)
        signal = "event_expected" if inverted else "normal"

        return {
            "near_iv": round(near_iv, 4),
            "far_iv": round(far_iv, 4),
            "slope": round(slope, 4),
            "inverted": inverted,
            "signal": signal,
        }
    except Exception as exc:
        logger.debug("Term structure calculation failed: %s", exc)
        return {"near_iv": 0.0, "far_iv": 0.0, "slope": 0.0,
                "inverted": False, "signal": "normal"}


def compute_implied_move(chain_data: Dict[str, Any]) -> Dict[str, Any]:
    """Market-implied one-standard-deviation move by expiration.

    Implied move = (ATM call price + ATM put price) * ~0.85 / spot price

    A 5% implied move in a week is huge — market expects a catalyst.

    Returns:
        implied_move_pct: float (as % of current price)
        days_to_expiration: int
    """
    try:
        import pandas as pd
        from datetime import datetime
        current = chain_data["current_price"]
        near = chain_data["near_term"]
        exp_str = near["expiration"]
        calls = near["calls"]
        puts = near["puts"]

        if calls is None or puts is None or calls.empty or puts.empty or current <= 0:
            return {"implied_move_pct": 0.0, "days_to_expiration": 0}

        # Days to expiration
        try:
            exp_dt = datetime.strptime(exp_str, "%Y-%m-%d")
            dte = (exp_dt - datetime.now()).days
        except Exception:
            dte = 0

        # Find ATM call and put midpoints
        call_idx = (calls["strike"] - current).abs().idxmin()
        put_idx = (puts["strike"] - current).abs().idxmin()

        call_row = calls.loc[call_idx]
        put_row = puts.loc[put_idx]

        call_mid = (float(call_row.get("bid", 0) or 0) + float(call_row.get("ask", 0) or 0)) / 2
        put_mid = (float(put_row.get("bid", 0) or 0) + float(put_row.get("ask", 0) or 0)) / 2

        if call_mid <= 0 and put_mid <= 0:
            # Fallback to last price
            call_mid = float(call_row.get("lastPrice", 0) or 0)
            put_mid = float(put_row.get("lastPrice", 0) or 0)

        straddle = call_mid + put_mid
        implied_move = straddle * 0.85 / current * 100 if current > 0 else 0.0

        return {
            "implied_move_pct": round(implied_move, 2),
            "days_to_expiration": dte,
        }
    except Exception as exc:
        logger.debug("Implied move calculation failed: %s", exc)
        return {"implied_move_pct": 0.0, "days_to_expiration": 0}


def compute_put_call_ratios(chain_data: Dict[str, Any]) -> Dict[str, Any]:
    """Put/call ratios from volume and open interest across all near-term strikes.

    Volume PCR: what's being bought TODAY — intraday sentiment.
    OI PCR: cumulative positioning — longer-term sentiment.

    Returns: vol_pcr, oi_pcr, signal.
    """
    try:
        near = chain_data["near_term"]
        calls = near["calls"]
        puts = near["puts"]

        call_vol = int(calls["volume"].fillna(0).sum()) if calls is not None and not calls.empty else 0
        put_vol = int(puts["volume"].fillna(0).sum()) if puts is not None and not puts.empty else 0
        call_oi = int(calls["openInterest"].fillna(0).sum()) if calls is not None and not calls.empty else 0
        put_oi = int(puts["openInterest"].fillna(0).sum()) if puts is not None and not puts.empty else 0

        vol_pcr = put_vol / max(call_vol, 1)
        oi_pcr = put_oi / max(call_oi, 1)

        if vol_pcr >= 1.2:
            signal = "bearish_flow"
        elif vol_pcr <= 0.5:
            signal = "bullish_flow"
        else:
            signal = "neutral"

        return {
            "call_volume": call_vol,
            "put_volume": put_vol,
            "call_oi": call_oi,
            "put_oi": put_oi,
            "vol_pcr": round(vol_pcr, 3),
            "oi_pcr": round(oi_pcr, 3),
            "signal": signal,
        }
    except Exception as exc:
        logger.debug("PCR calculation failed: %s", exc)
        return {"call_volume": 0, "put_volume": 0, "call_oi": 0, "put_oi": 0,
                "vol_pcr": 0.0, "oi_pcr": 0.0, "signal": "neutral"}


def compute_gamma_exposure(chain_data: Dict[str, Any]) -> Dict[str, Any]:
    """Estimate dealer gamma exposure (GEX) from call vs put open interest.

    Assumption: dealers are typically short calls (selling to retail) and
    long puts (buying protection). Net gamma positioning tells us whether
    dealer hedging amplifies moves or dampens them.

    Positive GEX (dealers long gamma): price pinning near high-OI strikes,
    volatility contraction.

    Negative GEX (dealers short gamma): volatility expansion, moves
    self-reinforce.

    Returns: gex_sign ('positive'|'negative'|'neutral'), regime.
    """
    try:
        current = chain_data["current_price"]
        near = chain_data["near_term"]
        calls = near["calls"]
        puts = near["puts"]

        if calls is None or puts is None or calls.empty or puts.empty or current <= 0:
            return {"gex_sign": "neutral", "regime": "unknown",
                    "call_gamma_oi": 0, "put_gamma_oi": 0}

        # Approximation: use open interest weighted by distance-from-ATM.
        # Real GEX requires spot gamma per strike; we approximate with OI
        # concentration near the current price where gamma is largest.
        calls_near = calls[abs(calls["strike"] - current) < current * 0.1]
        puts_near = puts[abs(puts["strike"] - current) < current * 0.1]

        call_gamma_oi = int(calls_near["openInterest"].fillna(0).sum()) if not calls_near.empty else 0
        put_gamma_oi = int(puts_near["openInterest"].fillna(0).sum()) if not puts_near.empty else 0

        # Dealer positioning heuristic
        net = call_gamma_oi - put_gamma_oi
        total = call_gamma_oi + put_gamma_oi
        if total == 0:
            return {"gex_sign": "neutral", "regime": "unknown",
                    "call_gamma_oi": 0, "put_gamma_oi": 0}

        ratio = net / total
        if ratio > 0.25:
            gex_sign = "positive"
            regime = "pinning_volatility_contraction"
        elif ratio < -0.25:
            gex_sign = "negative"
            regime = "volatility_expansion"
        else:
            gex_sign = "neutral"
            regime = "balanced"

        return {
            "gex_sign": gex_sign,
            "regime": regime,
            "call_gamma_oi": call_gamma_oi,
            "put_gamma_oi": put_gamma_oi,
        }
    except Exception as exc:
        logger.debug("GEX calculation failed: %s", exc)
        return {"gex_sign": "neutral", "regime": "unknown",
                "call_gamma_oi": 0, "put_gamma_oi": 0}


def compute_max_pain(chain_data: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the max-pain strike — where option holders collectively lose the most.

    Near expiration, price tends to gravitate toward max pain due to dealer
    hedging. Large distance from max pain = potential pinning pressure.

    Returns: max_pain_strike, distance_pct (current - max_pain as % of current).
    """
    try:
        current = chain_data["current_price"]
        near = chain_data["near_term"]
        calls = near["calls"]
        puts = near["puts"]

        if calls is None or puts is None or calls.empty or puts.empty or current <= 0:
            return {"max_pain_strike": 0.0, "distance_pct": 0.0, "pinning": False}

        strikes = sorted(set(list(calls["strike"]) + list(puts["strike"])))
        if not strikes:
            return {"max_pain_strike": 0.0, "distance_pct": 0.0, "pinning": False}

        best_strike = strikes[0]
        min_pain = float("inf")

        for strike in strikes:
            # Total dollars option holders lose if price pins at `strike`
            # For ITM calls (strike < price): call holders gain (price - strike) * OI
            # For ITM puts (strike > price): put holders gain (strike - price) * OI
            call_pain = 0.0
            put_pain = 0.0

            for _, row in calls.iterrows():
                k = float(row["strike"])
                oi = float(row.get("openInterest", 0) or 0)
                if strike > k:
                    call_pain += (strike - k) * oi
            for _, row in puts.iterrows():
                k = float(row["strike"])
                oi = float(row.get("openInterest", 0) or 0)
                if strike < k:
                    put_pain += (k - strike) * oi

            total = call_pain + put_pain
            if total < min_pain:
                min_pain = total
                best_strike = strike

        distance_pct = (current - best_strike) / current * 100 if current > 0 else 0.0
        # Pinning pressure if within 2% of max pain and <14 DTE
        dte = compute_implied_move(chain_data).get("days_to_expiration", 99)
        pinning = bool(abs(distance_pct) < 2.0 and dte < 14)

        return {
            "max_pain_strike": round(float(best_strike), 2),
            "distance_pct": round(distance_pct, 2),
            "pinning": pinning,
        }
    except Exception as exc:
        logger.debug("Max pain calculation failed: %s", exc)
        return {"max_pain_strike": 0.0, "distance_pct": 0.0, "pinning": False}


def compute_iv_rank(symbol: str, current_iv: float) -> Dict[str, Any]:
    """Estimate where current IV sits vs recent history using realized vol.

    Pure yfinance doesn't expose historical IV directly, so we compare
    current ATM IV to 52-week realized volatility as a proxy. A precise
    IV rank would require an options data vendor; this is a good-enough
    approximation for qualitative signal.

    Returns: rank_pct (0-100), signal ('iv_high'|'iv_low'|'neutral')
    """
    try:
        import yfinance as yf
        yf_sym = symbol.replace("/", "-") if "/" in symbol else symbol
        ticker = yf.Ticker(yf_sym)
        hist = ticker.history(period="1y")
        if hist.empty or len(hist) < 30:
            return {"rank_pct": 50.0, "signal": "neutral", "realized_vol": 0.0}

        returns = hist["Close"].pct_change().dropna()
        if returns.std() <= 0:
            return {"rank_pct": 50.0, "signal": "neutral", "realized_vol": 0.0}
        # Annualized realized vol
        realized_vol = float(returns.std() * math.sqrt(252))

        if realized_vol <= 0:
            return {"rank_pct": 50.0, "signal": "neutral", "realized_vol": 0.0}

        # IV rank approximation: compare current IV to realized vol.
        # current_iv is an annualized decimal like 0.35 (35% IV)
        ratio = current_iv / realized_vol
        # ratio 1.0 = current IV matches realized vol exactly; map to 50th percentile
        # ratio 2.0+ = IV is much higher than realized (overpriced)
        # ratio 0.5- = IV is much lower (underpriced)
        rank_pct = max(0, min(100, (ratio - 0.5) * 100 / 1.5))

        if rank_pct >= 75:
            signal = "iv_high"   # options overpriced — sell premium
        elif rank_pct <= 25:
            signal = "iv_low"    # options underpriced — buy premium
        else:
            signal = "neutral"

        return {
            "rank_pct": round(rank_pct, 1),
            "signal": signal,
            "realized_vol": round(realized_vol, 4),
        }
    except Exception as exc:
        logger.debug("IV rank calculation failed for %s: %s", symbol, exc)
        return {"rank_pct": 50.0, "signal": "neutral", "realized_vol": 0.0}


# ---------------------------------------------------------------------------
# Top-level combined API
# ---------------------------------------------------------------------------

def get_options_oracle(symbol: str) -> Dict[str, Any]:
    """Return the full options-oracle signal bundle for a symbol.

    Safe to call in hot loops (cached 30 min). Returns `None` inside
    'has_options' if the symbol has no options chain (most crypto, some
    small caps, penny stocks).

    Returns dict with:
        has_options: bool
        current_price: float
        skew: {skew, put_iv, call_iv, signal}
        term_structure: {near_iv, far_iv, slope, inverted, signal}
        implied_move: {implied_move_pct, days_to_expiration}
        pcr: {vol_pcr, oi_pcr, signal, ...}
        gex: {gex_sign, regime, ...}
        max_pain: {max_pain_strike, distance_pct, pinning}
        iv_rank: {rank_pct, signal, realized_vol}
    """
    # Skip crypto — no options on Alpaca/yfinance for crypto
    if "/" in symbol:
        return {"has_options": False}

    chain = _fetch_chain(symbol)
    if not chain:
        return {"has_options": False}

    skew = compute_iv_skew(chain)
    term = compute_term_structure(chain)
    move = compute_implied_move(chain)
    pcr = compute_put_call_ratios(chain)
    gex = compute_gamma_exposure(chain)
    pain = compute_max_pain(chain)
    rank = compute_iv_rank(symbol, skew.get("call_iv", 0))

    return {
        "has_options": True,
        "current_price": chain["current_price"],
        "expiration": chain["near_term"]["expiration"],
        "skew": skew,
        "term_structure": term,
        "implied_move": move,
        "pcr": pcr,
        "gex": gex,
        "max_pain": pain,
        "iv_rank": rank,
    }


def summarize_for_ai(oracle: Dict[str, Any]) -> Optional[str]:
    """Produce a compact single-line summary suitable for the AI prompt.

    Returns None if the symbol has no options (nothing to say).
    """
    if not oracle.get("has_options"):
        return None

    parts = []
    skew = oracle.get("skew", {})
    term = oracle.get("term_structure", {})
    move = oracle.get("implied_move", {})
    pcr = oracle.get("pcr", {})
    gex = oracle.get("gex", {})
    pain = oracle.get("max_pain", {})
    rank = oracle.get("iv_rank", {})

    if skew.get("signal") != "neutral":
        parts.append(f"skew={skew['signal']}({skew.get('skew',1):.2f})")
    if term.get("inverted"):
        parts.append("IV TERM INVERTED")
    if move.get("implied_move_pct", 0) > 0:
        parts.append(f"implied_move={move['implied_move_pct']:.1f}%/{move.get('days_to_expiration',0)}d")
    if pcr.get("signal") != "neutral":
        parts.append(f"PCR={pcr.get('vol_pcr',0):.2f}({pcr['signal']})")
    if gex.get("regime") not in ("unknown", "balanced"):
        parts.append(f"gex={gex['regime']}")
    if pain.get("pinning"):
        parts.append(f"PIN@${pain['max_pain_strike']:.2f}")
    if rank.get("signal") != "neutral":
        parts.append(f"iv_rank={rank['signal']}")

    if not parts:
        return None
    return " | ".join(parts)
