"""Alpha decay monitoring — keep the alpha pool fresh.

Phase 3 of the Quant Fund Evolution roadmap (see ROADMAP.md).

Every signal decays over time. Momentum worked for decades then got
arbitraged away. Value investing has been dead for years. Most systems
— retail AND institutional — cling to dead strategies because no one
measures decay rigorously.

This module:
  1. Computes rolling performance metrics per strategy_type from the
     profile's ai_predictions table.
  2. Writes daily snapshots to signal_performance_history so we have a
     historical curve of each signal's edge over time.
  3. Detects when a strategy's rolling Sharpe has degraded materially
     from its lifetime average for N consecutive days.
  4. Auto-deprecates strategies that cross the decay threshold. The
     trade pipeline checks this table and skips deprecated strategies.
  5. Restores strategies if their rolling edge recovers.

Core principle: every strategy is innocent until proven dead, and
proven dead just means "failed to maintain its historical edge for
30+ consecutive days of fresh data."
"""

from __future__ import annotations

import logging
import math
import sqlite3
import statistics
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decay detection thresholds
# ---------------------------------------------------------------------------

DECAY_THRESHOLDS = {
    "rolling_window_days": 30,         # short-term rolling window for "current" edge
    "lifetime_min_predictions": 50,    # below this, no lifetime baseline
    "rolling_min_predictions": 10,     # below this, rolling metric is noise
    "sharpe_degradation_pct": 30.0,    # rolling Sharpe must be >=30% below lifetime
    "consecutive_bad_days": 30,        # that degradation must persist this many days
    "restoration_recovery_pct": 15.0,  # within this much of lifetime Sharpe to restore
    "restoration_good_days": 14,       # for this many consecutive days
}


# ---------------------------------------------------------------------------
# Rolling metrics computation
# ---------------------------------------------------------------------------

