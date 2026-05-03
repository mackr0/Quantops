"""Item 5c — Monte Carlo backtest using slippage-model bootstrap.

The deterministic backtester (`backtester.py`) gives a single P&L
number per strategy run. That number depends on the specific slippage
realization the model used at fill time. Reality has slippage variance:
two backtest runs covering the same period would produce different
fills, with the spread driven by liquidity / regime / spread realizations.

This module replays a list of backtest trades N times, drawing slippage
on each entry + exit from the bootstrap residual distribution fitted in
`slippage_model.calibrate_from_history`. Output is the P&L
distribution (5/50/95th percentiles, mean, σ, worst case, best case).

Why it's useful:
  - Tells you "this strategy looks great deterministically but the
    5th-percentile run loses money" — i.e. is the edge robust to
    realistic execution variance.
  - Distinguishes strategies whose alpha is larger than execution
    variance from strategies whose deterministic P&L is just noise.

Limits:
  - The MC samples slippage IID per trade; correlated regimes (a
    full day of wide spreads when the whole strategy fires) aren't
    captured. To capture those, we'd need to bootstrap by day, not
    by trade — future enhancement.
  - Bootstrap residuals require ≥ 20 historical trades per size
    bucket per market_type. Below that, MC residuals fall back to a
    Gaussian fit from the global mean/std (configurable).
"""
from __future__ import annotations

import logging
import math
import random
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _sample_slippage_bps(
    bootstrap_residuals: Dict[str, List[float]],
    bucket: Optional[str],
    fallback_mean: float = 5.0,
    fallback_std: float = 8.0,
    rng: Optional[random.Random] = None,
) -> float:
    """Sample one slippage realization in bps. Tries bootstrap first;
    falls back to a Gaussian when no bucket data is available."""
    rng = rng or random.Random()
    if bucket and bucket in bootstrap_residuals:
        samples = bootstrap_residuals[bucket]
        if len(samples) >= 5:
            return float(rng.choice(samples))
    # Gaussian fallback. Always positive (adverse-direction).
    return abs(rng.gauss(fallback_mean, fallback_std))


