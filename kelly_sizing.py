"""Kelly position sizing helpers.

P4.2 of LONG_SHORT_PLAN.md. The Kelly criterion gives the position
fraction that maximizes long-run logarithmic growth given a known
edge. For trading:

  f* = (bp - q) / b

Where:
  b = avg_win / avg_loss (odds received per dollar risked)
  p = probability of win
  q = 1 - p

Real funds run FRACTIONAL Kelly (typically 0.25 × full) because:
  - Full Kelly is the growth-maximizing fraction, but variance is
    extreme — single bad runs can compound into 50%+ drawdowns.
  - Quarter Kelly captures ~75% of the growth rate at ~50% of
    the variance — nearly always the better risk-adjusted choice.
  - Edge estimates have error; full Kelly assumes perfect knowledge,
    fractional Kelly is the natural Bayesian shrinkage.

This module reads from ai_predictions (per-direction stats from
P1.9b) and computes the Kelly recommendation per direction. The
recommendation is surfaced to the AI prompt as guidance — it
doesn't OVERRIDE the configured max_position_pct, just complements it.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default fractional-Kelly factor. Quarter Kelly is the standard
# pro-fund convention for the variance-vs-growth tradeoff.
DEFAULT_KELLY_FRACTION = 0.25

# Minimum sample size before Kelly recommendation is meaningful.
# Below this, edge estimates are too noisy — return None.
MIN_SAMPLES_FOR_KELLY = 30


def compute_kelly_fraction(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    fractional: float = DEFAULT_KELLY_FRACTION,
) -> Optional[float]:
    """Return the (fractional) Kelly fraction f* for given edge stats.

    Args:
      win_rate: probability of winning, [0.0, 1.0]
      avg_win: average winning return (positive number, e.g. 0.05 = 5%)
      avg_loss: average losing return as a magnitude (positive number,
        e.g. 0.03 = 3% — pass abs() to be safe)
      fractional: Kelly multiplier (0.25 = quarter Kelly default)

    Returns:
      Fraction of capital recommended per trade, OR None when:
        - inputs are zero/negative
        - the resulting Kelly fraction is negative (no edge — don't trade)
        - the result is unreasonably large (>0.5 even after fractional)
    """
    if win_rate is None or win_rate <= 0 or win_rate >= 1:
        return None
    if avg_win is None or avg_win <= 0:
        return None
    if avg_loss is None or avg_loss <= 0:
        return None

    b = avg_win / avg_loss
    p = win_rate
    q = 1 - p
    full_kelly = (b * p - q) / b

    if full_kelly <= 0:
        # Negative Kelly = the edge is negative, don't trade
        return None

    f = full_kelly * fractional
    # Sanity cap when in fractional mode (fractional < 1.0): if even
    # the safety-multiplied recommendation exceeds 50% of capital,
    # the inputs are extreme (single outlier dominating avg_win) and
    # the result isn't trustworthy. In report mode (fractional=1.0)
    # we always return the full Kelly — the caller decides whether
    # to use it directly or apply its own multiplier.
    if fractional < 1.0 and f > 0.50:
        return None
    return f


def compute_kelly_recommendation(
    db_path: str,
    direction: str = "long",
    fractional: float = DEFAULT_KELLY_FRACTION,
    min_samples: int = MIN_SAMPLES_FOR_KELLY,
) -> Optional[Dict[str, Any]]:
    """Read per-direction edge stats from ai_predictions and return
    the Kelly sizing recommendation.

    direction is 'long' (BUY/HOLD predictions) or 'short' (SHORT or
    SELL on non-held).

    Returns a dict like:
      {
        "win_rate": 0.65,
        "avg_win_pct": 0.045,
        "avg_loss_pct": 0.030,
        "n": 124,
        "full_kelly": 0.367,
        "fractional_kelly": 0.092,  # fractional × full
        "fraction_used": 0.25,
      }

    Returns None when there's insufficient sample (n < min_samples)
    or no positive edge.
    """
    if not db_path or direction not in ("long", "short"):
        return None
    try:
        conn = sqlite3.connect(db_path)
        ptype = "directional_long" if direction == "long" else "directional_short"
        # Kelly is for sizing NEW entries — only count rows where we
        # actually took a position. HOLD predictions tagged as
        # directional_long must be excluded; their P&L reflects existing
        # positions, not new bets, and pollutes win rate / avg win/loss.
        if direction == "long":
            entry_signals = ("BUY", "STRONG_BUY")
        else:
            entry_signals = ("SHORT", "SELL", "STRONG_SELL", "STRONG_SHORT")
        placeholders = ",".join("?" * len(entry_signals))
        # Use prediction_type when present; fall back to predicted_signal
        # for legacy rows that haven't been backfilled.
        rows = conn.execute(
            "SELECT actual_outcome, actual_return_pct "
            "FROM ai_predictions "
            "WHERE status = 'resolved' "
            "AND actual_return_pct IS NOT NULL "
            f"AND predicted_signal IN ({placeholders}) "
            "AND ("
            "  prediction_type = ? OR "
            "  prediction_type IS NULL"
            ")",
            (*entry_signals, ptype),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.debug("kelly query failed: %s", exc)
        return None

    if len(rows) < min_samples:
        return None

    wins = []
    losses = []
    for outcome, ret_pct in rows:
        if ret_pct is None:
            continue
        # For directional_short, a NEGATIVE actual_return_pct is a win
        # (price dropped as predicted). The "win" / "loss" outcome
        # column already reflects this — use it directly.
        if outcome == "win":
            wins.append(abs(ret_pct))
        elif outcome == "loss":
            losses.append(abs(ret_pct))

    n = len(wins) + len(losses)
    if n < min_samples or not wins or not losses:
        return None

    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) / 100.0  # to fraction
    avg_loss = sum(losses) / len(losses) / 100.0
    full_k = compute_kelly_fraction(win_rate, avg_win, avg_loss, fractional=1.0)
    if full_k is None:
        return None
    return {
        "win_rate": round(win_rate, 4),
        "avg_win_pct": round(avg_win, 4),
        "avg_loss_pct": round(avg_loss, 4),
        "n": n,
        "full_kelly": round(full_k, 4),
        "fractional_kelly": round(full_k * fractional, 4),
        "fraction_used": fractional,
    }


def render_for_prompt(rec_long: Optional[Dict[str, Any]],
                       rec_short: Optional[Dict[str, Any]]) -> str:
    """Format the long + short Kelly recommendations as a compact
    AI prompt block. Returns empty string when neither side has
    enough data.
    """
    parts = []
    if rec_long and rec_long.get("fractional_kelly"):
        parts.append(
            f"  LONG: Kelly {rec_long['fractional_kelly']*100:.1f}% "
            f"(WR {rec_long['win_rate']*100:.0f}%, avg win "
            f"{rec_long['avg_win_pct']*100:.1f}%, avg loss "
            f"{rec_long['avg_loss_pct']*100:.1f}%, n={rec_long['n']})"
        )
    if rec_short and rec_short.get("fractional_kelly"):
        parts.append(
            f"  SHORT: Kelly {rec_short['fractional_kelly']*100:.1f}% "
            f"(WR {rec_short['win_rate']*100:.0f}%, avg win "
            f"{rec_short['avg_win_pct']*100:.1f}%, avg loss "
            f"{rec_short['avg_loss_pct']*100:.1f}%, n={rec_short['n']})"
        )
    if not parts:
        return ""
    fraction = (rec_long or rec_short or {}).get("fraction_used",
                                                    DEFAULT_KELLY_FRACTION)
    return (
        f"\nKELLY SIZING (fractional={fraction:.2f}):\n"
        f"  Suggested size per trade based on observed edge.\n"
        + "\n".join(parts)
    )
