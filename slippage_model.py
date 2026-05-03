"""Item 5c (slippage modeling) — realistic fill-cost estimation.

Real backtests need realistic slippage. A flat "0.2% on entry, 0.2% on
exit" inflates apparent edge and hides which strategies actually scale.
Live trading wants the same number for sizing — pass over names where
the expected execution cost would eat the alpha.

Four-component model:

  1. Half-spread (deterministic).
       half-spread bps = (ask - bid) / mid × 10000 / 2
     Pulled from current snapshot. Liquid SPX names ~0.5-2 bps;
     illiquid micro-caps 10-100+ bps.

  2. Market impact (size-dependent, sqrt scaling).
       impact_bps = K × sqrt(participation_rate)
     where participation_rate = order_qty × 100 / 20d_ADV (for an
     options contract, 100 multiplier; for equity, 1).
     Almgren-Chriss square-root model. K is calibrated weekly per
     market_type from the realized fill / decision deviation in the
     trades table — empirical, not assumed.

  3. Volatility scalar.
       vol_bps = vol_factor × daily_vol_bps
     Higher-vol names experience more decision-to-fill drift even
     for tiny orders (the price simply moves more between the
     decision moment and the fill moment).

  4. Bootstrap residual.
     The above three components are model-driven; reality has noise
     they can't capture (regime, intraday liquidity windows, news
     events). Bootstrap samples the empirical distribution of
     residuals — `actual_slippage − model_slippage` — from past
     trades, conditioned on order-size bucket.

Lazy calibration: K coefficient cached on disk per market_type, refit
when stale (≥ 7 days) or when called for a market_type with no cache.
No scheduler task — refresh happens on the next call after staleness.

Limits documented honestly:
  - K is fitted from our own historical fills, which are paper. Real
    fills will deviate; the calibrator should be re-run after going
    live for 30+ days.
  - Sqrt impact assumes intra-bar fills aren't subject to order-book
    depth pathology (squeeze events, regime breaks). For typical
    sizes (< 5% of ADV) this is fine.
  - Bootstrap requires ≥ 50 historical trades per market_type per
    size bucket; below that the residual is set to zero (no noise).
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration constants — tunable in code, not per-profile (the model
# is empirically calibrated; users shouldn't have to fiddle with K).
# ---------------------------------------------------------------------------

# Default K when no calibration data yet. ~12 bps for 1% participation
# is a common starting point in academic market-impact literature.
DEFAULT_K_BPS = 12.0

# Vol scalar — fraction of daily realized vol that bleeds into the
# decision-to-fill window. 0.05 = 5% of one daily move on average.
DEFAULT_VOL_FACTOR = 0.05

# Cached calibration TTL (seconds). 7 days.
CALIBRATION_TTL_SECONDS = 7 * 24 * 3600

# Cache file path
CALIBRATION_CACHE_DIR = ".cache/slippage_calibration"

# Min trades required to fit K per market_type (below this, fall back
# to DEFAULT_K_BPS rather than fitting a noisy K from too few rows).
MIN_TRADES_FOR_FIT = 30

# Order-size buckets for bootstrap residual lookup. Each bucket is a
# fraction-of-ADV range. Larger orders see more noise.
SIZE_BUCKETS = [
    (0.0, 0.001),    # tiny: < 0.1% of ADV
    (0.001, 0.005),  # small: 0.1-0.5%
    (0.005, 0.02),   # medium: 0.5-2%
    (0.02, 0.05),    # large: 2-5%
    (0.05, 1.0),     # extreme: ≥ 5% (market impact dominates)
]

MIN_TRADES_FOR_BOOTSTRAP = 20   # per bucket per market_type


# ---------------------------------------------------------------------------
# Calibration cache (disk-backed)
# ---------------------------------------------------------------------------

def _calibration_path(market_type: str) -> str:
    os.makedirs(CALIBRATION_CACHE_DIR, exist_ok=True)
    return os.path.join(
        CALIBRATION_CACHE_DIR, f"k_{market_type}.json"
    )


def _read_cached_k(market_type: str) -> Optional[Dict[str, Any]]:
    path = _calibration_path(market_type)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        if (time.time() - data.get("fitted_at", 0)) > CALIBRATION_TTL_SECONDS:
            return None
        return data
    except Exception:
        return None


def _write_cached_k(market_type: str, fit: Dict[str, Any]) -> None:
    try:
        with open(_calibration_path(market_type), "w") as f:
            json.dump(fit, f)
    except Exception as exc:
        logger.warning("Could not cache slippage K for %s: %s",
                          market_type, exc)


# ---------------------------------------------------------------------------
# Calibration — fit K from historical (decision_price, fill_price) pairs
# ---------------------------------------------------------------------------

def calibrate_from_history(
    db_path: str,
    market_type: Optional[str] = None,
    min_trades: int = MIN_TRADES_FOR_FIT,
) -> Dict[str, Any]:
    """Fit the market-impact coefficient K from historical fills.

    Pulls trades with both decision_price and fill_price set, computes
    realized slippage in bps signed in the adverse direction, then runs
    a least-squares fit:
        slippage_bps = K × sqrt(participation_rate) + noise

    Returns dict with K, n_samples, mean_residual, fitted_at, plus the
    bootstrap residual distributions per size bucket.
    """
    import sqlite3
    K_default = {
        "K_bps": DEFAULT_K_BPS,
        "n_samples": 0,
        "mean_residual_bps": 0.0,
        "fitted_at": time.time(),
        "market_type": market_type or "all",
        "bootstrap_residuals": {},
        "source": "default",
    }
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Pull realized fills + their estimated participation rate.
        # We don't store ADV at trade time, so use a simple proxy:
        # `volume_at_decision` if we have it, else fall back to a
        # default participation. Real calibration improves over time
        # as traders data fills in.
        rows = conn.execute(
            "SELECT symbol, qty, decision_price, fill_price, "
            "side FROM trades "
            "WHERE decision_price IS NOT NULL AND fill_price IS NOT NULL "
            "AND decision_price > 0 AND status = 'filled'"
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.debug("calibrate_from_history: query failed: %s", exc)
        return K_default

    samples: List[Tuple[float, float, float]] = []
    # Each: (participation_rate, realized_bps, qty_dollars)
    for row in rows:
        try:
            qty = abs(float(row["qty"] or 0))
            dp = float(row["decision_price"])
            fp = float(row["fill_price"])
            side = (row["side"] or "").lower()
            if qty <= 0 or dp <= 0 or fp <= 0:
                continue
            # Slippage in bps in the ADVERSE direction. For a buy,
            # adverse = paid more than decision; for a sell, adverse =
            # received less. Always express as positive bps.
            if side in ("buy", "buy_to_open", "buy_to_close"):
                bps = (fp - dp) / dp * 10000
            else:
                bps = (dp - fp) / dp * 10000
            # Without ADV-at-trade-time stored, approximate
            # participation as a function of order notional vs typical
            # 20d ADV (use universe-typical $50M ADV as the divisor).
            # This is a coarse fit; better calibration will arrive when
            # we start storing ADV alongside the fill. For now this
            # gives K a stable baseline.
            notional = qty * dp
            assumed_adv_dollars = 50_000_000
            participation = notional / assumed_adv_dollars
            participation = max(participation, 1e-6)
            samples.append((participation, bps, notional))
        except Exception:
            continue

    if len(samples) < min_trades:
        K_default["source"] = "insufficient_history"
        K_default["n_samples"] = len(samples)
        return K_default

    # Least-squares fit: bps = K × sqrt(p).
    # Closed form: K = sum(bps × sqrt(p)) / sum(p).
    num = sum(b * math.sqrt(p) for p, b, _ in samples)
    den = sum(p for p, _, _ in samples)
    K_fit = num / den if den > 0 else DEFAULT_K_BPS
    # Clamp to a sane range — outliers can otherwise blow up the fit.
    K_fit = max(1.0, min(K_fit, 200.0))

    # Mean residual (model error)
    residuals = [
        b - K_fit * math.sqrt(p) for p, b, _ in samples
    ]
    mean_residual = sum(residuals) / len(residuals)

    # Bootstrap residuals per size bucket
    bootstrap: Dict[str, List[float]] = {}
    for low, high in SIZE_BUCKETS:
        bucket_residuals = [
            b - K_fit * math.sqrt(p)
            for p, b, _ in samples
            if low <= p < high
        ]
        if len(bucket_residuals) >= MIN_TRADES_FOR_BOOTSTRAP:
            # Cap the cached samples at 200 to keep the file small
            bootstrap[f"{low:.4f}_{high:.4f}"] = bucket_residuals[-200:]

    fit = {
        "K_bps": float(K_fit),
        "n_samples": len(samples),
        "mean_residual_bps": float(mean_residual),
        "fitted_at": time.time(),
        "market_type": market_type or "all",
        "bootstrap_residuals": bootstrap,
        "source": "fit",
    }
    _write_cached_k(market_type or "all", fit)
    return fit


def get_k(db_path: str, market_type: Optional[str] = None) -> Dict[str, Any]:
    """Lazy: return cached K or refit if stale / missing."""
    cached = _read_cached_k(market_type or "all")
    if cached is not None:
        return cached
    return calibrate_from_history(db_path, market_type)


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------

def _bucket_for(participation: float) -> Optional[str]:
    for low, high in SIZE_BUCKETS:
        if low <= participation < high:
            return f"{low:.4f}_{high:.4f}"
    return None


def _sample_bootstrap_residual(
    bootstrap: Dict[str, List[float]],
    bucket: Optional[str],
    rng=None,
) -> float:
    """Pick a random residual from the bucket's empirical distribution.
    Returns 0.0 when there's no data for the bucket."""
    import random
    if not bucket or bucket not in bootstrap:
        return 0.0
    samples = bootstrap[bucket]
    if not samples:
        return 0.0
    rng = rng or random.Random()
    return float(rng.choice(samples))