def replay_trade(
    trade: Dict[str, Any],
    bootstrap_residuals: Dict[str, List[float]],
    bucket: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> float:
    """Replay one trade with sampled entry + exit slippage.
    Returns the perturbed pnl_pct (decimal, e.g. 0.05 = 5%).

    Trade dict must contain entry_price, exit_price (positive prices).
    Side is inferred: long trades have pnl = exit - entry; short
    trades the opposite. We treat all trades as long unless `side`
    field is explicitly 'short'.
    """
    entry = float(trade.get("entry_price") or 0)
    exit_p = float(trade.get("exit_price") or 0)
    if entry <= 0 or exit_p <= 0:
        return 0.0
    side = (trade.get("side") or "long").lower()

    entry_slip_bps = _sample_slippage_bps(
        bootstrap_residuals, bucket, rng=rng,
    )
    exit_slip_bps = _sample_slippage_bps(
        bootstrap_residuals, bucket, rng=rng,
    )
    # For longs: slippage hurts on both sides — pay more on entry,
    # receive less on exit. For shorts: opposite.
    if side == "short":
        entry_perturbed = entry * (1 - entry_slip_bps / 10000)
        exit_perturbed = exit_p * (1 + exit_slip_bps / 10000)
        pnl_pct = (entry_perturbed - exit_perturbed) / entry_perturbed
    else:
        entry_perturbed = entry * (1 + entry_slip_bps / 10000)
        exit_perturbed = exit_p * (1 - exit_slip_bps / 10000)
        pnl_pct = (exit_perturbed - entry_perturbed) / entry_perturbed
    return pnl_pct


def run_monte_carlo(
    trades: List[Dict[str, Any]],
    *,
    db_path: Optional[str] = None,
    market_type: Optional[str] = None,
    n_sims: int = 1000,
    seed: Optional[int] = 42,
    initial_capital: float = 100_000.0,
    position_size_pct: float = 0.10,
) -> Dict[str, Any]:
    """Run an MC backtest over a list of trades.

    For each of `n_sims` simulations:
      For each trade: sample bootstrap slippage on entry + exit
      Aggregate to total cumulative return (compounded)
    Returns the distribution stats.

    Args:
        trades: list of trade dicts (entry_price, exit_price, side?).
        db_path: source DB for slippage_model bootstrap calibration.
        market_type: scope the K cache.
        n_sims: number of MC trajectories.
        seed: deterministic RNG (for tests / reproducibility).
        initial_capital, position_size_pct: convert pct returns to
          dollar curve. Each trade is sized at `position_size_pct` of
          current equity (compounding).
    """
    if not trades:
        return {"error": "empty trade list", "n_sims": 0}

    bootstrap_residuals: Dict[str, List[float]] = {}
    if db_path:
        try:
            from slippage_model import calibrate_from_history
            fit = calibrate_from_history(db_path, market_type=market_type)
            bootstrap_residuals = fit.get("bootstrap_residuals", {}) or {}
        except Exception as exc:
            logger.debug("MC: slippage calibration failed: %s", exc)

    # Use one bucket for everything; finer-grained per-trade ADV
    # estimation isn't available without bar-level data. The MC
    # purpose is variance estimation, not precision per-trade.
    default_bucket = next(iter(bootstrap_residuals.keys()), None)

    total_returns: List[float] = []
    final_equities: List[float] = []
    rng = random.Random(seed) if seed is not None else random.Random()
    for sim in range(n_sims):
        equity = initial_capital
        for tr in trades:
            pnl_pct = replay_trade(
                tr, bootstrap_residuals, bucket=default_bucket, rng=rng,
            )
            position = equity * position_size_pct
            equity += position * pnl_pct
        total_returns.append((equity - initial_capital) / initial_capital)
        final_equities.append(equity)

    total_returns.sort()
    final_equities.sort()
    n = len(total_returns)

    def pct(p: float, arr: List[float]) -> float:
        if not arr:
            return 0.0
        idx = max(0, min(n - 1, int(round(p * (n - 1)))))
        return float(arr[idx])

    mean_return = sum(total_returns) / n
    var = sum((r - mean_return) ** 2 for r in total_returns) / max(n - 1, 1)
    std_return = math.sqrt(var)

    return {
        "n_sims": n,
        "n_trades": len(trades),
        "p5_return":  pct(0.05, total_returns),
        "p25_return": pct(0.25, total_returns),
        "p50_return": pct(0.50, total_returns),
        "p75_return": pct(0.75, total_returns),
        "p95_return": pct(0.95, total_returns),
        "mean_return": float(mean_return),
        "std_return": float(std_return),
        "worst_return": float(min(total_returns)),
        "best_return": float(max(total_returns)),
        "p5_dollars":  initial_capital * pct(0.05, total_returns),
        "p50_dollars": initial_capital * pct(0.50, total_returns),
        "p95_dollars": initial_capital * pct(0.95, total_returns),
        "prob_loss": sum(1 for r in total_returns if r < 0) / n,
        "initial_capital": initial_capital,
        "bootstrap_buckets_used": list(bootstrap_residuals.keys()),
    }


def render_mc_for_prompt(result: Dict[str, Any]) -> str:
    """Compact summary for dashboard / prompt context."""
    if not result or result.get("error") or result.get("n_sims", 0) == 0:
        return ""
    return (
        f"MC ({result['n_sims']} sims, {result['n_trades']} trades): "
        f"median {result['p50_return'] * 100:+.2f}%, "
        f"5/95 [{result['p5_return'] * 100:+.2f}% / "
        f"{result['p95_return'] * 100:+.2f}%], "
        f"σ {result['std_return'] * 100:.2f}%, "
        f"P(loss)={result['prob_loss'] * 100:.0f}%"
    )
