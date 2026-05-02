"""Strategy-level capital allocator — Item 6b of COMPETITIVE_GAP_PLAN.md.

Within each profile, distribute capital across strategies based on
recent track record. Stronger strategies (higher Sharpe × win-rate)
get larger position sizing; weaker ones get smaller.

This complements the existing profile-level capital_allocator (off
by default — that one rebalances ACROSS profiles within an Alpaca
account). This one rebalances WITHIN a profile across strategies.

Score formula (matches profile-level convention):
    score = recent_sharpe × (1 + win_rate)
Normalized: weights sum to N (number of strategies), so the AVERAGE
weight is 1.0. A strategy with weight 1.5 gets 50% larger positions
than baseline; weight 0.5 gets 50% smaller.

Bounds:
    SCALE_FLOOR = 0.25    (no strategy ever drops below 25% baseline)
    SCALE_CEILING = 2.0   (no strategy ever exceeds 200% baseline)

A strategy with no track record (< MIN_SAMPLES) gets the group median
weight, so new strategies aren't auto-penalized.

Used by trade_pipeline to scale max_position_pct on each AI-proposed
trade according to the strategy that generated the candidate signal.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Bounds — match the profile-level allocator's conventions
SCALE_FLOOR = 0.25
SCALE_CEILING = 2.0
MIN_SAMPLES = 10  # below this, strategy gets median weight (new-strategy fallback)
DEFAULT_WINDOW_DAYS = 30


def _strategy_score(metrics: Dict[str, Any]) -> Optional[float]:
    """Compute per-strategy capital-allocation score from rolling metrics.

    Returns None when there's not enough data (caller treats as median).
    """
    n = int(metrics.get("n_predictions") or 0)
    if n < MIN_SAMPLES:
        return None
    sharpe = float(metrics.get("sharpe_ratio") or 0)
    win_rate = float(metrics.get("win_rate") or 0)  # already 0-1 fraction
    # If sharpe is negative, score goes negative (strategy actively losing)
    return sharpe * (1 + win_rate)


def compute_strategy_weights(
    db_path: str,
    strategies_used: List[str],
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Dict[str, float]:
    """Compute capital weights per strategy for one profile.

    Args:
        db_path: profile journal DB.
        strategies_used: list of strategy_type values active in this
            profile (typically read from the strategy registry).
        window_days: lookback for rolling metrics.

    Returns:
        {strategy_type: weight}, where weights are bounded in
        [SCALE_FLOOR, SCALE_CEILING] and the AVERAGE weight equals
        1.0 (so strategy-level scaling is mean-preserving across the
        active strategy set).

    Empty input → empty output. Single-strategy input → {strategy: 1.0}.
    """
    if not strategies_used:
        return {}
    if len(strategies_used) == 1:
        return {strategies_used[0]: 1.0}

    try:
        from alpha_decay import compute_rolling_metrics
    except ImportError:
        # No metrics module → fall back to neutral weights
        return {s: 1.0 for s in strategies_used}

    # Gather raw scores per strategy
    raw_scores: Dict[str, Optional[float]] = {}
    for strategy in strategies_used:
        try:
            metrics = compute_rolling_metrics(
                db_path, strategy, window_days=window_days,
            )
            raw_scores[strategy] = _strategy_score(metrics)
        except Exception as exc:
            logger.debug("metrics fetch failed for %s: %s", strategy, exc)
            raw_scores[strategy] = None

    # Median imputation for new / no-data strategies. Use median of
    # the observed scores; if NONE have track records, all-1.0.
    observed = [s for s in raw_scores.values() if s is not None]
    if not observed:
        return {s: 1.0 for s in strategies_used}

    median = sorted(observed)[len(observed) // 2]
    filled_scores: Dict[str, float] = {
        strategy: (score if score is not None else median)
        for strategy, score in raw_scores.items()
    }

    # Translate scores to weights so that:
    #   - average weight = 1.0
    #   - bounded in [SCALE_FLOOR, SCALE_CEILING]
    # Approach: shift scores so min becomes a small positive number,
    # then normalize so the mean is 1.0. Apply bounds. Re-normalize
    # if clamping shifts the mean materially.
    scores = list(filled_scores.values())
    score_min = min(scores)

    # Shift so min(score) = 0.5 to keep sane positive multipliers
    shifted = {s: filled_scores[s] - score_min + 0.5 for s in strategies_used}
    shifted_mean = sum(shifted.values()) / len(shifted)
    if shifted_mean <= 0:
        return {s: 1.0 for s in strategies_used}

    weights = {s: shifted[s] / shifted_mean for s in strategies_used}

    # Clamp to bounds
    weights = {s: max(SCALE_FLOOR, min(SCALE_CEILING, w))
               for s, w in weights.items()}

    # Re-center to mean ~1.0 after clamping. Multiply each weight by
    # 1 / mean(weights). Re-clamp once to enforce bounds again.
    new_mean = sum(weights.values()) / len(weights)
    if new_mean > 0 and abs(new_mean - 1.0) > 0.01:
        scale = 1.0 / new_mean
        weights = {s: max(SCALE_FLOOR, min(SCALE_CEILING, w * scale))
                   for s, w in weights.items()}

    # Round for display sanity
    return {s: round(w, 3) for s, w in weights.items()}


def render_weights_for_prompt(weights: Dict[str, float]) -> str:
    """Build an AI-prompt block surfacing current strategy weights.

    AI can use this to bias which strategies it picks from when
    multiple shortlist candidates are tied on raw signal strength.
    """
    if not weights or len(weights) <= 1:
        return ""
    lines = ["STRATEGY CAPITAL WEIGHTS (per recent rolling Sharpe × win rate):"]
    for s, w in sorted(weights.items(), key=lambda x: -x[1]):
        marker = ("⬆" if w >= 1.2
                  else "⬇" if w <= 0.8
                  else "·")
        lines.append(f"  {marker} {s}: {w:.2f}x baseline")
    lines.append(
        "  → Position sizes on AI-proposed trades scale by the "
        "candidate's strategy weight."
    )
    return "\n".join(lines)