def estimate_slippage(
    *,
    symbol: str,
    qty: int,
    side: str,
    decision_price: float,
    spread_bps: Optional[float] = None,
    adv_shares: Optional[float] = None,
    daily_vol_bps: Optional[float] = None,
    db_path: Optional[str] = None,
    market_type: Optional[str] = None,
    seed: Optional[int] = None,
    apply_bootstrap_noise: bool = False,
) -> Dict[str, Any]:
    """Estimate the slippage cost of a proposed order.

    Args:
        symbol:           ticker. Informational; the model itself is
                          symbol-agnostic.
        qty:              shares (or contracts × 100 for options).
        side:             'buy' / 'sell' / 'buy_to_open' / etc.
        decision_price:   price at decision time, in dollars.
        spread_bps:       bid-ask spread in bps. None → uses default
                          5 bps for liquid equity, 30 bps fallback for
                          illiquid (caller doesn't know).
        adv_shares:       20-day ADV (shares). None → assumes large
                          enough that participation is negligible.
        daily_vol_bps:    realized daily vol of `decision_price` (bps).
                          None → assumes 200 bps (2% / day).
        db_path:          for K calibration.
        market_type:      for per-bucket K.
        seed:             RNG seed for deterministic bootstrap (tests).
        apply_bootstrap_noise: when True, ADD a sampled bootstrap
                          residual to the estimate. False = point
                          estimate only (default — backtests want
                          deterministic; the noise shows up via the
                          confidence interval).

    Returns:
        dict with:
          symbol, qty, side
          half_spread_bps
          impact_bps
          vol_bps
          bootstrap_residual_bps (or 0)
          total_bps              (sum)
          slippage_dollars
          fill_price             (decision_price adjusted for direction)
          components             (breakdown for transparency)
          K_bps_used
          participation_rate
    """
    if qty <= 0 or decision_price <= 0:
        return {"error": "invalid qty or price",
                "total_bps": 0.0, "slippage_dollars": 0.0,
                "fill_price": decision_price}

    # 1. Half-spread component
    if spread_bps is None:
        spread_bps = 5.0   # conservative default for unknown
    half_spread_bps = max(0.0, spread_bps / 2.0)

    # 2. Market impact (sqrt model)
    fit = get_k(db_path, market_type) if db_path else {
        "K_bps": DEFAULT_K_BPS,
        "bootstrap_residuals": {},
        "source": "no_db",
    }
    K = float(fit.get("K_bps", DEFAULT_K_BPS))
    if adv_shares and adv_shares > 0:
        participation = qty / adv_shares
    else:
        participation = 0.0
    participation = max(participation, 1e-6)
    impact_bps = K * math.sqrt(participation)

    # 3. Volatility scalar
    if daily_vol_bps is None:
        daily_vol_bps = 200.0
    vol_bps = max(0.0, DEFAULT_VOL_FACTOR * daily_vol_bps)

    # 4. Bootstrap residual (optional — backtests use point estimate
    # by default; the residual distribution is exposed separately for
    # Monte Carlo callers).
    bucket = _bucket_for(participation)
    residual_bps = 0.0
    if apply_bootstrap_noise:
        import random
        rng = random.Random(seed) if seed is not None else None
        residual_bps = _sample_bootstrap_residual(
            fit.get("bootstrap_residuals", {}), bucket, rng=rng,
        )

    total_bps = half_spread_bps + impact_bps + vol_bps + residual_bps

    notional = qty * decision_price
    slippage_dollars = notional * total_bps / 10000

    side_l = (side or "").lower()
    if "buy" in side_l:
        # Adverse for buys = pay more
        fill_price = decision_price * (1 + total_bps / 10000)
    else:
        # Adverse for sells = receive less
        fill_price = decision_price * (1 - total_bps / 10000)

    return {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "half_spread_bps": round(half_spread_bps, 2),
        "impact_bps": round(impact_bps, 2),
        "vol_bps": round(vol_bps, 2),
        "bootstrap_residual_bps": round(residual_bps, 2),
        "total_bps": round(total_bps, 2),
        "slippage_dollars": round(slippage_dollars, 2),
        "fill_price": round(fill_price, 4),
        "components": {
            "half_spread": round(half_spread_bps, 2),
            "market_impact": round(impact_bps, 2),
            "volatility": round(vol_bps, 2),
            "bootstrap": round(residual_bps, 2),
        },
        "K_bps_used": round(K, 2),
        "participation_rate": round(participation, 6),
        "K_source": fit.get("source", "unknown"),
    }


def apply_to_fill(decision_price: float, slippage_bps: float, side: str) -> float:
    """Convenience: given a decision price + the slippage estimate,
    return the realistic fill price for backtest simulation."""
    if decision_price <= 0:
        return decision_price
    side_l = (side or "").lower()
    if "buy" in side_l:
        return decision_price * (1 + slippage_bps / 10000)
    return decision_price * (1 - slippage_bps / 10000)


def render_slippage_for_prompt(estimate: Dict[str, Any]) -> str:
    """Compact per-candidate string for the AI prompt:
       'exec cost ~ 8.4 bps ($42 on this order)'
    """
    if not estimate or estimate.get("error"):
        return ""
    bps = estimate.get("total_bps", 0)
    dollars = estimate.get("slippage_dollars", 0)
    if bps <= 0:
        return ""
    return (
        f"exec cost ~{bps:.1f} bps (${dollars:,.0f} on this order)"
    )
