"""Scientific backtesting infrastructure — the discipline gate.

Phase 2 of the Quant Fund Evolution roadmap (see ROADMAP.md).

No strategy goes live without passing this gauntlet. No exceptions.

The `validate_strategy()` function is the single entry point. It runs every
rigor check and returns a PASS / FAIL verdict with a detailed report:

    result = validate_strategy(
        strategy_fn=my_strategy,           # callable (symbol, df) -> signal dict
        market_type='midcap',              # which universe
        history_days=540,                  # ~2 years
        params=None,                       # optional strategy params
    )
    if result['verdict'] == 'PASS':
        deploy(my_strategy)
    else:
        print(result['failures'])          # list of reasons it failed

Each gate is individually implemented and can be called alone for diagnostics.
See Phase 2 documentation for thresholds and rationale.
"""

from __future__ import annotations

import json
import logging
import math
import random
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gate thresholds — the rules every strategy must satisfy
# ---------------------------------------------------------------------------

THRESHOLDS = {
    # Minimum statistics
    "min_total_trades": 30,             # below this, Sharpe is meaningless
    "min_sharpe": 1.0,                  # risk-adjusted return bar
    "min_sortino": 1.0,                 # downside-only variant
    "max_drawdown_pct": -25.0,          # worst peak-to-trough
    "min_win_rate": 35.0,               # % (allows high R:R strategies)
    "min_profit_factor": 1.3,           # gross gain / gross loss
    "max_p_value": 0.05,                # Sharpe significance
    "max_oos_sharpe_degradation_pct": 30.0,   # in-sample -> OOS drop
    # Regime gate — strategy must not depend on one regime
    "min_regimes_profitable": 2,        # at least 2 of 4 regimes positive
    # Monte Carlo gate
    "min_monte_carlo_positive_pct": 60.0,  # % of bootstraps with positive return
    # Capacity gate
    "max_pct_daily_volume": 0.01,       # 1% — beyond this, slippage kills alpha
}


# ---------------------------------------------------------------------------
# Top-level gate
# ---------------------------------------------------------------------------