def compute_rolling_metrics(
    db_path: str,
    strategy_type: str,
    window_days: int = 30,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute rolling metrics for one strategy_type on a given date.

    Parameters
    ----------
    db_path : str
        Per-profile database path.
    strategy_type : str
        Value from ai_predictions.strategy_type (e.g., "mean_reversion").
    window_days : int
        Look at predictions resolved within the last N days.
    as_of : str, optional
        ISO date string. Defaults to "now".

    Returns
    -------
    dict with: n_predictions, wins, losses, win_rate, avg_return_pct,
    sharpe_ratio, profit_factor. Returns zeros when insufficient data.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if as_of:
        cutoff_sql = (
            "AND resolved_at IS NOT NULL "
            "AND resolved_at <= ? "
            "AND resolved_at >= datetime(?, ? || ' days')"
        )
        params = (strategy_type, as_of, as_of, f"-{window_days}")
    else:
        cutoff_sql = (
            "AND resolved_at IS NOT NULL "
            "AND resolved_at >= datetime('now', ? || ' days')"
        )
        params = (strategy_type, f"-{window_days}")

    try:
        rows = conn.execute(
            f"SELECT actual_outcome, actual_return_pct FROM ai_predictions "
            f"WHERE strategy_type = ? "
            f"AND status = 'resolved' "
            f"{cutoff_sql}",
            params,
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("Failed to query ai_predictions: %s", exc)
        conn.close()
        return _empty_metrics()
    finally:
        conn.close()

    return _metrics_from_rows(rows)


def compute_lifetime_metrics(
    db_path: str,
    strategy_type: str,
    exclude_recent_days: int = 0,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute the strategy's "lifetime" baseline metrics on
    resolved predictions STRICTLY OLDER than the rolling window.

    Wave 3 / Fix #8 (METHODOLOGY_FIX_PLAN.md): the previous
    implementation queried ALL resolved predictions, which meant the
    rolling-window data was INCLUDED in the lifetime baseline. When
    `decay_detector` compared rolling vs lifetime Sharpe to detect
    degradation, the comparison was less sensitive than it should be
    because both sides shared the most-recent data.

    Now: `lifetime` covers `[earliest, as_of - exclude_recent_days]`,
    `rolling` (separate function) covers
    `[as_of - exclude_recent_days, as_of]`. The two windows are
    strictly disjoint in time.

    Pass `exclude_recent_days=0` to get the legacy "all resolved
    predictions" behavior (kept for backwards compatibility).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        if exclude_recent_days <= 0:
            rows = conn.execute(
                "SELECT actual_outcome, actual_return_pct "
                "FROM ai_predictions "
                "WHERE strategy_type = ? AND status = 'resolved'",
                (strategy_type,),
            ).fetchall()
        elif as_of:
            rows = conn.execute(
                "SELECT actual_outcome, actual_return_pct "
                "FROM ai_predictions "
                "WHERE strategy_type = ? AND status = 'resolved' "
                "AND resolved_at IS NOT NULL "
                "AND resolved_at <= datetime(?, ? || ' days')",
                (strategy_type, as_of, f"-{exclude_recent_days}"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT actual_outcome, actual_return_pct "
                "FROM ai_predictions "
                "WHERE strategy_type = ? AND status = 'resolved' "
                "AND resolved_at IS NOT NULL "
                "AND resolved_at <= datetime('now', ? || ' days')",
                (strategy_type, f"-{exclude_recent_days}"),
            ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return _empty_metrics()
    finally:
        conn.close()

    return _metrics_from_rows(rows)


def _metrics_from_rows(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    """Core metric computation — works on a list of prediction rows."""
    n = len(rows)
    if n == 0:
        return _empty_metrics()

    wins = sum(1 for r in rows if r["actual_outcome"] == "win")
    losses = n - wins
    returns = [float(r["actual_return_pct"] or 0) for r in rows]

    win_rate = (wins / n * 100) if n > 0 else 0.0
    avg_return = statistics.mean(returns)

    # Sharpe on per-prediction returns — annualizing assumes ~252 predictions
    # per year which matches our typical scan cadence.
    if len(returns) >= 2:
        stdev = statistics.stdev(returns)
        sharpe = (avg_return / stdev) * math.sqrt(252) if stdev > 0 else 0.0
    else:
        sharpe = 0.0

    # Profit factor: gross wins / gross losses
    gross_wins = sum(r for r in returns if r > 0)
    gross_losses = abs(sum(r for r in returns if r < 0))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (
        float("inf") if gross_wins > 0 else 0.0
    )

    return {
        "n_predictions": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "avg_return_pct": round(avg_return, 3),
        "sharpe_ratio": round(sharpe, 3),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
    }


def _empty_metrics() -> Dict[str, Any]:
    return {
        "n_predictions": 0, "wins": 0, "losses": 0,
        "win_rate": 0.0, "avg_return_pct": 0.0,
        "sharpe_ratio": 0.0, "profit_factor": 0.0,
    }


# ---------------------------------------------------------------------------
# Snapshot writing
# ---------------------------------------------------------------------------

def snapshot_all_strategies(
    db_path: str,
    window_days: int = 30,
    as_of: Optional[str] = None,
) -> List[str]:
    """Write one snapshot row per distinct strategy_type for this date.

    Returns list of strategy_type names that were snapshotted.
    """
    from datetime import datetime

    snapshot_date = as_of or datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT strategy_type FROM ai_predictions "
            "WHERE strategy_type IS NOT NULL AND strategy_type != ''"
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []

    types = [r[0] for r in rows]

    for stype in types:
        metrics = compute_rolling_metrics(db_path, stype, window_days, as_of)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO signal_performance_history
                   (snapshot_date, strategy_type, window_days,
                    n_predictions, wins, losses, win_rate,
                    avg_return_pct, sharpe_ratio, profit_factor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_date, stype, window_days,
                    metrics["n_predictions"], metrics["wins"], metrics["losses"],
                    metrics["win_rate"], metrics["avg_return_pct"],
                    metrics["sharpe_ratio"], metrics.get("profit_factor"),
                ),
            )
        except sqlite3.OperationalError as exc:
            logger.warning("Failed to write snapshot for %s: %s", stype, exc)

    conn.commit()
    conn.close()
    logger.info("Wrote %d signal performance snapshots for %s", len(types), snapshot_date)
    return types


# ---------------------------------------------------------------------------
# Decay detection
# ---------------------------------------------------------------------------

def detect_decay(
    db_path: str,
    strategy_type: str,
    thresholds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Check whether a strategy has decayed below its lifetime edge.

    Algorithm:
      1. Compute lifetime Sharpe from all resolved predictions.
      2. For each of the last N snapshot days (window_days + consecutive_bad_days),
         check whether rolling Sharpe was degraded by >=threshold%.
      3. If degradation persisted for consecutive_bad_days in a row -> DECAY.

    Returns dict with: decay_detected, lifetime_sharpe, current_rolling_sharpe,
    consecutive_bad_days, degradation_pct, reason.
    """
    t = dict(DECAY_THRESHOLDS)
    if thresholds:
        t.update(thresholds)

    # Lifetime baseline excludes the recent rolling window so the
    # comparison "rolling vs lifetime" reads disjoint data. Without
    # this, rolling-window predictions are inside lifetime, biasing
    # the baseline toward recent performance and dampening decay
    # detection. See METHODOLOGY_FIX_PLAN.md Wave 3 / Fix #8.
    lifetime = compute_lifetime_metrics(
        db_path, strategy_type,
        exclude_recent_days=int(t.get("rolling_window_days", 30)),
    )
    if lifetime["n_predictions"] < t["lifetime_min_predictions"]:
        return {
            "decay_detected": False,
            "lifetime_sharpe": lifetime["sharpe_ratio"],
            "current_rolling_sharpe": None,
            "consecutive_bad_days": 0,
            "degradation_pct": 0.0,
            "reason": f"Insufficient lifetime data ({lifetime['n_predictions']} < {t['lifetime_min_predictions']})",
        }

    lifetime_sharpe = lifetime["sharpe_ratio"]
    if lifetime_sharpe <= 0:
        return {
            "decay_detected": False,
            "lifetime_sharpe": lifetime_sharpe,
            "current_rolling_sharpe": None,
            "consecutive_bad_days": 0,
            "degradation_pct": 0.0,
            "reason": "Lifetime Sharpe is non-positive — can't detect decay of a strategy with no edge",
        }

    # Pull most recent N snapshots
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT snapshot_date, sharpe_ratio, n_predictions
               FROM signal_performance_history
               WHERE strategy_type = ? AND window_days = ?
               ORDER BY snapshot_date DESC
               LIMIT ?""",
            (strategy_type, t["rolling_window_days"], t["consecutive_bad_days"] + 5),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()

    if not rows:
        return {
            "decay_detected": False,
            "lifetime_sharpe": lifetime_sharpe,
            "current_rolling_sharpe": None,
            "consecutive_bad_days": 0,
            "degradation_pct": 0.0,
            "reason": "No snapshot history yet — decay detector needs daily snapshots to accumulate",
        }

    # Threshold rolling Sharpe = lifetime_sharpe * (1 - degradation/100)
    bad_threshold = lifetime_sharpe * (1 - t["sharpe_degradation_pct"] / 100)

    # Count consecutive days (from newest) where rolling Sharpe < bad_threshold.
    # Skip days with too few predictions (rolling_min_predictions).
    consecutive = 0
    for row in rows:
        rolling_sharpe = row[1] or 0
        n_preds = row[2] or 0
        if n_preds < t["rolling_min_predictions"]:
            # Not enough data on this day — can't count as good or bad
            break
        if rolling_sharpe < bad_threshold:
            consecutive += 1
        else:
            break

    current_rolling = rows[0][1] if rows else None
    degradation_pct = (
        (lifetime_sharpe - current_rolling) / lifetime_sharpe * 100
        if current_rolling is not None and lifetime_sharpe > 0 else 0.0
    )

    decayed = consecutive >= t["consecutive_bad_days"]

    return {
        "decay_detected": decayed,
        "lifetime_sharpe": round(lifetime_sharpe, 3),
        "current_rolling_sharpe": round(current_rolling, 3) if current_rolling is not None else None,
        "consecutive_bad_days": consecutive,
        "degradation_pct": round(degradation_pct, 1),
        "reason": (
            f"Rolling Sharpe below {t['sharpe_degradation_pct']:.0f}% of lifetime "
            f"for {consecutive} consecutive days"
            if decayed
            else f"{consecutive} bad day(s) — threshold is {t['consecutive_bad_days']}"
        ),
    }


# ---------------------------------------------------------------------------
# Auto-deprecation / restoration
# ---------------------------------------------------------------------------

def deprecate_strategy(
    db_path: str,
    strategy_type: str,
    detection: Dict[str, Any],
) -> None:
    """Mark a strategy as deprecated so the pipeline skips its signals."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO deprecated_strategies
               (strategy_type, deprecated_at, reason,
                rolling_sharpe_at_deprecation, lifetime_sharpe,
                consecutive_bad_days, restored_at)
               VALUES (?, datetime('now'), ?, ?, ?, ?, NULL)""",
            (
                strategy_type,
                detection.get("reason", "alpha decay detected"),
                detection.get("current_rolling_sharpe"),
                detection.get("lifetime_sharpe"),
                detection.get("consecutive_bad_days", 0),
            ),
        )
        conn.commit()
        logger.warning("Deprecated strategy '%s': %s", strategy_type, detection["reason"])
    finally:
        conn.close()


def restore_strategy(db_path: str, strategy_type: str) -> None:
    """Undo a deprecation — strategy is active again in the pipeline."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE deprecated_strategies SET restored_at = datetime('now') "
            "WHERE strategy_type = ? AND restored_at IS NULL",
            (strategy_type,),
        )
        conn.commit()
        logger.info("Restored strategy '%s'", strategy_type)
    finally:
        conn.close()


def is_deprecated(db_path: str, strategy_type: str) -> bool:
    """Check whether a strategy is currently deprecated."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM deprecated_strategies "
            "WHERE strategy_type = ? AND restored_at IS NULL",
            (strategy_type,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def check_restoration(
    db_path: str,
    strategy_type: str,
    thresholds: Optional[Dict[str, Any]] = None,
) -> bool:
    """Should this deprecated strategy be restored? True if rolling edge has
    recovered to within X% of lifetime for Y consecutive days.
    """
    t = dict(DECAY_THRESHOLDS)
    if thresholds:
        t.update(thresholds)

    if not is_deprecated(db_path, strategy_type):
        return False

    lifetime = compute_lifetime_metrics(
        db_path, strategy_type,
        exclude_recent_days=int(t.get("rolling_window_days", 30)),
    )
    if lifetime["sharpe_ratio"] <= 0:
        return False

    # Target threshold: rolling must be within restoration_recovery_pct% of lifetime
    recovery_threshold = lifetime["sharpe_ratio"] * (1 - t["restoration_recovery_pct"] / 100)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT sharpe_ratio, n_predictions FROM signal_performance_history
               WHERE strategy_type = ? AND window_days = ?
               ORDER BY snapshot_date DESC
               LIMIT ?""",
            (strategy_type, t["rolling_window_days"], t["restoration_good_days"]),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()

    if len(rows) < t["restoration_good_days"]:
        return False  # need enough good days

    for row in rows:
        rolling = row[0] or 0
        n = row[1] or 0
        if n < t["rolling_min_predictions"] or rolling < recovery_threshold:
            return False

    return True


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_decay_cycle(db_path: str) -> Dict[str, Any]:
    """One full cycle: snapshot, detect decay, deprecate/restore as needed.

    Safe to call daily. Idempotent — reruns on the same day overwrite that
    day's snapshot rather than duplicating.
    """
    summary = {
        "strategies_snapshotted": [],
        "newly_deprecated": [],
        "restored": [],
        "errors": [],
    }

    # Step 1: write today's snapshots for every known strategy_type
    try:
        summary["strategies_snapshotted"] = snapshot_all_strategies(db_path)
    except Exception as exc:
        summary["errors"].append(f"snapshot: {exc}")
        return summary

    # Step 2: check each strategy for decay or restoration
    for stype in summary["strategies_snapshotted"]:
        try:
            if is_deprecated(db_path, stype):
                if check_restoration(db_path, stype):
                    restore_strategy(db_path, stype)
                    summary["restored"].append(stype)
            else:
                detection = detect_decay(db_path, stype)
                if detection["decay_detected"]:
                    deprecate_strategy(db_path, stype, detection)
                    summary["newly_deprecated"].append(stype)
        except Exception as exc:
            summary["errors"].append(f"{stype}: {exc}")

    return summary


def list_deprecated(db_path: str) -> List[Dict[str, Any]]:
    """Return all currently-deprecated strategies with decay context."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM deprecated_strategies "
            "WHERE restored_at IS NULL "
            "ORDER BY deprecated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def get_performance_history(
    db_path: str,
    strategy_type: str,
    days: int = 90,
) -> List[Dict[str, Any]]:
    """Return the snapshot series for charting a strategy's decay curve."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT snapshot_date, sharpe_ratio, win_rate, n_predictions
               FROM signal_performance_history
               WHERE strategy_type = ?
               ORDER BY snapshot_date DESC
               LIMIT ?""",
            (strategy_type, days),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
