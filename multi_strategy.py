"""Multi-strategy aggregation and capital allocation.

Phase 6 of the Quant Fund Evolution roadmap (see ROADMAP.md).

The trade pipeline used to call one market-specific strategy engine.
Now it calls the registry, which returns every active (non-deprecated)
strategy applicable to this market type. Each strategy proposes its own
candidates. Capital is allocated across strategies based on rolling
performance.

Allocation algorithm: simple inverse-variance (risk parity) weighting
based on each strategy's 30-day rolling Sharpe ratio. Strategies with
no track record yet get equal-weight default until they accumulate
enough resolved predictions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Per-call default weight: 1/N where N is the number of strategies in
# the current allocation. Prevents the previous hardcoded 1/6 from
# silently breaking as the library grows.
def _default_weight(n_strategies: int) -> float:
    return 1.0 / max(1, n_strategies)


def aggregate_candidates(ctx: Any, universe: List[str],
                         db_path: Optional[str] = None) -> Dict[str, Any]:
    """Run every active strategy across the universe and merge their candidates.

    Returns a dict with:
        candidates: list of merged candidate dicts (one per unique symbol)
        per_strategy_counts: {strategy_name: int} how many candidates each strategy produced
        active_strategies: list of strategy names that ran
    """
    from strategies import get_active_strategies

    market_type = getattr(ctx, "segment", "small")
    strategies = get_active_strategies(market_type, db_path=db_path)

    # When the profile cannot go short, SELL votes from long-only entry
    # strategies would otherwise be scored as bearish sentiment and bias
    # every candidate toward STRONG_SELL — which then gets labeled as "no
    # edge" by the AI. If shorting is disabled, treat SELL votes as HOLD.
    shorting_enabled = bool(getattr(ctx, "enable_short_selling", True))

    by_symbol: Dict[str, Dict[str, Any]] = {}
    per_strategy_counts: Dict[str, int] = {}
    active_names = []

    for mod in strategies:
        name = getattr(mod, "NAME", "unknown")
        active_names.append(name)
        try:
            results = mod.find_candidates(ctx, universe) or []
        except Exception as exc:
            logger.warning("Strategy %s failed: %s", name, exc)
            continue
        per_strategy_counts[name] = len(results)

        for r in results:
            sym = r.get("symbol", "")
            if not sym:
                continue
            signal = r.get("signal", "HOLD")
            raw_score = r.get("score", 0)
            if not shorting_enabled and "SELL" in signal:
                signal = "HOLD"
                raw_score = 0
            existing = by_symbol.get(sym)
            if existing is None:
                # First strategy to flag this symbol
                merged = dict(r)
                merged["signal"] = signal
                merged["score"] = raw_score
                merged["source_strategies"] = [name]
                votes = dict(merged.get("votes", {}))
                votes[name] = signal
                merged["votes"] = votes
                by_symbol[sym] = merged
            else:
                existing["source_strategies"].append(name)
                existing["votes"][name] = signal
                existing_dir = _signal_direction(existing.get("signal", "HOLD"))
                new_dir = _signal_direction(signal)
                if existing_dir == new_dir and existing_dir != 0:
                    existing["score"] = existing.get("score", 0) + (1 if existing_dir > 0 else -1)
                elif existing_dir != 0 and new_dir != 0 and existing_dir != new_dir:
                    pass

    # Re-derive signal label from final score (>=2 STRONG_BUY, 1 BUY, etc)
    for sym, entry in by_symbol.items():
        score = entry.get("score", 0)
        if score >= 2:
            entry["signal"] = "STRONG_BUY"
        elif score == 1:
            entry["signal"] = "BUY"
        elif score == -1:
            entry["signal"] = "SELL"
        elif score <= -2:
            entry["signal"] = "STRONG_SELL"
        # else leave as is

    return {
        "candidates": list(by_symbol.values()),
        "per_strategy_counts": per_strategy_counts,
        "active_strategies": active_names,
    }


def aggregate_shadow_candidates(ctx: Any, universe: List[str],
                                 db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Run every SHADOW auto-strategy and return their candidates.

    Shadow predictions are recorded so we can measure their edge, but the
    returned candidates are flagged and must NOT drive real trades. The
    caller is expected to record predictions via `ai_tracker` and then
    drop the results on the floor.
    """
    from strategies import get_shadow_strategies

    market_type = getattr(ctx, "segment", "small")
    shadows = get_shadow_strategies(market_type, db_path=db_path)
    out: List[Dict[str, Any]] = []
    for mod in shadows:
        name = getattr(mod, "NAME", "unknown")
        try:
            results = mod.find_candidates(ctx, universe) or []
        except Exception as exc:
            logger.warning("Shadow strategy %s failed: %s", name, exc)
            continue
        for r in results:
            r = dict(r)
            r["source_strategies"] = [name]
            r["shadow"] = True
            out.append(r)
    return out