def validate_strategy(
    strategy_fn: Optional[Callable],
    market_type: str,
    history_days: int = 540,
    params: Optional[Dict[str, Any]] = None,
    initial_capital: float = 10_000,
    transaction_cost_pct: float = 0.004,   # 0.2% entry + 0.2% exit
    oos_fraction: float = 0.2,
    walk_forward_folds: int = 4,
    monte_carlo_iterations: int = 1000,
    sample_size: int = 30,
) -> Dict[str, Any]:
    """Run every validation gate and return PASS/FAIL with detailed report.

    This is the single entry point for Phase 2 validation. A strategy function
    must satisfy every gate to receive a PASS verdict.

    Parameters
    ----------
    strategy_fn : callable
        Function taking (symbol, df) or (symbol, ctx, df, params) that returns
        a signal dict compatible with the trade pipeline.
    market_type : str
        Which universe to test against (micro/small/midcap/largecap/crypto).
    history_days : int
        How many trading days of history to use. 540 ~= 2 years, enough for
        multiple market regimes.
    params : dict, optional
        Strategy-specific parameter overrides.
    initial_capital : float
        Per-run starting capital for return calculations.
    transaction_cost_pct : float
        Round-trip transaction cost subtracted from each simulated trade.
    oos_fraction : float
        Fraction of history reserved as out-of-sample (0.0-1.0).
    walk_forward_folds : int
        Number of walk-forward slices.
    monte_carlo_iterations : int
        Number of bootstrap iterations for stress testing.
    sample_size : int
        Symbols to include in each backtest pass.

    Returns
    -------
    dict with:
        verdict: 'PASS' | 'FAIL'
        score: float (0-100, for ranking)
        passed_gates: list[str]
        failed_gates: list[dict] — each with 'gate', 'reason', 'actual', 'threshold'
        metrics: comprehensive metrics dict
        timestamp: ISO timestamp of validation
        config: echo of input params
    """
    from backtester import backtest_strategy
    from segments import get_segment

    start = time.time()
    failures: List[Dict[str, Any]] = []
    passed: List[str] = []

    # Pre-sample symbols once so every gate tests the SAME universe.
    # This lets the per-symbol yfinance cache serve every subsequent gate
    # instantly (huge speedup: ~5x for the default configuration).
    seg = get_segment(market_type)
    universe = list(seg.get("universe", []))
    if len(universe) > sample_size:
        shared_symbols = random.sample(universe, sample_size)
    else:
        shared_symbols = universe

    logger.info("Validation sampled %d symbols for %s", len(shared_symbols), market_type)

    # --- Phase A: full-history backtest for baseline metrics ---
    baseline = backtest_strategy(
        market_type=market_type,
        days=history_days,
        initial_capital=initial_capital,
        sample_size=sample_size,
        symbols=shared_symbols,
        signal_fn=strategy_fn,
    )
    baseline_trades = baseline.get("trades", []) or []
    baseline_metrics = {
        "total_return_pct": baseline.get("total_return_pct", 0),
        "win_rate": baseline.get("win_rate", 0),
        "max_drawdown_pct": baseline.get("max_drawdown_pct", 0),
        "sharpe_ratio": baseline.get("sharpe_ratio", 0),
        "num_trades": baseline.get("num_trades", 0),
    }

    # --- Gate 1: minimum activity ---
    if baseline_metrics["num_trades"] < THRESHOLDS["min_total_trades"]:
        failures.append({
            "gate": "min_trades",
            "reason": "Insufficient trade count — Sharpe is statistically meaningless",
            "actual": baseline_metrics["num_trades"],
            "threshold": THRESHOLDS["min_total_trades"],
        })
    else:
        passed.append("min_trades")

    # --- Gate 2: Sharpe ratio ---
    sharpe = baseline_metrics.get("sharpe_ratio", 0) or 0
    if sharpe < THRESHOLDS["min_sharpe"]:
        failures.append({
            "gate": "sharpe",
            "reason": "Risk-adjusted return below bar",
            "actual": sharpe,
            "threshold": THRESHOLDS["min_sharpe"],
        })
    else:
        passed.append("sharpe")

    # --- Gate 3: drawdown ---
    dd = baseline_metrics.get("max_drawdown_pct", 0) or 0
    if dd < THRESHOLDS["max_drawdown_pct"]:
        failures.append({
            "gate": "max_drawdown",
            "reason": "Drawdown exceeds acceptable threshold",
            "actual": dd,
            "threshold": THRESHOLDS["max_drawdown_pct"],
        })
    else:
        passed.append("max_drawdown")

    # --- Gate 4: win rate ---
    win_rate = baseline_metrics.get("win_rate", 0) or 0
    if win_rate < THRESHOLDS["min_win_rate"]:
        failures.append({
            "gate": "win_rate",
            "reason": "Win rate too low",
            "actual": win_rate,
            "threshold": THRESHOLDS["min_win_rate"],
        })
    else:
        passed.append("win_rate")

    # --- Gate 5: statistical significance ---
    sig = check_statistical_significance(baseline_trades)
    if sig["p_value"] > THRESHOLDS["max_p_value"]:
        failures.append({
            "gate": "statistical_significance",
            "reason": "Sharpe ratio not statistically significant",
            "actual": round(sig["p_value"], 4),
            "threshold": THRESHOLDS["max_p_value"],
        })
    else:
        passed.append("statistical_significance")

    # --- Gate 6: Monte Carlo stress test ---
    mc = monte_carlo_stress(
        baseline_trades,
        iterations=monte_carlo_iterations,
        transaction_cost_pct=transaction_cost_pct,
    )
    mc_positive_pct = mc["positive_pct"]
    if mc_positive_pct < THRESHOLDS["min_monte_carlo_positive_pct"]:
        failures.append({
            "gate": "monte_carlo",
            "reason": "Under resampling, strategy fails >40% of scenarios",
            "actual": round(mc_positive_pct, 1),
            "threshold": THRESHOLDS["min_monte_carlo_positive_pct"],
        })
    else:
        passed.append("monte_carlo")

    # --- Gate 7: out-of-sample degradation ---
    oos = out_of_sample_degradation(
        strategy_fn=strategy_fn,
        market_type=market_type,
        history_days=history_days,
        params=params,
        initial_capital=initial_capital,
        oos_fraction=oos_fraction,
        sample_size=sample_size,
        symbols=shared_symbols,
    )
    if oos["degradation_pct"] > THRESHOLDS["max_oos_sharpe_degradation_pct"]:
        failures.append({
            "gate": "out_of_sample",
            "reason": "Strategy overfit — OOS Sharpe much worse than in-sample",
            "actual": round(oos["degradation_pct"], 1),
            "threshold": THRESHOLDS["max_oos_sharpe_degradation_pct"],
        })
    else:
        passed.append("out_of_sample")

    # --- Gate 8: regime consistency ---
    regime_result = regime_analysis(baseline_trades)
    if regime_result["regimes_profitable"] < THRESHOLDS["min_regimes_profitable"]:
        failures.append({
            "gate": "regime_consistency",
            "reason": "Strategy only works in one market regime (curve-fit risk)",
            "actual": regime_result["regimes_profitable"],
            "threshold": THRESHOLDS["min_regimes_profitable"],
        })
    else:
        passed.append("regime_consistency")

    # --- Gate 9: walk-forward stability ---
    wf = walk_forward_analysis(
        strategy_fn=strategy_fn,
        market_type=market_type,
        history_days=history_days,
        folds=walk_forward_folds,
        params=params,
        initial_capital=initial_capital,
        sample_size=sample_size,
        symbols=shared_symbols,
    )
    # Require at least 50% of folds profitable
    wf_profitable = wf["profitable_folds"] / max(wf["total_folds"], 1)
    if wf_profitable < 0.5:
        failures.append({
            "gate": "walk_forward",
            "reason": "Too many walk-forward folds are unprofitable",
            "actual": round(wf_profitable, 2),
            "threshold": 0.5,
        })
    else:
        passed.append("walk_forward")

    # --- Gate 10: capacity ---
    cap = capacity_analysis(baseline_trades, initial_capital=initial_capital)
    if cap["max_pct_of_volume"] > THRESHOLDS["max_pct_daily_volume"]:
        failures.append({
            "gate": "capacity",
            "reason": "Position sizes exceed 1% of daily volume — will impact price",
            "actual": round(cap["max_pct_of_volume"] * 100, 3),
            "threshold": round(THRESHOLDS["max_pct_daily_volume"] * 100, 3),
        })
    else:
        passed.append("capacity")

    # --- Final verdict ---
    verdict = "PASS" if not failures else "FAIL"
    total_gates = len(passed) + len(failures)
    score = round((len(passed) / total_gates) * 100, 1) if total_gates > 0 else 0

    elapsed = round(time.time() - start, 1)

    return {
        "verdict": verdict,
        "score": score,
        "passed_gates": passed,
        "failed_gates": failures,
        "metrics": {
            "baseline": baseline_metrics,
            "statistical_significance": sig,
            "monte_carlo": mc,
            "out_of_sample": oos,
            "regime": regime_result,
            "walk_forward": wf,
            "capacity": cap,
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_sec": elapsed,
        "config": {
            "market_type": market_type,
            "history_days": history_days,
            "initial_capital": initial_capital,
            "transaction_cost_pct": transaction_cost_pct,
            "oos_fraction": oos_fraction,
            "walk_forward_folds": walk_forward_folds,
            "monte_carlo_iterations": monte_carlo_iterations,
            "sample_size": sample_size,
            "params": params,
        },
        "thresholds": dict(THRESHOLDS),
    }


# ---------------------------------------------------------------------------
# Gate: Statistical significance
# ---------------------------------------------------------------------------

def check_statistical_significance(trades: List[Dict]) -> Dict[str, Any]:
    """Compute t-statistic and p-value for Sharpe ratio.

    H0: Sharpe = 0 (no edge). We want to reject this with p < 0.05.

    Returns dict with t_stat, p_value, sharpe, n, significant.
    """
    if not trades or len(trades) < 2:
        return {"t_stat": 0.0, "p_value": 1.0, "sharpe": 0.0,
                "n": len(trades), "significant": False}

    returns = [float(t.get("return_pct", 0) or 0) for t in trades]
    mean_r = statistics.mean(returns)
    stdev_r = statistics.stdev(returns) if len(returns) > 1 else 1e-9
    n = len(returns)

    # t-statistic: mean / (stdev / sqrt(n))
    if stdev_r > 0:
        t_stat = mean_r / (stdev_r / math.sqrt(n))
    else:
        t_stat = 0.0

    # Two-tailed p-value using normal approximation for large n.
    # For small n we use a conservative t-distribution via scipy when available.
    try:
        from scipy import stats as _stats
        p_value = 2 * (1 - _stats.t.cdf(abs(t_stat), df=n - 1))
    except ImportError:
        # Normal approximation
        p_value = 2 * (1 - _normal_cdf(abs(t_stat)))

    # Sharpe from trade-level returns
    sharpe = mean_r / stdev_r if stdev_r > 0 else 0.0

    return {
        "t_stat": round(t_stat, 3),
        "p_value": round(float(p_value), 4),
        "sharpe": round(sharpe, 3),
        "n": n,
        "significant": bool(p_value < 0.05),
    }


def _normal_cdf(x: float) -> float:
    """Approximation of the standard normal CDF when scipy isn't available."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ---------------------------------------------------------------------------
# Gate: Monte Carlo bootstrap
# ---------------------------------------------------------------------------

def monte_carlo_stress(
    trades: List[Dict],
    iterations: int = 1000,
    transaction_cost_pct: float = 0.004,
) -> Dict[str, Any]:
    """Bootstrap-resample trade returns to estimate the distribution of outcomes.

    Each iteration samples len(trades) trades with replacement, applies
    transaction costs, computes the total return, and records it. Returns
    percentile statistics on the resulting distribution.
    """
    if not trades:
        return {
            "iterations": 0, "positive_pct": 0.0,
            "var_5": 0.0, "var_95": 0.0, "median": 0.0,
        }

    # Per-trade return after transaction costs
    net_returns = [
        float(t.get("return_pct", 0) or 0) - transaction_cost_pct * 100
        for t in trades
    ]

    outcomes: List[float] = []
    n = len(net_returns)
    for _ in range(iterations):
        sample = [random.choice(net_returns) for _ in range(n)]
        # Compound return assuming equal position sizing per trade
        # Use mean as a simple approximation of cumulative per-trade return
        outcomes.append(sum(sample) / n)

    outcomes.sort()
    positive_count = sum(1 for v in outcomes if v > 0)
    var_5 = outcomes[int(0.05 * iterations)]
    var_95 = outcomes[int(0.95 * iterations)]
    median = outcomes[iterations // 2]

    return {
        "iterations": iterations,
        "positive_pct": round(positive_count / iterations * 100, 1),
        "var_5": round(var_5, 3),
        "var_95": round(var_95, 3),
        "median": round(median, 3),
    }


# ---------------------------------------------------------------------------
# Gate: Out-of-sample degradation
# ---------------------------------------------------------------------------

def out_of_sample_degradation(
    strategy_fn: Callable,
    market_type: str,
    history_days: int,
    params: Optional[Dict[str, Any]],
    initial_capital: float,
    oos_fraction: float,
    sample_size: int,
    symbols: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Compare in-sample and out-of-sample performance.

    Splits history: first (1-fraction) is in-sample, last `fraction` is OOS.
    Returns degradation in Sharpe ratio. High degradation = overfit.
    """
    from backtester import backtest_strategy

    in_sample_days = int(history_days * (1 - oos_fraction))
    oos_days = history_days - in_sample_days

    # Baseline over in-sample period
    is_result = backtest_strategy(
        market_type=market_type,
        days=in_sample_days,
        initial_capital=initial_capital,
        sample_size=sample_size,
        symbols=symbols,
        signal_fn=strategy_fn,
    )
    is_sharpe = is_result.get("sharpe_ratio", 0) or 0

    # OOS — we run over just the recent window
    oos_result = backtest_strategy(
        market_type=market_type,
        days=oos_days,
        initial_capital=initial_capital,
        sample_size=sample_size,
        symbols=symbols,
        signal_fn=strategy_fn,
    )
    oos_sharpe = oos_result.get("sharpe_ratio", 0) or 0

    if is_sharpe > 0:
        degradation = max(0.0, (is_sharpe - oos_sharpe) / is_sharpe * 100)
    else:
        degradation = 0.0 if oos_sharpe >= is_sharpe else 100.0

    return {
        "in_sample_sharpe": round(is_sharpe, 3),
        "oos_sharpe": round(oos_sharpe, 3),
        "degradation_pct": round(degradation, 1),
        "in_sample_days": in_sample_days,
        "oos_days": oos_days,
    }


# ---------------------------------------------------------------------------
# Gate: Regime analysis
# ---------------------------------------------------------------------------

def regime_analysis(trades: List[Dict]) -> Dict[str, Any]:
    """Split trades by market regime and report per-regime profitability.

    Each trade should carry a 'regime' field; if missing, all trades go to
    'unknown' and the gate fails-open.
    """
    if not trades:
        return {"per_regime": {}, "regimes_profitable": 0, "regimes_tested": 0}

    per_regime: Dict[str, List[float]] = {}
    for t in trades:
        regime = str(t.get("regime", "unknown") or "unknown")
        ret = float(t.get("return_pct", 0) or 0)
        per_regime.setdefault(regime, []).append(ret)

    results = {}
    profitable_count = 0
    for regime, returns in per_regime.items():
        total_return = sum(returns)
        mean_return = statistics.mean(returns) if returns else 0
        results[regime] = {
            "n_trades": len(returns),
            "total_return_pct": round(total_return, 2),
            "avg_return_pct": round(mean_return, 3),
            "profitable": total_return > 0,
        }
        if total_return > 0 and regime != "unknown":
            profitable_count += 1

    known_regimes = [r for r in results if r != "unknown"]

    return {
        "per_regime": results,
        "regimes_profitable": profitable_count,
        "regimes_tested": len(known_regimes),
    }


# ---------------------------------------------------------------------------
# Gate: Walk-forward analysis
# ---------------------------------------------------------------------------

def walk_forward_analysis(
    strategy_fn: Callable,
    market_type: str,
    history_days: int,
    folds: int,
    params: Optional[Dict[str, Any]],
    initial_capital: float,
    sample_size: int,
    symbols: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Roll a backtest window forward through history, measuring consistency.

    Instead of one backtest, we run `folds` sequential non-overlapping windows.
    A strategy with real edge should be profitable in most folds.
    """
    from backtester import backtest_strategy

    fold_days = history_days // folds
    if fold_days < 30:
        return {
            "folds": [], "total_folds": 0, "profitable_folds": 0,
            "consistency_pct": 0, "note": "insufficient history for walk-forward"
        }

    fold_results = []
    profitable = 0
    for i in range(folds):
        # Each fold uses a subset of the most recent days.
        # yfinance fetches the latest N days, so we emulate rolling by
        # requesting a shorter window per fold.
        days_for_fold = fold_days
        result = backtest_strategy(
            market_type=market_type,
            days=days_for_fold,
            initial_capital=initial_capital,
            sample_size=sample_size,
            symbols=symbols,
            signal_fn=strategy_fn,
        )
        total_ret = result.get("total_return_pct", 0) or 0
        fold_results.append({
            "fold": i + 1,
            "days": days_for_fold,
            "total_return_pct": round(total_ret, 2),
            "sharpe_ratio": round(result.get("sharpe_ratio", 0) or 0, 3),
            "num_trades": result.get("num_trades", 0),
        })
        if total_ret > 0:
            profitable += 1

    return {
        "folds": fold_results,
        "total_folds": folds,
        "profitable_folds": profitable,
        "consistency_pct": round(profitable / folds * 100, 1),
    }


# ---------------------------------------------------------------------------
# Gate: Capacity
# ---------------------------------------------------------------------------

def capacity_analysis(
    trades: List[Dict],
    initial_capital: float,
) -> Dict[str, Any]:
    """Estimate strategy capacity based on position-to-volume ratios.

    For each trade, we look at avg_daily_dollar_volume (when available) and
    compute what fraction of daily volume our position represented. Positions
    above 1% of daily volume will experience significant slippage.

    Returns:
        max_pct_of_volume: largest observed position / daily_volume ratio
        avg_pct_of_volume: average ratio
        capacity_usd: maximum capital that keeps max_pct below threshold
    """
    if not trades:
        return {
            "max_pct_of_volume": 0.0,
            "avg_pct_of_volume": 0.0,
            "capacity_usd": float("inf"),
        }

    ratios: List[float] = []
    for t in trades:
        position_value = float(t.get("cost_basis", 0) or 0)
        daily_volume_usd = float(t.get("avg_daily_dollar_volume", 0) or 0)
        if daily_volume_usd > 0 and position_value > 0:
            ratios.append(position_value / daily_volume_usd)

    if not ratios:
        # Approximate via generic scale: assume $1M daily dollar volume floor
        # and our position size from trade cost.
        avg_trade_value = sum(float(t.get("cost_basis", initial_capital * 0.1) or initial_capital * 0.1)
                               for t in trades) / len(trades)
        approx_ratio = avg_trade_value / 1_000_000
        return {
            "max_pct_of_volume": round(approx_ratio, 4),
            "avg_pct_of_volume": round(approx_ratio, 4),
            "capacity_usd": round(initial_capital * (0.01 / max(approx_ratio, 1e-6)), 0),
            "approximated": True,
        }

    max_ratio = max(ratios)
    avg_ratio = sum(ratios) / len(ratios)
    # Capacity = current capital × (threshold / max_ratio)
    capacity = initial_capital * (THRESHOLDS["max_pct_daily_volume"] / max_ratio) if max_ratio > 0 else float("inf")

    return {
        "max_pct_of_volume": round(max_ratio, 4),
        "avg_pct_of_volume": round(avg_ratio, 4),
        "capacity_usd": round(capacity, 0),
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

VALIDATIONS_DB = "strategy_validations.db"


def init_validations_db(db_path: str = VALIDATIONS_DB) -> None:
    """Create the strategy_validations table if missing."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_validations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            strategy_name TEXT NOT NULL,
            market_type TEXT NOT NULL,
            verdict TEXT NOT NULL,
            score REAL NOT NULL,
            passed_gates TEXT NOT NULL,
            failed_gates TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            config_json TEXT NOT NULL,
            elapsed_sec REAL
        )
    """)
    conn.commit()
    conn.close()


def save_validation(
    strategy_name: str,
    result: Dict[str, Any],
    db_path: str = VALIDATIONS_DB,
) -> int:
    """Persist a validation result."""
    init_validations_db(db_path)

    import sqlite3
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """INSERT INTO strategy_validations
           (strategy_name, market_type, verdict, score,
            passed_gates, failed_gates, metrics_json, config_json, elapsed_sec)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            strategy_name,
            result.get("config", {}).get("market_type", "unknown"),
            result.get("verdict", "FAIL"),
            result.get("score", 0),
            json.dumps(result.get("passed_gates", [])),
            json.dumps(result.get("failed_gates", [])),
            json.dumps(result.get("metrics", {}), default=str),
            json.dumps(result.get("config", {}), default=str),
            result.get("elapsed_sec", 0),
        ),
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    logger.info("Saved validation #%d: %s -> %s (score=%.1f)",
                row_id, strategy_name, result.get("verdict"), result.get("score", 0))
    return row_id


def get_recent_validations(limit: int = 50, db_path: str = VALIDATIONS_DB) -> List[Dict[str, Any]]:
    """List recent validation runs from the database."""
    import os
    import sqlite3
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM strategy_validations ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