def _signal_direction(signal: str) -> int:
    """+1 for buy, -1 for sell, 0 for hold."""
    if "BUY" in signal:
        return 1
    if "SELL" in signal:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Capital allocation
# ---------------------------------------------------------------------------

def compute_capital_allocations(strategy_names: List[str], db_path: str,
                                 window_days: int = 30) -> Dict[str, float]:
    """Inverse-variance (risk parity) capital allocation across strategies.

    Each strategy's weight is proportional to 1 / variance of returns.
    Strategies with insufficient history get a 1/N default baseline
    until they accumulate >=20 resolved predictions.

    Returns dict {strategy_name: weight} where weights sum to 1.0.
    """
    from alpha_decay import compute_rolling_metrics, compute_lifetime_metrics

    default_weight = _default_weight(len(strategy_names))

    raw_weights: Dict[str, float] = {}
    for name in strategy_names:
        try:
            rolling = compute_rolling_metrics(db_path, name, window_days=window_days)
            lifetime = compute_lifetime_metrics(db_path, name)
        except Exception:
            raw_weights[name] = default_weight
            continue

        # Need enough resolved predictions to estimate variance reliably
        n_lifetime = lifetime.get("n_predictions", 0)
        if n_lifetime < 20:
            raw_weights[name] = default_weight
            continue

        sharpe = rolling.get("sharpe_ratio", 0) or 0
        if sharpe <= 0:
            # No edge or losing — minimum weight
            raw_weights[name] = default_weight * 0.25
            continue

        # Higher Sharpe → higher weight, but bounded so one strategy can't
        # crowd out the others. Cap individual weight at 40%.
        raw_weights[name] = min(sharpe, 4.0)

    # Normalize to sum to 1.0
    total = sum(raw_weights.values())
    if total <= 0:
        # All zero — fall back to equal weight
        n = max(len(strategy_names), 1)
        return {name: 1.0 / n for name in strategy_names}

    normalized = {name: w / total for name, w in raw_weights.items()}

    # Cap any single strategy at 40% of capital, redistributing the excess
    # proportionally to strategies still under the cap. Iterate until stable
    # so that redistribution doesn't push another strategy over the cap.
    # If there's only one strategy (nowhere to redistribute), it keeps 100%.
    CAP = 0.40
    if len(normalized) >= 2:
        for _ in range(len(normalized)):
            over = [n for n, w in normalized.items() if w > CAP + 1e-9]
            if not over:
                break
            under = [n for n, w in normalized.items() if w <= CAP + 1e-9]
            if not under:
                break
            excess = sum(normalized[n] - CAP for n in over)
            for n in over:
                normalized[n] = CAP
            under_total = sum(normalized[n] for n in under)
            if under_total > 0:
                for n in under:
                    normalized[n] += excess * (normalized[n] / under_total)
            else:
                share = excess / len(under)
                for n in under:
                    normalized[n] += share

    return normalized


def get_allocation_summary(db_path: str, market_type: str) -> List[Dict[str, Any]]:
    """Return per-strategy weight + rolling Sharpe + n_trades for the dashboard."""
    from strategies import get_active_strategies
    from alpha_decay import compute_rolling_metrics, compute_lifetime_metrics

    strategies = get_active_strategies(market_type, db_path=db_path)
    names = [getattr(m, "NAME", "?") for m in strategies]
    weights = compute_capital_allocations(names, db_path)

    summary = []
    for name in names:
        try:
            rolling = compute_rolling_metrics(db_path, name, window_days=30)
            lifetime = compute_lifetime_metrics(db_path, name)
        except Exception:
            rolling = {"sharpe_ratio": 0, "n_predictions": 0, "win_rate": 0}
            lifetime = {"sharpe_ratio": 0, "n_predictions": 0}
        summary.append({
            "name": name,
            "weight": round(weights.get(name, 0), 4),
            "rolling_sharpe": rolling.get("sharpe_ratio", 0),
            "lifetime_sharpe": lifetime.get("sharpe_ratio", 0),
            "rolling_n": rolling.get("n_predictions", 0),
            "lifetime_n": lifetime.get("n_predictions", 0),
            "rolling_win_rate": rolling.get("win_rate", 0),
        })
    return summary
