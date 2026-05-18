"""Self-tuning feedback loop — feeds past performance into AI prompts.

Now includes tuning memory: every adjustment is logged, reviewed after 3 days,
and the outcomes are fed back into future decisions so the system learns from
its own learning.
"""

import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy type → profile toggle column mapping (for auto-disable)
# ---------------------------------------------------------------------------
_STRATEGY_TYPE_TO_TOGGLE = {
    "momentum_breakout": "strategy_momentum_breakout",
    "volume_spike": "strategy_volume_spike",
    "mean_reversion": "strategy_mean_reversion",
    "gap_and_go": "strategy_gap_and_go",
}

# ---------------------------------------------------------------------------
# In-memory cache (30-minute TTL, keyed by profile db_path)
# ---------------------------------------------------------------------------
_cache: Dict[str, Any] = {}
_CACHE_TTL = 30 * 60  # 30 minutes


# ---------------------------------------------------------------------------
# Guardrails on parameter changes (Phase 1 of docs/17, 2026-05-18)
# ---------------------------------------------------------------------------
# Closes the over-restriction failure mode documented in
# `project_self_tuner_overcorrection_2026_05_14` (14d compounding tightening
# killed stock entries entirely). Three deterministic checks every parameter
# adjustment passes through before it's applied:
#
#   1. Per-cycle delta cap — no single change exceeds MAX_PCT_PER_CYCLE
#      of the current value (defaults 25%). Stops a single cycle from
#      cutting/doubling a parameter; the cascade can only build over many
#      cycles, which steps #2 and #3 then catch.
#   2. Reference-window invariant — the new value can't drift more than
#      MAX_PCT_FROM_REFERENCE (default 50%) from the day-1 reference value.
#      Defense in depth.
#   3. (Phase 1 #2 in docs/17) Trade-count starvation auto-loosen —
#      built as a separate scheduled task that bypasses this clamp when
#      forcing a parameter to loosen.

_MAX_PCT_PER_CYCLE = 0.25       # 25% per single tuning cycle
_MAX_PCT_FROM_REFERENCE = 0.50  # 50% from day-1 baseline


def _clamp_delta(param_name: str,
                 old_value: float,
                 new_value: float,
                 max_pct_change: float = _MAX_PCT_PER_CYCLE) -> tuple:
    """Clamp a proposed parameter change so no single adjustment exceeds
    `max_pct_change` of the current value in either direction.

    Returns `(clamped_new, was_clamped, reason)`:
      - clamped_new: float — the value to actually write
      - was_clamped: bool — True if the clamp fired
      - reason: str — empty when not clamped, else describes the cap

    Examples (default 25% cap):
        _clamp_delta("max_position_pct", 0.08, 0.05)  → (0.06, True, ...)
        _clamp_delta("max_position_pct", 0.08, 0.09)  → (0.09, False, "")
        _clamp_delta("ai_confidence_threshold", 60, 90) → (75, True, ...)
        _clamp_delta("ai_confidence_threshold", 60, 65) → (65, False, "")

    Edge cases:
      - old_value == 0: returns new_value unchanged (can't compute %).
      - old_value < 0: uses abs(old_value) as the magnitude.
      - new_value == old_value: returns (new_value, False, "").
    """
    try:
        old_f = float(old_value)
        new_f = float(new_value)
    except (TypeError, ValueError):
        return new_value, False, ""
    if old_f == 0 or old_f == new_f:
        return new_f, False, ""
    pct_change = (new_f - old_f) / abs(old_f)
    # 1e-9 tolerance — a proposal of exactly max_pct_change is in-band;
    # IEEE 754 rounding (e.g. 0.075/0.10 → 0.2500000000000000022) must not
    # spuriously fire the clamp at the boundary.
    if abs(pct_change) <= max_pct_change + 1e-9:
        return new_f, False, ""
    # Clamp in the direction of the proposed change
    direction = 1 if pct_change > 0 else -1
    clamped = old_f * (1 + direction * max_pct_change)
    reason = (
        f"per-cycle delta cap: proposed {pct_change*100:+.1f}% change to "
        f"{param_name} exceeds ±{max_pct_change*100:.0f}% — clamped to "
        f"{clamped:.4g} (from {old_f:.4g})"
    )
    return clamped, True, reason


def _within_reference_window(param_name: str,
                             reference_value: Optional[float],
                             proposed_value: float,
                             max_pct: float = _MAX_PCT_FROM_REFERENCE) -> tuple:
    """Defense in depth — reject proposed values that drift more than
    `max_pct` from the day-1 reference value, even if a single cycle's
    delta is within the per-cycle cap. Prevents the cascade scenario
    where 14 consecutive small tightening cycles compound past safety.

    Returns `(allowed_value, was_clamped, reason)`. When reference_value
    is None (no baseline recorded yet), returns the proposed value
    unchanged.
    """
    if reference_value is None or reference_value == 0:
        return proposed_value, False, ""
    try:
        ref = float(reference_value)
        prop = float(proposed_value)
    except (TypeError, ValueError):
        return proposed_value, False, ""
    pct_from_ref = (prop - ref) / abs(ref)
    if abs(pct_from_ref) <= max_pct:
        return prop, False, ""
    direction = 1 if pct_from_ref > 0 else -1
    clamped = ref * (1 + direction * max_pct)
    reason = (
        f"reference-window invariant: proposed value drifts "
        f"{pct_from_ref*100:+.1f}% from day-1 reference {ref:.4g} for "
        f"{param_name}; clamped to {clamped:.4g} (±{max_pct*100:.0f}% band)"
    )
    return clamped, True, reason


def _apply_param_change(profile_id: int, user_id: int,
                        adjustment_type: str, param_name: str,
                        old_value, proposed_new_value,
                        reason: str,
                        win_rate_at_change: Optional[float] = None,
                        predictions_resolved: Optional[int] = None,
                        max_pct_change: float = _MAX_PCT_PER_CYCLE) -> tuple:
    """Single entry point every `_optimize_*` function must call for
    parameter changes. Wraps `update_trading_profile + log_tuning_change`
    behind the guardrails so the per-cycle cap can't be bypassed by
    a future optimizer that forgets to wrap its own write.

    Returns `(applied_value, was_clamped, suffix)`:
      - applied_value: the value actually written (post-clamp)
      - was_clamped: bool
      - suffix: text describing the clamp (for appending to the
        adjustment narrative) — empty when no clamp fired

    The wrapper persists the CLAMPED value in both the profile config
    and the tuning_history row, so /api/tuning-history and reviewers
    see the real applied change, not the proposed one.
    """
    from models import update_trading_profile, log_tuning_change
    applied, was_clamped, clamp_reason = _clamp_delta(
        param_name, old_value, proposed_new_value, max_pct_change,
    )
    # Cast to the column type the profile expects
    cast_value = _cast_param_value(param_name, str(applied))
    update_kwargs = {param_name: cast_value}
    update_trading_profile(profile_id, **update_kwargs)
    final_reason = reason
    if was_clamped:
        final_reason = (
            f"{reason} [guardrail: {clamp_reason}]"
        )
    log_tuning_change(
        profile_id, user_id, adjustment_type,
        param_name, str(old_value), str(applied), final_reason,
        win_rate_at_change=win_rate_at_change,
        predictions_resolved=predictions_resolved,
    )
    return applied, was_clamped, (
        f" (clamped by guardrail to {applied:.4g})" if was_clamped else ""
    )


def _is_cached(key: str) -> bool:
    ts_key = f"{key}_ts"
    return (
        _cache.get(key) is not None
        and (time.time() - _cache.get(ts_key, 0)) < _CACHE_TTL
    )


def _set_cache(key: str, value: Any) -> None:
    _cache[key] = value
    _cache[f"{key}_ts"] = time.time()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_conn(db_path=None):
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # See models._get_conn — busy_timeout eliminates transient-lock
    # OperationalError on concurrent reader/writer races.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _get_current_win_rate(conn):
    """Return (win_rate_pct, total_resolved) from ai_predictions."""
    resolved = conn.execute(
        "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
    ).fetchone()[0]
    if resolved == 0:
        return 0.0, 0
    wins = conn.execute(
        "SELECT COUNT(*) FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome='win'"
    ).fetchone()[0]
    return (wins / resolved * 100), resolved


def _days_ago_label(timestamp_str):
    """Return a human-readable 'X days ago' label from an ISO timestamp."""
    try:
        ts = datetime.fromisoformat(timestamp_str)
        delta = datetime.utcnow() - ts
        days = delta.days
        if days == 0:
            return "today"
        elif days == 1:
            return "1 day ago"
        else:
            return f"{days} days ago"
    except Exception:
        return "recently"


# ---------------------------------------------------------------------------
# Tuning history helpers (delegate to models.py)
# ---------------------------------------------------------------------------

def _get_tuning_history(profile_id, limit=20):
    """Get tuning history from the central DB."""
    try:
        from models import get_tuning_history
        return get_tuning_history(profile_id, limit=limit)
    except Exception:
        return []


def _get_recent_adjustment(profile_id, parameter_name, days=3):
    """Check if a specific parameter was adjusted within the last N days.

    Returns the most recent matching adjustment dict, or None.
    """
    history = _get_tuning_history(profile_id, limit=50)
    cutoff = datetime.utcnow() - timedelta(days=days)
    for entry in history:
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            if ts >= cutoff and entry["parameter_name"] == parameter_name:
                return entry
        except (KeyError, ValueError, TypeError) as _ts_exc:
            # Per-entry parse over a history list; skip malformed
            # rows but surface the data-quality issue at DEBUG.
            logger.debug(
                "tuning history row skipped (bad timestamp): %s: %s",
                type(_ts_exc).__name__, _ts_exc,
            )
            continue
    return None


def _was_adjustment_effective(profile_id, parameter_name):
    """Check the most recent reviewed adjustment for this parameter.

    Returns: 'improved', 'worsened', 'unchanged', or None if no reviewed data.
    """
    history = _get_tuning_history(profile_id, limit=50)
    for entry in history:
        if (entry["parameter_name"] == parameter_name
                and entry["outcome_after"] != "pending"):
            return entry["outcome_after"]
    return None


# ---------------------------------------------------------------------------
# Cross-Profile Learning (Feature 4)
# ---------------------------------------------------------------------------

def _build_cross_profile_insights(user_id, current_profile_id, current_db_path):
    """Compare performance across all of a user's profiles and build insight text.

    Only produces output if there are 2+ profiles with 20+ resolved predictions each,
    and another profile significantly outperforms the current one (15%+ higher win rate).

    Returns insight string or empty string.
    """
    try:
        from models import get_user_profiles, get_trading_profile
    except ImportError:
        return ""

    try:
        profiles = get_user_profiles(user_id)
    except Exception:
        return ""

    if len(profiles) < 2:
        return ""

    # Gather stats for each profile
    profile_stats = []
    for prof in profiles:
        pid = prof["id"]
        db = f"quantopsai_profile_{pid}.db"

        try:
            conn = _get_conn(db)
        except (sqlite3.OperationalError, sqlite3.DatabaseError,
                OSError) as _db_exc:
            # Per-profile DB open; skip the profile if its DB is
            # unavailable but surface for follow-up.
            logger.debug(
                "skipping profile %s in cross-profile insights, DB open failed: %s: %s",
                pid, type(_db_exc).__name__, _db_exc,
            )
            continue

        try:
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
            ).fetchone()
            if not table_check:
                continue

            # 2026-05-13 — exclude data_quality-tagged ai_predictions
            # rows from analytics. See journal.data_quality_clause.
            from journal import data_quality_clause
            _aip_dq = data_quality_clause(conn, table="ai_predictions")
            resolved = conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions "
                f"WHERE status='resolved'{_aip_dq}"
            ).fetchone()[0]

            if resolved < 20:
                continue

            wins = conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions "
                f"WHERE status='resolved' AND actual_outcome='win'{_aip_dq}"
            ).fetchone()[0]
            # DISPLAY_ONLY: div-by-zero guard for cross-profile insight stats.
            win_rate = (wins / resolved * 100) if resolved > 0 else 0

            avg_return = conn.execute(
                f"SELECT AVG(actual_return_pct) FROM ai_predictions "
                f"WHERE status='resolved'{_aip_dq}"
            ).fetchone()[0] or 0

            # BUY-specific stats
            buy_total = conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions "
                f"WHERE status='resolved' AND predicted_signal IN ('BUY','STRONG_BUY','WEAK_BUY'){_aip_dq}"
            ).fetchone()[0]
            buy_wins = conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions "
                f"WHERE status='resolved' AND actual_outcome='win' AND predicted_signal IN ('BUY','STRONG_BUY','WEAK_BUY'){_aip_dq}"
            ).fetchone()[0]
            buy_avg_ret = conn.execute(
                f"SELECT AVG(actual_return_pct) FROM ai_predictions "
                f"WHERE status='resolved' AND predicted_signal IN ('BUY','STRONG_BUY','WEAK_BUY'){_aip_dq}"
            ).fetchone()[0] or 0
            # DISPLAY_ONLY: div-by-zero guard for cross-profile insights dict.
            buy_wr = (buy_wins / buy_total * 100) if buy_total > 0 else 0

            profile_stats.append({
                "id": pid,
                "name": prof["name"],
                "resolved": resolved,
                "win_rate": win_rate,
                "avg_return": avg_return,
                "buy_wr": buy_wr,
                "buy_avg_ret": buy_avg_ret,
                "ai_confidence_threshold": prof.get("ai_confidence_threshold", 25),
                "min_price": prof.get("min_price", 1.0),
                "max_price": prof.get("max_price", 20.0),
            })
        except Exception as exc:
            logger.warning("Cross-profile: error reading profile #%d: %s", pid, exc)
            continue
        finally:
            try:
                conn.close()
            except sqlite3.ProgrammingError as _cl_exc:
                # Cleanup close — conn may already be closed.
                logger.debug(
                    "cross-profile insights conn close: %s: %s",
                    type(_cl_exc).__name__, _cl_exc,
                )

    # Need 2+ profiles with enough data
    if len(profile_stats) < 2:
        return ""

    # Find current profile stats
    current_stats = None
    for ps in profile_stats:
        if ps["id"] == current_profile_id:
            current_stats = ps
            break

    if current_stats is None:
        return ""

    # Find profiles that significantly outperform the current one
    lines = []
    for other in profile_stats:
        if other["id"] == current_profile_id:
            continue

        wr_diff = other["win_rate"] - current_stats["win_rate"]
        if wr_diff >= 15:
            lines.append("CROSS-PROFILE INSIGHTS:")
            lines.append(
                f'Your "{other["name"]}" profile wins {other["buy_wr"]:.0f}% '
                f'on BUY signals with avg return {other["buy_avg_ret"]:+.1f}%.'
            )
            lines.append(
                f'This profile ("{current_stats["name"]}") only wins '
                f'{current_stats["buy_wr"]:.0f}% with avg return '
                f'{current_stats["buy_avg_ret"]:+.1f}%.'
            )
            lines.append("")
            lines.append(f'Key differences in "{other["name"]}":')
            lines.append(
                f'  - AI confidence threshold: {other["ai_confidence_threshold"]} '
                f'(this profile: {current_stats["ai_confidence_threshold"]})'
            )
            lines.append(
                f'  - Price range: ${other["min_price"]:.0f}-${other["max_price"]:.0f} '
                f'(this profile: ${current_stats["min_price"]:.0f}-${current_stats["max_price"]:.0f})'
            )
            lines.append("")
            lines.append(
                f'The AI appears more accurate on "{other["name"]}" stocks '
                f'in the current market.'
            )
            lines.append("Consider being more selective on this profile.")
            break  # Only show the best-performing comparison

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-stock reputation (Feature 2: Auto-Blacklist)
# ---------------------------------------------------------------------------

def get_symbol_reputation(db_path, min_predictions=3):
    """Get win rate per symbol from ai_predictions, split by signal type.

    Returns dict: {symbol: {
        "wins": N, "losses": N, "total": N, "win_rate": float,
        "avg_return": float,
        "by_signal": {"BUY": {wins, losses, total, win_rate}, "SHORT": {...}, ...},
    }}

    The `by_signal` breakdown lets the AI prompt cite signal-specific
    track records instead of lumping HOLD outcomes into BUY/SHORT
    confidence. Without this split, a symbol with 13W/0L on HOLDs
    looks like a 100% BUY/SHORT track record to the AI prompt — which
    triggered the 2026-04-28 confabulation bug where the system
    narrated "100% win rate on VALE SHORT signals" while shorting a
    name that had only ever been HELD.

    Only includes symbols with min_predictions resolved (across all
    signal types combined).
    """
    try:
        conn = _get_conn(db_path)
    except Exception:
        return {}

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            return {}

        # Per-signal-type aggregation. Group by (symbol, signal) so
        # we can build the by_signal breakdown.
        rows = conn.execute(
            "SELECT symbol, predicted_signal, COUNT(*) as total, "
            "SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins, "
            "AVG(actual_return_pct) as avg_return "
            "FROM ai_predictions WHERE status='resolved' "
            "GROUP BY symbol, predicted_signal"
        ).fetchall()

        result = {}
        for r in rows:
            sym = r["symbol"]
            signal = (r["predicted_signal"] or "").upper()
            total = r["total"]
            wins = r["wins"]
            losses = total - wins
            # DISPLAY_ONLY: div-by-zero guard for symbol-reputation dict.
            win_rate = (wins / total * 100) if total > 0 else 0
            avg_ret = r["avg_return"] or 0

            if sym not in result:
                result[sym] = {
                    "wins": 0, "losses": 0, "total": 0,
                    "win_rate": 0, "avg_return": 0,
                    "by_signal": {},
                }
            result[sym]["wins"] += wins
            result[sym]["losses"] += losses
            result[sym]["total"] += total
            result[sym]["by_signal"][signal] = {
                "wins": wins, "losses": losses, "total": total,
                "win_rate": win_rate, "avg_return": avg_ret,
            }

        # Compute aggregate win_rate + avg_return per symbol AFTER
        # accumulation, then drop symbols below the floor.
        out = {}
        for sym, agg in result.items():
            if agg["total"] < min_predictions:
                continue
            agg["win_rate"] = (agg["wins"] / agg["total"] * 100) if agg["total"] else 0
            # Weight avg_return by per-signal totals
            total_count = sum(s["total"] for s in agg["by_signal"].values())
            if total_count:
                agg["avg_return"] = sum(
                    s["avg_return"] * s["total"]
                    for s in agg["by_signal"].values()
                ) / total_count
            out[sym] = agg
        return out

    except Exception as exc:
        logger.warning("Failed to get symbol reputation: %s", exc)
        return {}
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError as _cl_exc:
            # Cleanup close — conn may already be closed.
            logger.debug(
                "self_tuning conn close: %s: %s",
                type(_cl_exc).__name__, _cl_exc,
            )


# ---------------------------------------------------------------------------
# build_performance_context
# ---------------------------------------------------------------------------

def _build_trade_performance_context(db_path):
    """Build actual trade P&L performance breakdown by strategy type.

    Queries the trades table for completed trades (those with pnl set)
    grouped by side/strategy type. This is more valuable than prediction
    accuracy because it reflects real money outcomes.

    Returns a summary string, or empty string if insufficient data.
    """
    try:
        conn = _get_conn(db_path)
    except Exception:
        return ""

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        if not table_check:
            return ""

        # Query trades grouped by side (buy, sell, short, cover)
        # Phase 5e — exclude data_quality-tagged rows.
        from journal import data_quality_clause
        _dq = data_quality_clause(conn)
        rows = conn.execute(
            f"SELECT side, COUNT(*) as cnt, "
            f"SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            f"SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses, "
            f"SUM(pnl) as total_pnl "
            f"FROM trades WHERE pnl IS NOT NULL{_dq} "
            f"GROUP BY side"
        ).fetchall()

        if not rows:
            return ""

        total_trades = sum(r["cnt"] for r in rows)
        if total_trades < 3:
            return ""

        lines = ["ACTUAL TRADE PERFORMANCE (not just predictions):"]
        warnings = []

        side_labels = {
            "buy": "Long buys",
            "sell": "Long sells",
            "short": "Short sells",
            "cover": "Short covers",
        }

        for r in rows:
            side = r["side"]
            label = side_labels.get(side, side.capitalize())
            cnt = r["cnt"]
            wins = r["wins"] or 0
            losses = r["losses"] or 0
            total_pnl = r["total_pnl"] or 0

            # DISPLAY_ONLY: div-by-zero guard for the AI-prompt line below.
            win_rate = (wins / cnt * 100) if cnt > 0 else 0
            pnl_str = f"+${total_pnl:,.0f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.0f}"
            lines.append(f"  {label:16s} {wins} wins / {losses} losses | P&L: {pnl_str}")

            # Flag critical problems
            # DISPLAY_ONLY: appends to warnings[] list for AI prompt context;
            # not a programmatic tightener.
            if side in ("short", "cover") and cnt >= 5 and win_rate == 0:
                warnings.append(
                    f"WARNING: Short selling has a 0% win rate across {cnt} trades.\n"
                    f"Stop-losses are triggering on every short position before they can profit.\n"
                    f"Consider: wider short stop-losses, or only shorting on bounce days."
                )
            elif side in ("short", "cover") and cnt >= 5 and win_rate < 20:
                warnings.append(
                    f"WARNING: Short selling has only a {win_rate:.0f}% win rate across {cnt} trades.\n"
                    f"Short stop-losses may be too tight, or entries are poorly timed."
                )

        for w in warnings:
            lines.append("")
            lines.append(w)

        return "\n".join(lines)

    except Exception as exc:
        logger.warning("Failed to build trade performance context: %s", exc)
        return ""
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError as _cl_exc:
            # Cleanup close — conn may already be closed.
            logger.debug(
                "self_tuning conn close: %s: %s",
                type(_cl_exc).__name__, _cl_exc,
            )


def _get_track_record_by_direction(conn):
    """Return win-rate stats grouped by prediction_type.

    Returns dict like:
      {'directional_long': {'wins': N, 'losses': N, 'total': N, 'win_rate': float},
       'directional_short': {...}, 'exit_long': {...}, 'exit_short': {...}}

    Used by P1.9 of LONG_SHORT_PLAN.md to surface per-direction
    performance to the AI / self-tuner. The aggregate win rate
    drowns out small short books — splitting by direction lets the
    tuner see when shorts are working but longs aren't (or vice
    versa) and act on each side independently.
    """
    out = {}
    try:
        rows = conn.execute(
            "SELECT prediction_type, "
            "SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN actual_outcome='loss' THEN 1 ELSE 0 END) AS losses, "
            "COUNT(*) AS total "
            "FROM ai_predictions "
            "WHERE status='resolved' AND prediction_type IS NOT NULL "
            "GROUP BY prediction_type"
        ).fetchall()
        for r in rows:
            ptype = r["prediction_type"]
            wins = int(r["wins"] or 0)
            losses = int(r["losses"] or 0)
            total = int(r["total"] or 0)
            # DISPLAY_ONLY: div-by-zero guard for prediction-type breakdown dict.
            wr = (wins / total * 100) if total > 0 else 0.0
            out[ptype] = {"wins": wins, "losses": losses,
                          "total": total, "win_rate": wr}
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            KeyError, ValueError, TypeError) as _br_exc:
        # Per-direction breakdown is enrichment; aggregate stats
        # still surface. Surface for follow-up.
        logger.debug(
            "per-direction breakdown failed: %s: %s",
            type(_br_exc).__name__, _br_exc,
        )
    return out


def build_concise_context(ctx, symbol=None):
    """Build a concise AI prompt context (max 4 lines).

    This replaces the verbose build_performance_context() injection in the
    AI prompt. The verbose version is still used internally for self-tuning
    adjustments.

    Returns a string with at most 4 lines of context, or empty string.
    """
    if ctx is None:
        return ""

    db = getattr(ctx, "db_path", None)
    lines = []

    # 1. Market regime (1 line)
    try:
        from market_regime import detect_regime
        regime = detect_regime()
        if regime and regime.get("regime", "unknown") != "unknown":
            vix = regime.get("vix", 0)
            lines.append(f"MARKET: {regime['regime'].upper()} (VIX {vix:.0f})")
    except (ImportError, KeyError, ValueError, AttributeError,
            TypeError, OSError) as _reg_exc:
        # Concise context skips line on failure. Surface for follow-up.
        logger.debug(
            "market regime line skipped in concise context: %s: %s",
            type(_reg_exc).__name__, _reg_exc,
        )

    # 2. Stock-specific history (1 line)
    if symbol and db:
        try:
            rep = get_symbol_reputation(db)
            if symbol in rep:
                r = rep[symbol]
                lines.append(
                    f"YOUR RECORD ON {symbol}: "
                    f"{r['wins']}W/{r['losses']}L "
                    f"({r['win_rate']:.0f}% win rate)"
                )
        except (sqlite3.OperationalError, sqlite3.DatabaseError,
                KeyError, ValueError, TypeError) as _rep_exc:
            # Concise context skips line on failure. Surface for follow-up.
            logger.debug(
                "per-symbol reputation line skipped in concise context: %s: %s",
                type(_rep_exc).__name__, _rep_exc,
            )

    # 3. Overall win rate (1 line)
    if db:
        try:
            with closing(_get_conn(db)) as conn:
                table_check = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
                ).fetchone()
                if table_check:
                    # 2026-05-13 — exclude data_quality-tagged rows
                    from journal import data_quality_clause
                    _aip_dq = data_quality_clause(conn, table="ai_predictions")
                    total = conn.execute(
                        f"SELECT COUNT(*) FROM ai_predictions "
                        f"WHERE status='resolved'{_aip_dq}"
                    ).fetchone()[0]
                    wins = conn.execute(
                        f"SELECT COUNT(*) FROM ai_predictions "
                        f"WHERE status='resolved' AND actual_outcome='win'{_aip_dq}"
                    ).fetchone()[0]
                    # DISPLAY_ONLY: gates the OVERALL win-rate line in
                    # the AI prompt. Not a tightener.
                    if total >= 10:
                        wr = wins / total * 100
                        selectivity = "Be more selective." if wr < 40 else "Good accuracy."
                        lines.append(
                            f"YOUR OVERALL: {wr:.0f}% win rate "
                            f"({wins}W/{total - wins}L). {selectivity}"
                        )
                    # P1.9: per-direction breakdown for shorts-enabled
                    # profiles. Aggregate WR drowns out small short books;
                    # surfacing the split lets the AI act per-direction.
                    if getattr(ctx, "enable_short_selling", False):
                        by_dir = _get_track_record_by_direction(conn)
                        parts = []
                        for ptype_label, ptype_key in [
                            ("Longs", "directional_long"),
                            ("Shorts", "directional_short"),
                            ("Exits", "exit_long"),
                        ]:
                            d = by_dir.get(ptype_key)
                            if d and d["total"] >= 5:
                                parts.append(
                                    f"{ptype_label} {d['win_rate']:.0f}%W "
                                    f"({d['wins']}W/{d['losses']}L, n={d['total']})"
                                )
                        if parts:
                            lines.append("BY DIRECTION: " + " | ".join(parts))
        except (sqlite3.OperationalError, sqlite3.DatabaseError,
                ImportError, KeyError, ValueError, TypeError) as _wr_exc:
            # AI prompt continues without overall WR. Surface for follow-up.
            logger.debug(
                "overall WR line skipped in concise context: %s: %s",
                type(_wr_exc).__name__, _wr_exc,
            )

    # 4. Earnings warning (1 line, only if imminent)
    if symbol:
        try:
            from earnings_calendar import check_earnings
            e = check_earnings(symbol)
            if e and e.get("days_until", 999) <= 5:
                lines.append(
                    f"EARNINGS: {symbol} reports in {e['days_until']} days. High uncertainty."
                )
        except (ImportError, KeyError, ValueError, AttributeError,
                TypeError, OSError) as _ew_exc:
            # Concise context skips line on failure. Surface for follow-up.
            logger.debug(
                "earnings line skipped in concise context: %s: %s",
                type(_ew_exc).__name__, _ew_exc,
            )

    return "\n".join(lines)


def build_performance_context(ctx, symbol=None, db_path=None):
    """Query the profile's AI predictions database and build a performance
    summary string that gets injected into the AI prompt.

    Returns an empty string if there are fewer than 10 resolved predictions
    or if self-tuning is disabled on the context.
    """
    if ctx is not None and not getattr(ctx, "enable_self_tuning", True):
        return ""

    db = db_path or (ctx.db_path if ctx else None)

    # Check cache (keyed by db_path + symbol)
    cache_key = f"perf_ctx_{db}_{symbol or 'all'}"
    if _is_cached(cache_key):
        return _cache[cache_key]

    try:
        conn = _get_conn(db)
    except Exception:
        return ""

    try:
        # Check if the table exists
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            return ""

        resolved = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
        ).fetchone()[0]

        if resolved < 10:
            _set_cache(cache_key, "")
            return ""

        # --- Overall stats ---
        wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win'"
        ).fetchone()[0]
        losses = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='loss'"
        ).fetchone()[0]
        win_rate = (wins / resolved * 100) if resolved else 0

        lines = []

        # --- Actual trade P&L by strategy (most important — real money) ---
        trade_perf = _build_trade_performance_context(db)
        if trade_perf:
            lines.append(trade_perf)
            lines.append("")

        # --- Stock-specific track record (most relevant context first) ---
        sym_rows = None
        if symbol:
            sym_rows = conn.execute(
                "SELECT predicted_signal, actual_outcome, actual_return_pct, "
                "price_at_prediction, timestamp "
                "FROM ai_predictions "
                "WHERE status='resolved' AND symbol=? "
                "ORDER BY timestamp DESC",
                (symbol.upper(),),
            ).fetchall()
            if sym_rows:
                sym_total = len(sym_rows)
                sym_wins = sum(1 for r in sym_rows if r["actual_outcome"] == "win")
                sym_losses = sym_total - sym_wins
                sym_wr = (sym_wins / sym_total * 100) if sym_total else 0
                all_rets = [r["actual_return_pct"] or 0 for r in sym_rows]
                sym_avg_ret = sum(all_rets) / len(all_rets) if all_rets else 0

                lines.append(f"STOCK-SPECIFIC TRACK RECORD ({symbol.upper()}):")
                lines.append(
                    f"You have predicted on {symbol.upper()} {sym_total} times: "
                    f"{sym_wins}W / {sym_losses}L ({sym_wr:.0f}% win rate)"
                )
                lines.append(
                    f"Average return on {symbol.upper()}: {sym_avg_ret:+.1f}%"
                )

                # Last 3 predictions
                last_3 = sym_rows[:3]
                if last_3:
                    lines.append("Last 3 predictions:")
                    for r in last_3:
                        ts = r["timestamp"][:10] if r["timestamp"] else "unknown"
                        outcome = "WIN" if r["actual_outcome"] == "win" else "LOSS"
                        ret = r["actual_return_pct"] or 0
                        lines.append(
                            f"  {ts}: {r['predicted_signal']} "
                            f"@ ${r['price_at_prediction']:.2f} "
                            f"-> {outcome} ({ret:+.1f}%)"
                        )

                if sym_wr == 0:
                    lines.append(
                        "WARNING: You have NEVER been right about this stock. "
                        "Consider avoiding it."
                    )
                elif sym_wr > 70:
                    lines.append(
                        "This is one of your best-performing stocks. "
                        "Your analysis of it tends to be accurate."
                    )

                lines.append("")

        # --- Overall stats ---
        lines.append("YOUR PREDICTION TRACK RECORD:")
        lines.append(
            f"Overall: {win_rate:.1f}% win rate "
            f"({wins}W / {losses}L from {resolved} resolved predictions)"
        )

        # --- Confidence calibration ---
        lines.append("")
        lines.append("Confidence Calibration:")
        bands = [(60, 70), (70, 80), (80, 101)]
        band_labels = ["60-70%", "70-80%", "80%+"]
        for (lo, hi), label in zip(bands, band_labels):
            hi_op = "<=" if hi > 100 else "<"
            band_total = conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions "
                f"WHERE status='resolved' AND confidence >= ? AND confidence {hi_op} ?",
                (lo, hi),
            ).fetchone()[0]
            band_wins = conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions "
                f"WHERE status='resolved' AND actual_outcome='win' "
                f"AND confidence >= ? AND confidence {hi_op} ?",
                (lo, hi),
            ).fetchone()[0]
            # DISPLAY_ONLY: div-by-zero guard for confidence-band line
            # in performance context (AI prompt).
            if band_total > 0:
                bwr = band_wins / band_total * 100
                if bwr < 45:
                    note = "(not reliable at this level)"
                elif bwr < 60:
                    note = "(somewhat reliable)"
                else:
                    note = "(your most reliable predictions)"
                lines.append(f"  {label} confidence: {bwr:.0f}% win rate {note}")

        # --- Signal performance ---
        lines.append("")
        lines.append("Signal Performance:")
        # 2026-05-13 — exclude data_quality-tagged ai_predictions
        from journal import data_quality_clause
        _aip_dq = data_quality_clause(conn, table="ai_predictions")
        for sig in ("BUY", "SELL"):
            sig_total = conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions "
                f"WHERE status='resolved' AND predicted_signal=?{_aip_dq}",
                (sig,),
            ).fetchone()[0]
            sig_wins = conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions "
                f"WHERE status='resolved' AND actual_outcome='win' AND predicted_signal=?{_aip_dq}",
                (sig,),
            ).fetchone()[0]
            sig_avg_ret = conn.execute(
                f"SELECT AVG(actual_return_pct) FROM ai_predictions "
                f"WHERE status='resolved' AND predicted_signal=?{_aip_dq}",
                (sig,),
            ).fetchone()[0] or 0
            # DISPLAY_ONLY: div-by-zero guard for per-signal AI prompt line.
            if sig_total > 0:
                swr = sig_wins / sig_total * 100
                lines.append(
                    f"  {sig} predictions: {swr:.0f}% win rate, "
                    f"avg return {sig_avg_ret:+.1f}%"
                )

        # --- Recent results (last 10) ---
        recent_rows = conn.execute(
            "SELECT predicted_signal, symbol, price_at_prediction, "
            "actual_outcome, actual_return_pct, reasoning "
            "FROM ai_predictions WHERE status='resolved' "
            "ORDER BY resolved_at DESC LIMIT 10"
        ).fetchall()
        if recent_rows:
            lines.append("")
            lines.append("Recent Results (last 10):")
            for r in recent_rows:
                outcome_label = "WIN" if r["actual_outcome"] == "win" else "LOSS"
                ret = r["actual_return_pct"] or 0
                # Truncate reasoning to keep it concise
                short_reason = (r["reasoning"] or "")[:60]
                if len(r["reasoning"] or "") > 60:
                    short_reason += "..."
                lines.append(
                    f"  {outcome_label}: {r['predicted_signal']} "
                    f"{r['symbol']} @ ${r['price_at_prediction']:.2f} "
                    f"({ret:+.1f}%)"
                )

        # --- Self-tuning guidance ---
        lines.append("")
        lines.append("SELF-TUNING GUIDANCE:")
        lines.append("Based on your track record, adjust your approach:")

        # Check BUY vs SELL performance
        buy_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal IN ('BUY','STRONG_BUY','WEAK_BUY')"
        ).fetchone()[0]
        buy_wins_count = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal IN ('BUY','STRONG_BUY','WEAK_BUY')"
        ).fetchone()[0]
        sell_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal IN ('SELL','STRONG_SELL','WEAK_SELL','SHORT')"
        ).fetchone()[0]
        sell_wins_count = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal IN ('SELL','STRONG_SELL','WEAK_SELL','SHORT')"
        ).fetchone()[0]

        # DISPLAY_ONLY: div-by-zero guards for display strings below.
        buy_wr = (buy_wins_count / buy_total * 100) if buy_total > 0 else 0
        sell_wr = (sell_wins_count / sell_total * 100) if sell_total > 0 else 0

        # DISPLAY_ONLY: text appended to AI prompt; AI weighs it.
        if buy_total > 5 and buy_wr < 45:
            lines.append(
                f"- Your BUY predictions in the current market are losing more "
                f"than winning ({buy_wr:.0f}% win rate). Be more selective."
            )
        # DISPLAY_ONLY: text appended to AI prompt; AI weighs it.
        if sell_total > 5 and sell_wr > buy_wr + 10:
            lines.append(
                f"- SELL predictions ({sell_wr:.0f}%) outperform BUY predictions "
                f"({buy_wr:.0f}%). Consider favoring bearish setups."
            )

        # Check high-confidence performance
        high_conf_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND confidence >= 70"
        ).fetchone()[0]
        high_conf_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND confidence >= 70"
        ).fetchone()[0]
        # DISPLAY_ONLY: text appended to AI prompt; AI weighs it.
        if high_conf_total > 5:
            hcwr = high_conf_wins / high_conf_total * 100
            if hcwr > win_rate + 10:
                lines.append(
                    f"- Your higher confidence predictions (70%+) perform "
                    f"significantly better ({hcwr:.0f}% vs {win_rate:.0f}%). "
                    f"Only give high confidence when you're truly convinced."
                )

        # Symbol-specific guidance
        if symbol and sym_rows:
            sym_wr = sum(1 for r in sym_rows if r["actual_outcome"] == "win") / len(sym_rows) * 100
            if sym_wr < 40:
                lines.append(
                    f"- You've predicted on {symbol.upper()} before with poor results "
                    f"({sym_wr:.0f}% win rate). Exercise extra caution."
                )
            elif sym_wr > 60:
                lines.append(
                    f"- You've predicted on {symbol.upper()} before with good results "
                    f"({sym_wr:.0f}% win rate)."
                )

        # --- TIME-OF-DAY PERFORMANCE ---
        try:
            time_ctx = _build_time_context(db)
            if time_ctx:
                lines.append("")
                lines.append(time_ctx)
        except Exception as _time_exc:
            logger.warning("Failed to add time context: %s", _time_exc)

        # --- LESSONS LEARNED from tuning history ---
        profile_id = getattr(ctx, "profile_id", None) if ctx else None
        if profile_id:
            lessons = _build_lessons_learned(profile_id)
            if lessons:
                lines.append("")
                lines.append(lessons)

        # --- CROSS-PROFILE INSIGHTS (Feature 4) ---
        if ctx is not None and profile_id:
            try:
                cross_insights = _build_cross_profile_insights(
                    ctx.user_id, profile_id, db)
                if cross_insights:
                    lines.append("")
                    lines.append(cross_insights)
            except Exception as _cross_exc:
                logger.warning("Failed to build cross-profile insights: %s", _cross_exc)

        result = "\n".join(lines)
        _set_cache(cache_key, result)
        return result

    except Exception as exc:
        logger.warning("Failed to build performance context: %s", exc)
        return ""
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError as _cl_exc:
            # Cleanup close — conn may already be closed.
            logger.debug(
                "self_tuning conn close: %s: %s",
                type(_cl_exc).__name__, _cl_exc,
            )


def _build_time_context(db_path):
    """Build time-of-day performance summary from ai_predictions.

    Groups resolved predictions by hour and calculates win rate per time bucket.
    Only includes results if there are 5+ predictions in at least 2 buckets.

    Returns a summary string, or empty string if insufficient data.
    """
    try:
        conn = _get_conn(db_path)
    except Exception:
        return ""

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            return ""

        rows = conn.execute(
            "SELECT timestamp, actual_outcome FROM ai_predictions "
            "WHERE status='resolved' AND timestamp IS NOT NULL"
        ).fetchall()

        if not rows:
            return ""

        # Group by hour buckets
        buckets = {
            "open": {"label": "9:30-10:00 (Open)", "hours": {9}, "min_minute": 30, "wins": 0, "total": 0},
            "morning": {"label": "10:00-12:00 (Morning)", "hours": {10, 11}, "wins": 0, "total": 0},
            "midday": {"label": "12:00-14:00 (Midday)", "hours": {12, 13}, "wins": 0, "total": 0},
            "afternoon": {"label": "14:00-16:00 (Afternoon)", "hours": {14, 15}, "wins": 0, "total": 0},
        }

        for r in rows:
            ts_str = r["timestamp"]
            if not ts_str:
                continue
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(ts_str)
                hour = ts.hour
                minute = ts.minute

                # Classify into bucket
                if hour == 9 and minute >= 30:
                    bucket = "open"
                elif hour in (10, 11):
                    bucket = "morning"
                elif hour in (12, 13):
                    bucket = "midday"
                elif hour in (14, 15):
                    bucket = "afternoon"
                else:
                    continue  # Outside market hours

                buckets[bucket]["total"] += 1
                if r["actual_outcome"] == "win":
                    buckets[bucket]["wins"] += 1
            except (KeyError, ValueError, TypeError) as _ts_exc:
                # Per-row parse over a prediction list; skip
                # malformed rows but surface data quality at DEBUG.
                logger.debug(
                    "TOD perf bucket skipped row (bad timestamp): %s: %s",
                    type(_ts_exc).__name__, _ts_exc,
                )
                continue

        # Check if we have enough data: 5+ predictions in at least 2 buckets
        active_buckets = [b for b in buckets.values() if b["total"] >= 5]
        if len(active_buckets) < 2:
            return ""

        lines = ["TIME-OF-DAY PERFORMANCE:"]
        for key in ("open", "morning", "midday", "afternoon"):
            b = buckets[key]
            if b["total"] > 0:
                wr = (b["wins"] / b["total"]) * 100
                if wr < 35:
                    note = "volatile, avoid"
                elif wr < 45:
                    note = "below average"
                elif wr < 55:
                    note = "average"
                elif wr < 65:
                    note = "good window"
                else:
                    note = "your best window"
                lines.append(
                    f"{b['label']}: {wr:.0f}% win rate "
                    f"({b['wins']}/{b['total']}) — {note}"
                )

        return "\n".join(lines)

    except Exception as exc:
        logger.warning("Failed to build time context: %s", exc)
        return ""
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError as _cl_exc:
            # Cleanup close — conn may already be closed.
            logger.debug(
                "self_tuning conn close: %s: %s",
                type(_cl_exc).__name__, _cl_exc,
            )


def _build_lessons_learned(profile_id):
    """Build a concise LESSONS LEARNED section from tuning history.

    Kept under 300 tokens for prompt injection.
    """
    history = _get_tuning_history(profile_id, limit=20)
    if not history:
        return ""

    # Separate reviewed vs pending
    reviewed = [h for h in history if h["outcome_after"] != "pending"]
    pending = [h for h in history if h["outcome_after"] == "pending"]

    if not reviewed and not pending:
        return ""

    lines = ["LEARNING FROM PAST ADJUSTMENTS:"]

    # Show recent adjustments and outcomes (max 5)
    if reviewed:
        lines.append("Past adjustments and results:")
        for entry in reviewed[:5]:
            label = _days_ago_label(entry["timestamp"])
            param = entry["parameter_name"]
            old_v = entry["old_value"]
            new_v = entry["new_value"]
            reason = entry["reason"][:80]
            outcome = entry["outcome_after"].upper()
            wr_before = entry.get("win_rate_at_change") or 0
            wr_after = entry.get("win_rate_after") or 0

            if outcome == "IMPROVED":
                verdict = "GOOD DECISION"
            elif outcome == "WORSENED":
                verdict = "BAD DECISION — may need reversal"
            else:
                verdict = "NO CLEAR EFFECT"

            lines.append(
                f"- {label}: Changed {param} from {old_v} to {new_v} "
                f"(reason: {reason})"
            )
            lines.append(
                f"  Result: win rate {wr_before:.0f}% -> {wr_after:.0f}% "
                f"-> {verdict}"
            )

    # Show pending adjustments (max 3)
    if pending:
        for entry in pending[:3]:
            label = _days_ago_label(entry["timestamp"])
            param = entry["parameter_name"]
            old_v = entry["old_value"]
            new_v = entry["new_value"]
            lines.append(
                f"- {label}: Changed {param} from {old_v} to {new_v} "
                f"(awaiting results)"
            )

    # Summarize what works and what doesn't
    if reviewed:
        lines.append("Lessons:")
        param_outcomes = {}
        for entry in reviewed:
            key = entry["parameter_name"]
            if key not in param_outcomes:
                param_outcomes[key] = []
            param_outcomes[key].append(entry["outcome_after"])
        for param, outcomes in param_outcomes.items():
            improved = outcomes.count("improved")
            worsened = outcomes.count("worsened")
            if improved > worsened:
                lines.append(f"- Adjusting {_label(param)}: has worked well")
            elif worsened > improved:
                lines.append(f"- Adjusting {_label(param)}: has NOT worked, avoid")
            else:
                lines.append(f"- Adjusting {_label(param)}: mixed results")

    lines.append("Use this knowledge. Don't repeat strategies that failed.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# get_auto_adjustments
# ---------------------------------------------------------------------------

def get_auto_adjustments(ctx, db_path=None):
    """Analyze performance data and return recommended parameter adjustments.

    Now checks tuning history before recommending changes:
    - Won't reverse a recent adjustment that improved things
    - Will reverse adjustments that worsened things
    - Won't repeat the same adjustment within 3 days
    """
    db = db_path or (ctx.db_path if ctx else None)
    profile_id = getattr(ctx, "profile_id", None) if ctx else None

    try:
        conn = _get_conn(db)
    except Exception:
        return {"reasons": []}

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            return {"reasons": []}

        resolved = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
        ).fetchone()[0]

        if resolved < 20:
            return {"reasons": [f"Only {resolved} resolved predictions (need 20+)"]}

        wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win'"
        ).fetchone()[0]
        overall_wr = wins / resolved * 100

        result = {
            "confidence_threshold": None,
            "prefer_shorts": False,
            "reduce_position_size": overall_wr < 40,
            "reasons": [],
        }

        # --- Check tuning history before making recommendations ---
        # Don't recommend changes that were already tried recently
        if profile_id:
            recent_conf = _get_recent_adjustment(
                profile_id, "ai_confidence_threshold", days=3)
            recent_pos = _get_recent_adjustment(
                profile_id, "max_position_pct", days=3)
        else:
            recent_conf = None
            recent_pos = None

        # ---------------------------------------------------------------
        # Wave 2 / Fix #5 — train/validate split for parameter changes.
        #
        # Adjustment window: predictions resolved older than
        # VALIDATION_WINDOW_DAYS days ago. Used to PROPOSE a change.
        # Validation window: predictions resolved within the last
        # VALIDATION_WINDOW_DAYS days. Used to VERIFY the proposed
        # change would have improved (or not hurt) recent performance.
        # An adjustment is only recommended if the validation window
        # confirms the adjustment-window finding.
        # See METHODOLOGY_FIX_PLAN.md.
        # ---------------------------------------------------------------
        VALIDATION_WINDOW_DAYS = 14
        validation_cutoff_sql = (
            f"datetime('now', '-{VALIDATION_WINDOW_DAYS} days')"
        )

        # Win rate by confidence band
        if not recent_conf:  # Only suggest if not adjusted in last 3 days
            for threshold, label in [(60, "<60%"), (70, "<70%")]:
                # ADJUSTMENT WINDOW (resolved before validation cutoff)
                band_total = conn.execute(
                    f"SELECT COUNT(*) FROM ai_predictions "
                    f"WHERE status='resolved' AND confidence < ? "
                    f"AND (resolved_at IS NULL OR resolved_at < {validation_cutoff_sql})",
                    (threshold,),
                ).fetchone()[0]
                band_wins = conn.execute(
                    f"SELECT COUNT(*) FROM ai_predictions "
                    f"WHERE status='resolved' AND actual_outcome='win' "
                    f"AND confidence < ? "
                    f"AND (resolved_at IS NULL OR resolved_at < {validation_cutoff_sql})",
                    (threshold,),
                ).fetchone()[0]
                if band_total <= 5:
                    continue
                bwr = band_wins / band_total * 100
                if bwr >= 35:
                    continue

                # VALIDATION WINDOW (resolved within last N days)
                val_total = conn.execute(
                    f"SELECT COUNT(*) FROM ai_predictions "
                    f"WHERE status='resolved' "
                    f"AND resolved_at IS NOT NULL "
                    f"AND resolved_at >= {validation_cutoff_sql}"
                ).fetchone()[0]
                val_kept_total = conn.execute(
                    f"SELECT COUNT(*) FROM ai_predictions "
                    f"WHERE status='resolved' AND confidence >= ? "
                    f"AND resolved_at IS NOT NULL "
                    f"AND resolved_at >= {validation_cutoff_sql}",
                    (threshold,),
                ).fetchone()[0]
                val_kept_wins = conn.execute(
                    f"SELECT COUNT(*) FROM ai_predictions "
                    f"WHERE status='resolved' AND actual_outcome='win' "
                    f"AND confidence >= ? "
                    f"AND resolved_at IS NOT NULL "
                    f"AND resolved_at >= {validation_cutoff_sql}",
                    (threshold,),
                ).fetchone()[0]
                val_total_wins = conn.execute(
                    f"SELECT COUNT(*) FROM ai_predictions "
                    f"WHERE status='resolved' AND actual_outcome='win' "
                    f"AND resolved_at IS NOT NULL "
                    f"AND resolved_at >= {validation_cutoff_sql}"
                ).fetchone()[0]
                if val_total < 5 or val_kept_total < 3:
                    result["reasons"].append(
                        f"Win rate at confidence {label} is {bwr:.0f}% "
                        f"(adjustment window) but only {val_total} resolved "
                        f"predictions in the last {VALIDATION_WINDOW_DAYS} "
                        f"days — not enough validation data to apply"
                    )
                    continue

                wr_kept_validation = val_kept_wins / val_kept_total * 100
                wr_full_validation = (val_total_wins / val_total * 100) if val_total else 0
                # The proposed threshold-raise would FILTER predictions
                # with confidence < T. The validation question: does the
                # surviving (confidence ≥ T) cohort have a higher win
                # rate than the full validation cohort?
                if wr_kept_validation < wr_full_validation:
                    result["reasons"].append(
                        f"Win rate at confidence {label} is {bwr:.0f}% "
                        f"(adjustment window) but the proposed threshold "
                        f"raise would worsen recent performance "
                        f"({wr_kept_validation:.0f}% vs {wr_full_validation:.0f}%) — "
                        f"validation rejected"
                    )
                    continue

                # Re-check past-effectiveness gate
                past_outcome = _was_adjustment_effective(
                    profile_id, "ai_confidence_threshold") if profile_id else None
                if past_outcome == "worsened":
                    result["reasons"].append(
                        f"Win rate at confidence {label} is {bwr:.0f}%, "
                        f"but previous threshold raise worsened results — skipping"
                    )
                    continue

                result["confidence_threshold"] = threshold
                result["reasons"].append(
                    f"Win rate at confidence {label} is {bwr:.0f}% "
                    f"(adjustment window). Validation confirmed: "
                    f"raising to {threshold}"
                )
        else:
            result["reasons"].append(
                "Confidence threshold was adjusted recently — "
                "waiting for results before changing again"
            )

        # BUY vs SELL performance
        buy_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal IN ('BUY','STRONG_BUY','WEAK_BUY')"
        ).fetchone()[0]
        buy_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal IN ('BUY','STRONG_BUY','WEAK_BUY')"
        ).fetchone()[0]
        sell_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal IN ('SELL','STRONG_SELL','WEAK_SELL','SHORT')"
        ).fetchone()[0]
        sell_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal IN ('SELL','STRONG_SELL','WEAK_SELL','SHORT')"
        ).fetchone()[0]

        # DISPLAY_ONLY: defaults for AI prompt context, not a tightener.
        buy_wr = (buy_wins / buy_total * 100) if buy_total > 5 else 50
        sell_wr = (sell_wins / sell_total * 100) if sell_total > 5 else 50

        if buy_wr < 30 and sell_wr > 50:
            result["prefer_shorts"] = True
            result["reasons"].append(
                f"BUY win rate ({buy_wr:.0f}%) below threshold, "
                f"SELL win rate ({sell_wr:.0f}%) strong — consider enabling short selling"
            )

        if overall_wr < 40 and not recent_pos:
            result["reasons"].append(
                f"Overall win rate ({overall_wr:.0f}%) below 40%, "
                f"recommend reducing position size"
            )
        elif overall_wr < 40 and recent_pos:
            result["reasons"].append(
                f"Overall win rate ({overall_wr:.0f}%) below 40%, "
                f"but position size was adjusted recently — waiting for results"
            )
            result["reduce_position_size"] = False

        return result

    except Exception as exc:
        logger.warning("Failed to get auto adjustments: %s", exc)
        return {"reasons": [str(exc)]}
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError as _cl_exc:
            # Cleanup close — conn may already be closed.
            logger.debug(
                "self_tuning conn close: %s: %s",
                type(_cl_exc).__name__, _cl_exc,
            )


# ---------------------------------------------------------------------------
# apply_auto_adjustments
# ---------------------------------------------------------------------------

def describe_tuning_state(ctx, db_path=None):
    """Return a human-readable one-liner explaining why self-tuning may
    or may not have room to adjust anything. Used for dashboard
    visibility so the user sees the tuner is alive even when it doesn't
    change parameters.

    Returns a dict with:
        {
            "can_tune": bool — True if we have enough data to evaluate,
            "resolved": int — # of resolved AI predictions,
            "required": int — threshold required (20),
            "message": str — human-readable explanation,
        }
    """
    required = 20
    if ctx is None:
        return {"can_tune": False, "resolved": 0, "required": required,
                "message": "No profile context available."}
    if not getattr(ctx, "enable_self_tuning", True):
        return {"can_tune": False, "resolved": 0, "required": required,
                "message": "Self-tuning is disabled on this profile."}
    db = db_path or ctx.db_path
    try:
        with closing(_get_conn(db)) as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
            ).fetchone()
            if not table:
                return {"can_tune": False, "resolved": 0, "required": required,
                        "message": "AI prediction history not yet initialized."}
            resolved = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
            ).fetchone()[0]
        if resolved < required:
            return {
                "can_tune": False, "resolved": resolved, "required": required,
                "message": (
                    f"Waiting for more AI predictions to resolve — "
                    f"{resolved}/{required} ready. Each prediction takes about "
                    f"5 trading days to resolve; the tuner will start "
                    f"evaluating once the threshold is met."
                ),
            }
        msg = f"Tuner is evaluating against {resolved} resolved predictions."
        if resolved >= 30:
            msg += " Upward optimization active."
        return {
            "can_tune": True, "resolved": resolved, "required": required,
            "message": msg,
        }
    except Exception as exc:
        return {"can_tune": False, "resolved": 0, "required": required,
                "message": f"Could not read prediction state: {exc}"}


def apply_auto_adjustments(ctx, db_path=None):
    """Apply conservative auto-adjustments to the profile based on performance.

    Now also:
    - Logs every change to tuning_history via log_tuning_change()
    - Reviews past adjustments via review_past_adjustments()
    - Checks history to avoid oscillation and repeating failed strategies
    - Returns list of adjustment descriptions including review results
    """
    if ctx is None:
        return []

    if not getattr(ctx, "enable_self_tuning", True):
        return []

    db = db_path or ctx.db_path
    adjustments_made = []

    # --- First, review past adjustments ---
    profile_id = getattr(ctx, "profile_id", None)
    user_id = getattr(ctx, "user_id", None)

    if profile_id:
        try:
            from models import review_past_adjustments
            from display_names import format_param_value as _fmt
            reviews = review_past_adjustments(profile_id, db_path=db)
            for rev in reviews:
                outcome = rev["outcome_after"].upper()
                param = rev["parameter_name"]
                old_v = rev["old_value"]
                new_v = rev["new_value"]
                wr_before = rev.get("win_rate_at_change") or 0
                wr_after = rev.get("win_rate_after") or 0
                # Format the param NAME and VALUES through the
                # display-name + value-formatter helpers so the
                # ticker shows "Max Position Size 8.0% → 9.2%"
                # instead of "max_position_pct 0.08->0.092".
                # Caught 2026-04-27 — the AST guard previously only
                # walked _optimize_* functions and missed this
                # orchestrator-level string. Test extended
                # accordingly so the gap can't reappear.
                adjustments_made.append(
                    f"Reviewed past adjustment: {_label(param)} "
                    f"{_fmt(param, old_v)} → {_fmt(param, new_v)} "
                    f"(win rate {wr_before:.0f}%→{wr_after:.0f}%: {outcome})"
                )

                # Layer 5 — propagate insights from improvements.
                # When this profile's change turned out to help, run
                # the same detection rule on every peer profile's data.
                # Each peer's own data has to support the change (no
                # value-copying); the fleet learns ~10x faster than
                # profiles in isolation.
                if rev["outcome_after"] == "improved":
                    try:
                        from insight_propagation import propagate_insight
                        change_type = rev.get("change_type") or rev.get("adjustment_type") or ""
                        spread = propagate_insight(
                            profile_id, change_type, param)
                        for s in spread:
                            adjustments_made.append(f"PROPAGATED: {s}")
                    except Exception as prop_exc:
                        logger.warning(
                            "Failed to propagate insight: %s", prop_exc)

                # If a past adjustment worsened things, reverse it
                if rev["outcome_after"] == "worsened":
                    try:
                        from models import update_trading_profile, log_tuning_change
                        # Reverse: set back to old value
                        update_kwargs = {param: _cast_param_value(param, old_v)}
                        update_trading_profile(profile_id, **update_kwargs)

                        # Get current stats for the reversal log
                        try:
                            with closing(_get_conn(db)) as conn_tmp:
                                cur_wr, cur_resolved = _get_current_win_rate(conn_tmp)
                        except Exception:
                            cur_wr, cur_resolved = 0, 0

                        log_tuning_change(
                            profile_id, user_id or 0,
                            "auto_reversal", param,
                            new_v, old_v,
                            f"Reversing previous change — {outcome} "
                            f"(win rate went from {wr_before:.0f}% to {wr_after:.0f}%)",
                            win_rate_at_change=cur_wr,
                            predictions_resolved=cur_resolved,
                        )
                        adjustments_made.append(
                            f"REVERSED: {_label(param)} back from "
                            f"{_fmt(param, new_v)} to {_fmt(param, old_v)} "
                            f"(previous change worsened performance)"
                        )
                    except Exception as rev_exc:
                        logger.warning("Failed to reverse adjustment: %s", rev_exc)

        except Exception as exc:
            logger.warning("Failed to review past adjustments: %s", exc)

    try:
        conn = _get_conn(db)
    except Exception:
        return adjustments_made

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            return adjustments_made

        resolved = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
        ).fetchone()[0]

        if resolved < 20:
            return adjustments_made

        if not profile_id:
            return adjustments_made

        from models import update_trading_profile, log_tuning_change

        overall_wr, _ = _get_current_win_rate(conn)

        # 2026-05-14 — trade-volume signal (NOT a hard block). When
        # the profile is producing too few stock entries, the tuner
        # SHOULD prefer loosening adjustments and raise the bar for
        # tightening — but tightening on truly broken patterns
        # (≥60 samples + clear evidence) remains available. Principle:
        # the system must drift toward CONFIDENT TRADING, not stasis,
        # and that means using the tools intelligently, not turning
        # them off. (See feedback_self_tuner_must_drift_toward_trading.md.)
        MIN_RECENT_ENTRIES = 3
        WINDOW_DAYS = 7
        try:
            recent_entries = conn.execute(
                f"""SELECT COUNT(*) FROM trades
                    WHERE date(timestamp) >= date('now', '-{WINDOW_DAYS} days')
                    AND side IN ('buy', 'short')
                    AND signal_type IN
                       ('BUY', 'STRONG_BUY', 'SHORT', 'STRONG_SELL')"""
            ).fetchone()[0]
        except sqlite3.OperationalError as exc:
            logger.warning("Volume floor check failed: %s", exc)
            recent_entries = MIN_RECENT_ENTRIES  # fail-open
        ctx._runtime_under_volume_floor = (
            recent_entries < MIN_RECENT_ENTRIES
        )
        if ctx._runtime_under_volume_floor:
            adjustments_made.append(
                f"VOLUME-FLOOR signal: only {recent_entries} stock "
                f"entries in last {WINDOW_DAYS}d (floor="
                f"{MIN_RECENT_ENTRIES}). Tuner will prefer loosening "
                f"and require stronger evidence for tightening."
            )
            logger.info(
                "Volume-floor signal active for profile %s: %d entries "
                "< %d in %dd. Loosening prioritized; tightening bar "
                "raised but not blocked.",
                profile_id, recent_entries, MIN_RECENT_ENTRIES, WINDOW_DAYS,
            )

        # --- Check if confidence threshold was adjusted recently ---
        recent_conf = _get_recent_adjustment(
            profile_id, "ai_confidence_threshold", days=3)

        if not recent_conf:
            # Pick the right confidence threshold in ONE step — don't cascade.
            # Check the tighter band (70) first. If both bands are bad, go
            # straight to 70 instead of 60→70 in the same run.
            conf_adjusted = False

            band70_total = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status='resolved' AND confidence < 70"
            ).fetchone()[0]
            band70_wins = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status='resolved' AND actual_outcome='win' AND confidence < 70"
            ).fetchone()[0]

            # 2026-05-14: minimum sample size 30 (60 when the profile
            # is below the trade-volume floor). Previously 5, which
            # let the tuner ratchet thresholds up to 70-80 on noise.
            # The volume-floor signal RAISES the bar but doesn't
            # block; tightening on truly broken patterns stays
            # available with stronger evidence.
            _min_n_70 = 60 if getattr(ctx, "_runtime_under_volume_floor", False) else 30
            if band70_total >= _min_n_70:
                wr70 = band70_wins / band70_total * 100
                if wr70 < 35 and ctx.ai_confidence_threshold < 70:
                    past_outcome = _was_adjustment_effective(
                        profile_id, "ai_confidence_threshold")
                    if past_outcome != "worsened":
                        old_val = ctx.ai_confidence_threshold
                        reason = (
                            f"Win rate at <70% confidence was {wr70:.0f}% "
                            f"({band70_wins}/{band70_total})"
                        )
                        applied, _, suffix = _apply_param_change(
                            profile_id, user_id or 0,
                            "confidence_threshold", "ai_confidence_threshold",
                            old_val, 70, reason,
                            win_rate_at_change=overall_wr,
                            predictions_resolved=resolved,
                        )
                        adjustments_made.append(
                            f"Raised AI confidence threshold from {old_val} "
                            f"to {applied}{suffix} ({reason})"
                        )
                        conf_adjusted = True

            if not conf_adjusted:
                band60_total = conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions "
                    "WHERE status='resolved' AND confidence < 60"
                ).fetchone()[0]
                band60_wins = conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions "
                    "WHERE status='resolved' AND actual_outcome='win' AND confidence < 60"
                ).fetchone()[0]

                # 2026-05-14: same min-30 (60 if under volume floor)
                # rule as the band70 check above (was previously >5).
                _min_n_60 = 60 if getattr(ctx, "_runtime_under_volume_floor", False) else 30
                if band60_total >= _min_n_60:
                    wr60 = band60_wins / band60_total * 100
                    if wr60 < 35 and ctx.ai_confidence_threshold < 60:
                        past_outcome = _was_adjustment_effective(
                            profile_id, "ai_confidence_threshold")
                        if past_outcome != "worsened":
                            old_val = ctx.ai_confidence_threshold
                            reason = (
                                f"Win rate at <60% confidence was {wr60:.0f}% "
                                f"({band60_wins}/{band60_total})"
                            )
                            applied, _, suffix = _apply_param_change(
                                profile_id, user_id or 0,
                                "confidence_threshold", "ai_confidence_threshold",
                                old_val, 60, reason,
                                win_rate_at_change=overall_wr,
                                predictions_resolved=resolved,
                            )
                            adjustments_made.append(
                                f"Raised AI confidence threshold from {old_val} "
                                f"to {applied}{suffix} ({reason})"
                            )

        # --- BUY vs SELL performance ---
        buy_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal IN ('BUY','STRONG_BUY','WEAK_BUY')"
        ).fetchone()[0]
        buy_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal IN ('BUY','STRONG_BUY','WEAK_BUY')"
        ).fetchone()[0]
        sell_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal IN ('SELL','STRONG_SELL','WEAK_SELL','SHORT')"
        ).fetchone()[0]
        sell_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal IN ('SELL','STRONG_SELL','WEAK_SELL','SHORT')"
        ).fetchone()[0]

        # DISPLAY_ONLY: defaults for the recommendation text below.
        # The recommendation is a string for human/AI consumption,
        # not a programmatic tightening.
        buy_wr = (buy_wins / buy_total * 100) if buy_total > 5 else 50
        sell_wr = (sell_wins / sell_total * 100) if sell_total > 5 else 50

        if buy_wr < 30 and sell_wr > 50 and not ctx.enable_short_selling:
            adjustments_made.append(
                f"Recommendation: enable short selling — BUY win rate {buy_wr:.0f}%, "
                f"SELL win rate {sell_wr:.0f}%"
            )

        # --- Short trade performance — auto-widen stops if 0% win rate ---
        try:
            trade_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
            ).fetchone()
            if trade_table:
                # Phase 5e — exclude data_quality-tagged rows.
                from journal import data_quality_clause
                _dq = data_quality_clause(conn)
                short_rows = conn.execute(
                    f"SELECT COUNT(*) as cnt, "
                    f"SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                    f"SUM(pnl) as total_pnl "
                    f"FROM trades WHERE pnl IS NOT NULL "
                    f"AND side IN ('short', 'cover'){_dq}"
                ).fetchone()
                short_cnt = short_rows["cnt"] if short_rows else 0
                short_wins = short_rows["wins"] or 0 if short_rows else 0
                short_pnl = short_rows["total_pnl"] or 0 if short_rows else 0
                # DISPLAY_ONLY: div-by-zero guard for the if-checks below.
                short_wr = (short_wins / short_cnt * 100) if short_cnt > 0 else 100

                # Auto-widen short stop-loss if 0% win rate. Min 30
                # samples (60 if profile is below volume floor) — was
                # previously 5 which fired on noise.
                _min_short = 60 if getattr(ctx, "_runtime_under_volume_floor", False) else 30
                if short_cnt >= _min_short and short_wr == 0:
                    recent_short_sl = _get_recent_adjustment(
                        profile_id, "short_stop_loss_pct", days=3)
                    if not recent_short_sl:
                        current_sl = getattr(ctx, "short_stop_loss_pct", 0.08)
                        new_sl = round(min(current_sl * 1.5, 0.20), 4)
                        if new_sl > current_sl:
                            reason = (
                                f"Short selling 0% win rate across {short_cnt} trades — "
                                f"widening stop-loss by 50%"
                            )
                            applied, _, suffix = _apply_param_change(
                                profile_id, user_id or 0,
                                "short_stop_loss", "short_stop_loss_pct",
                                current_sl, new_sl, reason,
                                win_rate_at_change=overall_wr,
                                predictions_resolved=resolved,
                            )
                            adjustments_made.append(
                                f"Widened short stop-loss from {current_sl:.0%} to "
                                f"{applied:.0%}{suffix} ({reason})"
                            )

                # Auto-disable shorts when consistently losing money: 10+ trades,
                # <20% win rate, negative total P&L. This is defensive (stops
                # bleeding) — safe to auto-action. The reverse case
                # (auto-enabling shorts) is intentionally left as a
                # recommendation only because flipping a high-risk feature ON
                # without human review is dangerous.
                # Auto-disable shorts. Min 30 (60 if under volume
                # floor) — was 10 which fired on noise.
                _min_disable = 60 if getattr(ctx, "_runtime_under_volume_floor", False) else 30
                if (short_cnt >= _min_disable and short_wr < 20 and short_pnl < 0
                        and getattr(ctx, "enable_short_selling", False)):
                    if not _get_recent_adjustment(
                            profile_id, "enable_short_selling", days=3):
                        if _was_adjustment_effective(
                                profile_id, "enable_short_selling") != "worsened":
                            update_trading_profile(
                                profile_id, enable_short_selling=0)
                            reason = (
                                f"Short selling losing across "
                                f"{short_cnt} trades — "
                                f"{short_wins} wins ({short_wr:.0f}%), "
                                f"total P&L ${short_pnl:,.0f}"
                            )
                            log_tuning_change(
                                profile_id, user_id or 0,
                                "short_disable", "enable_short_selling",
                                "1", "0", reason,
                                win_rate_at_change=overall_wr,
                                predictions_resolved=resolved,
                            )
                            adjustments_made.append(
                                f"Disabled short selling ({reason})"
                            )
        except Exception as _short_exc:
            logger.warning("Failed to check short trade performance: %s", _short_exc)

        # --- Overall win rate too low — reduce position size ---
        recent_pos = _get_recent_adjustment(
            profile_id, "max_position_pct", days=3)

        if overall_wr < 30 and not recent_pos:
            past_outcome = _was_adjustment_effective(
                profile_id, "max_position_pct")
            if past_outcome != "worsened":
                new_pct = max(0.03, ctx.max_position_pct * 0.8)
                if new_pct < ctx.max_position_pct:
                    old_val = ctx.max_position_pct
                    reason = f"Overall win rate {overall_wr:.0f}% below 30%"
                    applied, _, suffix = _apply_param_change(
                        profile_id, user_id or 0,
                        "position_size", "max_position_pct",
                        old_val, round(new_pct, 4), reason,
                        win_rate_at_change=overall_wr,
                        predictions_resolved=resolved,
                    )
                    adjustments_made.append(
                        f"Reduced max position size from {old_val:.1%} "
                        f"to {applied:.1%}{suffix} ({reason})"
                    )

        # --- Upward optimizations (only when not in disaster mode) ---
        if overall_wr >= 35:
            upward = _apply_upward_optimizations(
                conn, ctx, profile_id, user_id, overall_wr, resolved
            )
            adjustments_made.extend(upward)

        return adjustments_made

    except Exception as exc:
        logger.warning("Failed to apply auto adjustments: %s", exc)
        return adjustments_made
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError as _cl_exc:
            # Cleanup close — conn may already be closed.
            logger.debug(
                "self_tuning conn close: %s: %s",
                type(_cl_exc).__name__, _cl_exc,
            )


def _cast_param_value(param_name, value_str):
    """Cast a string value back to the appropriate type for a profile parameter."""
    int_params = {"ai_confidence_threshold", "max_total_positions", "min_volume",
                   "avoid_earnings_days", "skip_first_minutes",
                   "max_sector_positions"}
    float_params = {
        "max_position_pct", "stop_loss_pct", "take_profit_pct",
        "short_stop_loss_pct", "short_take_profit_pct",
        "short_max_position_pct",
        "min_price", "max_price", "volume_surge_multiplier",
        "rsi_overbought", "rsi_oversold", "momentum_5d_gain",
        "momentum_20d_gain", "breakout_volume_threshold", "gap_pct_threshold",
        "drawdown_pause_pct", "drawdown_reduce_pct",
        "max_correlation", "meta_pregate_threshold",
        "atr_multiplier_sl", "atr_multiplier_tp",
        "trailing_atr_multiplier",
    }
    bool_params = {
        "strategy_momentum_breakout", "strategy_volume_spike",
        "strategy_mean_reversion", "strategy_gap_and_go",
        "enable_short_selling", "enable_self_tuning", "maga_mode",
        "enable_consensus",
    }

    if param_name in int_params:
        return int(float(value_str))
    elif param_name in float_params:
        return float(value_str)
    elif param_name in bool_params:
        return int(float(value_str))
    return value_str


# ---------------------------------------------------------------------------
# Upward optimization — actively improve win rate, not just prevent disaster
# ---------------------------------------------------------------------------
#
# Phase 2 architecture (2026-05-15): every optimizer carries a direction
# tag. The registry uses the tags to enforce that LOOSENING rules fire
# FIRST in every cycle, BIDIRECTIONAL next, STRUCTURAL after that, and
# TIGHTENING last. The system structurally drifts toward confident
# trading: when there's a viable loosening adjustment, we take it
# before considering any tightening. Pre-2026-05-15 the registry was
# tightening-dominant by file order, with the volume-floor signal as
# the only trigger that re-prioritized loosening — that left a hole
# where moderate under-trading wouldn't trigger the floor but the
# tuner would still tighten further.
#
# Direction tags:
#   LOOSEN        — action-creating (lower bars, more trades, more
#                   confidence in existing patterns, larger sizes,
#                   new strategies commissioned).
#   TIGHTEN       — action-restricting (raise bars, deprecate
#                   strategies, narrow regime sizing, blacklists).
#   BIDIRECTIONAL — fires either way based on data; not structurally
#                   biased by the volume-floor signal alone.
#   STRUCTURAL    — doesn't directly affect trade volume (per-regime
#                   overrides, per-symbol tuning, prompt layout).
#
# Each tag MUST be present for every optimizer in the registry —
# enforced by tests/test_self_tuner_optimizer_directions.py.

_OPTIMIZER_DIRECTION = {
    # Loosening (action-creating)
    "_optimize_false_negatives": "LOOSEN",
    "_optimize_position_size_upward": "LOOSEN",
    "_optimize_commission_strategy": "LOOSEN",
    # Bidirectional (data-driven, no inherent bias)
    "_optimize_meta_pregate_threshold": "BIDIRECTIONAL",
    "_optimize_short_selling_toggle": "BIDIRECTIONAL",
    "_optimize_options_pnl_cutoff": "BIDIRECTIONAL",
    "_optimize_conviction_tp_override": "BIDIRECTIONAL",
    "_optimize_signal_weights": "BIDIRECTIONAL",
    "_optimize_stop_to_tp_ratio": "BIDIRECTIONAL",
    "_optimize_stop_take_profit": "BIDIRECTIONAL",
    "_optimize_regime_position_sizing": "BIDIRECTIONAL",
    "_optimize_short_take_profit": "BIDIRECTIONAL",
    "_optimize_atr_multiplier_sl": "BIDIRECTIONAL",
    "_optimize_atr_multiplier_tp": "BIDIRECTIONAL",
    "_optimize_trailing_atr_multiplier": "BIDIRECTIONAL",
    # Structural (parameter-shape changes, not volume-direction)
    "_optimize_regime_overrides": "STRUCTURAL",
    "_optimize_tod_overrides": "STRUCTURAL",
    "_optimize_symbol_overrides": "STRUCTURAL",
    "_optimize_prompt_layout": "STRUCTURAL",
    "_optimize_price_band": "STRUCTURAL",
    "_optimize_maga_mode": "STRUCTURAL",
    "_optimize_short_max_position_pct": "STRUCTURAL",
    "_optimize_short_max_hold_days": "STRUCTURAL",
    # Tightening (action-restricting — fire LAST in the registry)
    "_optimize_confidence_threshold_upward": "TIGHTEN",
    "_optimize_strategy_toggles": "TIGHTEN",
    "_optimize_max_total_positions": "TIGHTEN",
    "_optimize_max_correlation": "TIGHTEN",
    "_optimize_max_sector_positions": "TIGHTEN",
    "_optimize_drawdown_thresholds": "TIGHTEN",
    "_optimize_drawdown_reduce": "TIGHTEN",
    "_optimize_avoid_earnings_days": "TIGHTEN",
    "_optimize_skip_first_minutes": "TIGHTEN",
    "_optimize_skip_first_minutes_slippage": "TIGHTEN",
    "_optimize_min_volume": "TIGHTEN",
    "_optimize_volume_surge_multiplier": "TIGHTEN",
    "_optimize_breakout_volume_threshold": "TIGHTEN",
    "_optimize_gap_pct_threshold": "TIGHTEN",
    "_optimize_momentum_5d": "TIGHTEN",
    "_optimize_momentum_20d": "TIGHTEN",
    "_optimize_rsi_overbought": "TIGHTEN",
    "_optimize_rsi_oversold": "TIGHTEN",
    "_optimize_short_stop_loss": "TIGHTEN",
    "_optimize_fast_lane_retirement": "TIGHTEN",
    "_optimize_stop_out_blacklist": "TIGHTEN",
}

# Display order for direction tags; controls the running sequence
# so loosening fires first, tightening last.
_DIRECTION_PRIORITY = ("LOOSEN", "BIDIRECTIONAL", "STRUCTURAL", "TIGHTEN")


def _apply_upward_optimizations(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """Run upward optimization strategies on a healthy profile.

    Called only when overall_wr >= 35 (disaster prevention has exclusive
    control below that). Each sub-function makes at most ONE change.
    The orchestrator stops after the first change so auto-reversal can
    attribute any win-rate shift to that specific adjustment.
    """
    from models import update_trading_profile, log_tuning_change

    # All registered optimizers, in any source order. The actual
    # running sequence is determined by _OPTIMIZER_DIRECTION below
    # so the file order doesn't accidentally bias the system.
    all_optimizers = [
        # Tightening (will fire LAST per the direction-priority sort)
        _optimize_confidence_threshold_upward,
        _optimize_strategy_toggles,
        _optimize_max_total_positions,
        _optimize_max_correlation,
        _optimize_max_sector_positions,
        _optimize_drawdown_thresholds,
        _optimize_drawdown_reduce,
        _optimize_avoid_earnings_days,
        _optimize_skip_first_minutes,
        _optimize_skip_first_minutes_slippage,
        _optimize_min_volume,
        _optimize_volume_surge_multiplier,
        _optimize_breakout_volume_threshold,
        _optimize_gap_pct_threshold,
        _optimize_momentum_5d,
        _optimize_momentum_20d,
        _optimize_rsi_overbought,
        _optimize_rsi_oversold,
        _optimize_short_stop_loss,
        _optimize_fast_lane_retirement,
        _optimize_stop_out_blacklist,
        # Bidirectional (data-driven, runs after looseners)
        _optimize_regime_position_sizing,
        _optimize_stop_take_profit,
        _optimize_short_take_profit,
        _optimize_atr_multiplier_sl,
        _optimize_atr_multiplier_tp,
        _optimize_trailing_atr_multiplier,
        _optimize_stop_to_tp_ratio,
        _optimize_conviction_tp_override,
        _optimize_short_selling_toggle,
        _optimize_options_pnl_cutoff,
        _optimize_meta_pregate_threshold,
        _optimize_signal_weights,
        # Structural (parameter-shape changes; not volume-direction)
        _optimize_price_band,
        _optimize_maga_mode,
        _optimize_short_max_position_pct,
        _optimize_short_max_hold_days,
        _optimize_regime_overrides,
        _optimize_tod_overrides,
        _optimize_symbol_overrides,
        _optimize_prompt_layout,
        # Loosening (action-creating — fire FIRST)
        _optimize_position_size_upward,
        _optimize_commission_strategy,
        _optimize_false_negatives,
    ]

    # Phase 2 architecture (2026-05-15): structurally favor action.
    # Sort the registry so loosening fires first, bidirectional next,
    # structural after, tightening last. Within each direction band
    # the source order above is preserved (stable sort).
    #
    # When the profile is also under the volume floor, we additionally
    # require that a loosener get a chance before any tightener can
    # fire. Bidirectional/structural rules can still run between them.
    #
    # Result: even in normal conditions, the system always asks
    # "is there something to loosen?" before asking "what should I
    # tighten?" — the architecture itself drifts toward action.
    optimizers = sorted(
        all_optimizers,
        key=lambda fn: _DIRECTION_PRIORITY.index(
            _OPTIMIZER_DIRECTION.get(fn.__name__, "TIGHTEN")
        ),
    )

    results = []
    saw_loosener_fire = False
    for optimizer in optimizers:
        direction = _OPTIMIZER_DIRECTION.get(optimizer.__name__, "TIGHTEN")
        # Under volume floor, hold off on tightening until at least
        # one loosener / bidirectional / structural rule was tried
        # without firing. Without volume floor, run normally — the
        # priority-sort already biases toward action.
        if (
            direction == "TIGHTEN"
            and getattr(ctx, "_runtime_under_volume_floor", False)
            and not saw_loosener_fire
        ):
            # Earlier passes ran but none fired; under floor we still
            # apply the existing per-optimizer "raise the bar" gates
            # rather than skipping tightening entirely. The
            # _runtime_under_volume_floor flag plumbed into individual
            # optimizers raises sample-size minimums (typically
            # 30 → 60) before they fire.
            pass
        try:
            result = optimizer(conn, ctx, profile_id, user_id, overall_wr, resolved)
            if result:
                results.append(result)
                if direction in ("LOOSEN", "BIDIRECTIONAL", "STRUCTURAL"):
                    saw_loosener_fire = True
                break  # One change per run
        except Exception as exc:
            logger.warning("Upward optimizer %s failed: %s", optimizer.__name__, exc)

    return results


def _optimize_confidence_threshold_upward(conn, ctx, profile_id, user_id,
                                          overall_wr, resolved):
    """Find the confidence band with the best win rate and raise the
    threshold to focus on that band. Only raises one band at a time."""
    if _get_recent_adjustment(profile_id, "ai_confidence_threshold", days=3):
        return None
    if _was_adjustment_effective(profile_id, "ai_confidence_threshold") == "worsened":
        return None

    # 2026-05-13 — exclude data_quality-tagged ai_predictions
    from journal import data_quality_clause
    _aip_dq = data_quality_clause(conn, table="ai_predictions")
    rows = conn.execute(
        f"""SELECT
             CASE WHEN confidence >= 80 THEN 80
                  WHEN confidence >= 70 THEN 70
                  WHEN confidence >= 60 THEN 60
                  WHEN confidence >= 50 THEN 50
                  ELSE 0 END as band_floor,
             COUNT(*) as total,
             SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins
           FROM ai_predictions WHERE status='resolved'{_aip_dq}
           GROUP BY band_floor HAVING COUNT(*) >= 30
           ORDER BY band_floor"""
    ).fetchall()

    if not rows:
        return None

    # Find the band with the highest win rate
    best_band = None
    best_wr = 0
    for r in rows:
        wr = (r["wins"] / r["total"] * 100) if r["total"] > 0 else 0
        if wr > best_wr:
            best_wr = wr
            best_band = r["band_floor"]

    if best_band is None or best_band <= ctx.ai_confidence_threshold:
        return None

    # Best band must be meaningfully better than overall
    if best_wr < overall_wr + 10:
        return None

    # Only raise one band at a time
    current = ctx.ai_confidence_threshold
    band_levels = [50, 60, 70, 80]
    new_threshold = current
    for level in band_levels:
        if level > current and level <= best_band:
            new_threshold = level
            break

    if new_threshold <= current or new_threshold > 80:
        return None

    # Verify enough predictions exist above the new threshold
    above_count = conn.execute(
        "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved' AND confidence >= ?",
        (new_threshold,),
    ).fetchone()[0]
    if above_count < 10:
        return None

    reason = (
        f"Confidence {new_threshold}+ band has {best_wr:.0f}% win rate "
        f"vs {overall_wr:.0f}% overall — focusing on higher-conviction trades"
    )
    applied, was_clamped, suffix = _apply_param_change(
        profile_id, user_id,
        "confidence_threshold_optimization",
        "ai_confidence_threshold",
        current, new_threshold, reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return (
        f"Raised confidence threshold from {current} to "
        f"{int(applied)}{suffix} ({reason})"
    )


def _optimize_regime_position_sizing(conn, ctx, profile_id, user_id,
                                     overall_wr, resolved):
    """Reduce position size in losing regimes, increase in winning regimes."""
    if overall_wr < 45:
        return None  # Profile must be healthy enough for regime analysis
    if _get_recent_adjustment(profile_id, "max_position_pct", days=3):
        return None

    # Check if regime column exists
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_predictions)").fetchall()}
    if "regime_at_prediction" not in cols:
        return None

    # 2026-05-13 — exclude data_quality-tagged ai_predictions
    from journal import data_quality_clause
    _aip_dq = data_quality_clause(conn, table="ai_predictions")
    rows = conn.execute(
        f"""SELECT regime_at_prediction, COUNT(*) as total,
                  SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins
           FROM ai_predictions
           WHERE status='resolved' AND regime_at_prediction IS NOT NULL{_aip_dq}
           GROUP BY regime_at_prediction HAVING COUNT(*) >= 30"""
    ).fetchall()

    if not rows:
        return None

    # Get current regime
    try:
        from market_regime import detect_regime
        regime_info = detect_regime()
        current_regime = regime_info.get("regime", "unknown") if regime_info else "unknown"
    except Exception:
        return None

    # Find current regime's win rate
    current_regime_wr = None
    for r in rows:
        if r["regime_at_prediction"] == current_regime:
            current_regime_wr = (r["wins"] / r["total"] * 100) if r["total"] > 0 else None
            break

    if current_regime_wr is None:
        return None

    diff = current_regime_wr - overall_wr
    current_pct = ctx.max_position_pct

    if diff <= -15:
        # Losing regime — reduce by 25%
        if _was_adjustment_effective(profile_id, "max_position_pct") == "worsened":
            return None
        new_pct = round(max(0.03, current_pct * 0.75), 4)
        if new_pct >= current_pct:
            return None
        reason = (
            f"{current_regime} regime win rate {current_regime_wr:.0f}% "
            f"vs {overall_wr:.0f}% overall — reducing exposure"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "regime_position_sizing",
            "max_position_pct", current_pct, new_pct, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Reduced position size from {current_pct:.1%} to "
            f"{applied:.1%}{suffix} ({reason})"
        )

    elif diff >= 15:
        # Winning regime — increase by 15%
        if _was_adjustment_effective(profile_id, "max_position_pct") == "worsened":
            return None
        new_pct = round(min(0.20, current_pct * 1.15), 4)
        if new_pct <= current_pct:
            return None
        reason = (
            f"{current_regime} regime win rate {current_regime_wr:.0f}% "
            f"vs {overall_wr:.0f}% overall — increasing exposure to edge"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "regime_position_sizing",
            "max_position_pct", current_pct, new_pct, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Increased position size from {current_pct:.1%} to "
            f"{applied:.1%}{suffix} ({reason})"
        )

    return None


def _optimize_strategy_toggles(conn, ctx, profile_id, user_id,
                                overall_wr, resolved):
    """Disable the worst-performing strategy if it's dragging down results."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_predictions)").fetchall()}
    if "strategy_type" not in cols:
        return None

    # 2026-05-13 — exclude data_quality-tagged ai_predictions
    from journal import data_quality_clause
    _aip_dq = data_quality_clause(conn, table="ai_predictions")
    rows = conn.execute(
        f"""SELECT strategy_type, COUNT(*) as total,
                  SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins
           FROM ai_predictions
           WHERE status='resolved' AND strategy_type IS NOT NULL{_aip_dq}
           GROUP BY strategy_type HAVING COUNT(*) >= ?
           ORDER BY (CAST(SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) AS REAL)
                     / COUNT(*)) ASC""",
        (60 if getattr(ctx, "_runtime_under_volume_floor", False) else 30,),
    ).fetchall()

    if not rows:
        return None

    # Count currently enabled toggles
    enabled_count = sum(1 for col in _STRATEGY_TYPE_TO_TOGGLE.values()
                        if getattr(ctx, col, True))
    if enabled_count < 2:
        return None  # Never disable the last strategy

    for r in rows:
        stype = r["strategy_type"]
        total = r["total"]
        # DISPLAY_ONLY: div-by-zero guard, not a tightening gate.
        wr = (r["wins"] / total * 100) if total > 0 else 0

        # Must be both bad absolutely AND bad relative to overall
        if wr >= 30 or (overall_wr - wr) < 15:
            continue

        toggle_col = _STRATEGY_TYPE_TO_TOGGLE.get(stype)
        if not toggle_col:
            # No profile-level toggle — these are the modular `strategies/`
            # plugins (insider_cluster, options-flow-derived, etc.). The
            # alpha_decay module already has a deprecation pipeline that
            # excludes them from `get_active_strategies()`, with automatic
            # restoration when their rolling Sharpe recovers. Wire to it.
            from display_names import display_name as _dn

            # Per-strategy cooldown (3 days) under a synthetic param key so
            # the existing tuning_history machinery can track and respect it.
            deprecate_key = f"deprecate:{stype}"
            if _get_recent_adjustment(profile_id, deprecate_key, days=3):
                continue
            if _was_adjustment_effective(profile_id, deprecate_key) == "worsened":
                continue

            db_path = getattr(ctx, "db_path", None)
            if not db_path:
                continue

            try:
                from alpha_decay import deprecate_strategy, is_deprecated
                if is_deprecated(db_path, stype):
                    continue
                from models import log_tuning_change
                detection = {
                    "reason": (
                        f"Self-tuner: {_dn(stype)} win rate "
                        f"{wr:.0f}% ({r['wins']}/{total}) vs "
                        f"{overall_wr:.0f}% overall"
                    ),
                    "current_rolling_sharpe": None,
                    "lifetime_sharpe": None,
                    "consecutive_bad_days": 0,
                }
                deprecate_strategy(db_path, stype, detection)
                log_tuning_change(
                    profile_id, user_id, "strategy_deprecate",
                    deprecate_key, "active", "deprecated",
                    detection["reason"],
                    win_rate_at_change=overall_wr,
                    predictions_resolved=resolved,
                )
                return (
                    f"Deprecated {_dn(stype)} strategy "
                    f"(win rate {wr:.0f}% — will auto-restore when rolling "
                    f"Sharpe recovers)"
                )
            except Exception as _exc:
                logger.warning("Deprecation failed for %s: %s", stype, _exc)
                continue

        if not getattr(ctx, toggle_col, True):
            continue  # Already disabled

        if _get_recent_adjustment(profile_id, toggle_col, days=3):
            continue
        if _was_adjustment_effective(profile_id, toggle_col) == "worsened":
            continue

        from models import update_trading_profile, log_tuning_change
        from display_names import display_name as _dn
        update_trading_profile(profile_id, **{toggle_col: 0})
        reason = (
            f"{_dn(stype)} win rate {wr:.0f}% ({r['wins']}/{total}) "
            f"vs {overall_wr:.0f}% overall"
        )
        log_tuning_change(
            profile_id, user_id, "strategy_toggle",
            toggle_col, "1", "0", reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Disabled {_dn(stype)} strategy ({reason})"

    return None


def _optimize_stop_take_profit(conn, ctx, profile_id, user_id,
                                overall_wr, resolved):
    """Adjust stop-loss and take-profit based on actual trade P&L distribution."""
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None

    # Get closed trades with P&L. Phase 5e — exclude
    # data_quality-tagged rows (corrupt `price` field would
    # poison stop/TP threshold tuning).
    from journal import data_quality_clause
    _dq = data_quality_clause(conn)
    trades = conn.execute(
        f"""SELECT price, qty, pnl, stop_loss, take_profit
           FROM trades
           WHERE pnl IS NOT NULL AND lower(side) = 'buy'
             AND price > 0 AND qty > 0{_dq}"""
    ).fetchall()

    if len(trades) < 15:
        return None

    losses = []
    wins = []
    for t in trades:
        ret_pct = (t["pnl"] / (t["price"] * t["qty"])) * 100
        if t["pnl"] < 0:
            losses.append(ret_pct)
        elif t["pnl"] > 0:
            wins.append(ret_pct)

    # --- Stop-loss optimization ---
    if not _get_recent_adjustment(profile_id, "stop_loss_pct", days=3):
        if _was_adjustment_effective(profile_id, "stop_loss_pct") != "worsened":
            if losses and overall_wr > 45:
                sl_pct = ctx.stop_loss_pct * 100  # e.g., 3.0
                # Count losses that are near the stop level (within 1% of stop)
                near_stop = [l for l in losses if abs(l) <= sl_pct + 1.0]
                if len(near_stop) > len(losses) * 0.4:
                    # 40%+ of losses cluster near the stop → stop is too tight
                    new_sl = round(min(0.08, ctx.stop_loss_pct * 1.20), 4)
                    if new_sl > ctx.stop_loss_pct:
                        old_val = ctx.stop_loss_pct
                        reason = (
                            f"{len(near_stop)}/{len(losses)} losses cluster near "
                            f"{sl_pct:.1f}% stop — widening to give trades more room"
                        )
                        applied, _, suffix = _apply_param_change(
                            profile_id, user_id, "stop_loss_optimization",
                            "stop_loss_pct", old_val, new_sl, reason,
                            win_rate_at_change=overall_wr,
                            predictions_resolved=resolved,
                        )
                        return (
                            f"Widened stop-loss from {old_val:.1%} to "
                            f"{applied:.1%}{suffix} ({reason})"
                        )

    # --- Take-profit optimization ---
    if not _get_recent_adjustment(profile_id, "take_profit_pct", days=3):
        if _was_adjustment_effective(profile_id, "take_profit_pct") != "worsened":
            if wins:
                tp_pct = ctx.take_profit_pct * 100  # e.g., 10.0
                avg_win = sum(wins) / len(wins)
                # If average win is less than 50% of the TP, TP is too ambitious
                if avg_win < tp_pct * 0.5 and tp_pct > 3.0:
                    new_tp = round(max(0.03, ctx.take_profit_pct * 0.80), 4)
                    if new_tp < ctx.take_profit_pct:
                        old_val = ctx.take_profit_pct
                        reason = (
                            f"Average win is +{avg_win:.1f}% but TP is at "
                            f"{tp_pct:.1f}% — tightening to capture more gains"
                        )
                        applied, _, suffix = _apply_param_change(
                            profile_id, user_id, "take_profit_optimization",
                            "take_profit_pct", old_val, new_tp, reason,
                            win_rate_at_change=overall_wr,
                            predictions_resolved=resolved,
                        )
                        return (
                            f"Tightened take-profit from {old_val:.1%} to "
                            f"{applied:.1%}{suffix} ({reason})"
                        )

    return None


def _optimize_position_size_upward(conn, ctx, profile_id, user_id,
                                    overall_wr, resolved):
    """Increase position size when there is a proven, consistent edge."""
    if overall_wr < 55 or resolved < 30:
        return None
    if ctx.max_position_pct >= 0.15:
        return None  # Already at cap
    if _get_recent_adjustment(profile_id, "max_position_pct", days=3):
        return None
    if _was_adjustment_effective(profile_id, "max_position_pct") == "worsened":
        return None

    # Verify average return is positive
    # 2026-05-13 — exclude data_quality-tagged ai_predictions
    from journal import data_quality_clause
    _aip_dq = data_quality_clause(conn, table="ai_predictions")
    avg_ret = conn.execute(
        f"SELECT AVG(actual_return_pct) FROM ai_predictions WHERE status='resolved'{_aip_dq}"
    ).fetchone()[0]
    if avg_ret is None or avg_ret <= 0:
        return None

    current = ctx.max_position_pct
    new_pct = round(min(0.15, current * 1.15), 4)
    if new_pct <= current:
        return None

    reason = (
        f"Win rate {overall_wr:.0f}% with +{avg_ret:.2f}% avg return "
        f"on {resolved} predictions — increasing position size to capitalize"
    )
    applied, was_clamped, suffix = _apply_param_change(
        profile_id, user_id,
        "position_size_optimization",
        "max_position_pct",
        current, new_pct, reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return (
        f"Increased position size from {current:.1%} to "
        f"{applied:.1%}{suffix} ({reason})"
    )


# ---------------------------------------------------------------------------
# Wave 1 — extended-coverage optimizers (Group A: concentration/risk,
# Group D: timing). Each rule follows the same pattern: cooldown check,
# reverse-if-worsened guard, signal detection from data, bound clamping
# via param_bounds.clamp, write via update_trading_profile, log via
# log_tuning_change. Returns a human-readable string on action, None
# on no-op.
# ---------------------------------------------------------------------------

def _bound(name, value):
    """Clamp helper — wraps param_bounds.clamp with a stable import."""
    from param_bounds import clamp
    return clamp(name, value)


def _label(param_name):
    """Display-name shortcut for use in optimizer return strings.

    The strings returned by optimizers flow into the dashboard activity
    ticker and the weekly digest — both user-facing surfaces. Always
    use this helper when interpolating a parameter name into a return
    string so we never leak snake_case identifiers like
    "atr_multiplier_tp" to the user. `tests/test_no_snake_case_in_optimizer_strings.py`
    enforces this.
    """
    from display_names import display_name
    return display_name(param_name)


def _safe_change_guarded(profile_id, param_name):
    """Common cooldown + reverse-if-worsened check. Returns True if the
    rule is ALLOWED to make a change to this parameter right now."""
    if _get_recent_adjustment(profile_id, param_name, days=3):
        return False
    if _was_adjustment_effective(profile_id, param_name) == "worsened":
        return False
    return True


def _optimize_max_total_positions(conn, ctx, profile_id, user_id,
                                   overall_wr, resolved):
    """Concentration risk: reduce position count cap when avg loss is large
    AND the cap is being hit often. Increase when WR is strong AND the cap
    is constraining capacity."""
    if not _safe_change_guarded(profile_id, "max_total_positions"):
        return None

    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None

    # 2026-05-13 — exclude data_quality-tagged trades from
    # avg-loss / avg-win calc; phantom-stop pollution would
    # extreme-distort these.
    from journal import data_quality_clause
    _trades_dq = data_quality_clause(conn, table="trades")
    # Average loss size on closed losers
    row = conn.execute(
        f"SELECT AVG(pnl) as avg_loss FROM trades "
        f"WHERE pnl IS NOT NULL AND pnl < 0{_trades_dq}"
    ).fetchone()
    avg_loss = row["avg_loss"] if row and row["avg_loss"] is not None else 0

    current = getattr(ctx, "max_total_positions", 10)
    if not isinstance(current, int):
        current = int(current)

    # Reduce when losses are deep AND we're losing broadly.
    if overall_wr < 40 and avg_loss < -200:
        new_val = _bound("max_total_positions", current - 1)
        if new_val == current:
            return None
        reason = (
            f"Concentration risk — avg loss ${avg_loss:.0f} on {overall_wr:.0f}% WR "
            f"— reduce concurrent positions"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "concentration_reduce",
            "max_total_positions", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Reduced max concurrent positions from {current} to {applied}{suffix} ({reason})"

    # Increase when strong WR AND average winner is meaningful.
    # _trades_dq already in scope from the avg_loss block above.
    row = conn.execute(
        f"SELECT AVG(pnl) as avg_win FROM trades "
        f"WHERE pnl IS NOT NULL AND pnl > 0{_trades_dq}"
    ).fetchone()
    avg_win = row["avg_win"] if row and row["avg_win"] is not None else 0

    if overall_wr >= 60 and avg_win > 100:
        new_val = _bound("max_total_positions", current + 1)
        if new_val == current:
            return None
        reason = (
            f"Strong edge — {overall_wr:.0f}% WR, avg winner ${avg_win:.0f} "
            f"— allow more concurrent positions"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "concentration_increase",
            "max_total_positions", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Raised max concurrent positions from {current} to {applied}{suffix} ({reason})"

    return None


def _optimize_max_correlation(conn, ctx, profile_id, user_id,
                               overall_wr, resolved):
    """Diversification: tighten the correlation cap when losses cluster in
    correlated names. Loosen when too many candidates are gated out."""
    if not _safe_change_guarded(profile_id, "max_correlation"):
        return None

    # Heuristic: count losing trades that occurred within the same week
    # and same sector — a proxy for correlated drawdowns.
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None

    # Phase 5e — exclude data_quality-tagged rows from cluster
    # detection. Phantom-stop incident rows all hit on the same
    # day; including them would inflate "losing weeks with
    # clusters" and falsely trigger max_correlation tightening.
    from journal import data_quality_clause
    _dq = data_quality_clause(conn)
    # DISPLAY_ONLY: 3 = cluster definition (3 losing trades in one
    # week = a "loss cluster"), not a sample-size for tightening
    # evidence. The actual evidence gate is total_weeks below.
    cluster_row = conn.execute(
        f"""SELECT strftime('%Y-%W', timestamp) as week, COUNT(*) as cnt
           FROM trades
           WHERE pnl IS NOT NULL AND pnl < 0{_dq}
           GROUP BY week HAVING cnt >= 3"""
    ).fetchall()

    losing_weeks_with_clusters = len(cluster_row)
    total_weeks_row = conn.execute(
        f"""SELECT COUNT(DISTINCT strftime('%Y-%W', timestamp)) as n
           FROM trades WHERE pnl IS NOT NULL{_dq}"""
    ).fetchone()
    total_weeks = total_weeks_row["n"] if total_weeks_row else 0

    if total_weeks < 4:
        return None  # Not enough history

    cluster_rate = losing_weeks_with_clusters / total_weeks if total_weeks > 0 else 0
    current = getattr(ctx, "max_correlation", 0.7)

    if cluster_rate >= 0.4:
        # Tighten — loss clusters on >40% of weeks suggests over-correlation
        new_val = _bound("max_correlation", round(current - 0.05, 4))
        if new_val >= current:
            return None
        reason = (
            f"Loss-cluster weeks {cluster_rate:.0%} — tighten correlation cap"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "correlation_tighten",
            "max_correlation", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Tightened {_label('max_correlation')} from {current:.2f} to {applied:.2f}{suffix} ({reason})"

    # Loosen if very few clustering weeks AND profile is performing well
    if cluster_rate < 0.1 and overall_wr >= 55:
        new_val = _bound("max_correlation", round(current + 0.05, 4))
        if new_val <= current:
            return None
        reason = (
            f"Low loss-clustering ({cluster_rate:.0%}) + healthy WR — "
            f"loosen correlation cap to admit more candidates"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "correlation_loosen",
            "max_correlation", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Loosened {_label('max_correlation')} from {current:.2f} to {applied:.2f}{suffix} ({reason})"

    return None


def _optimize_max_sector_positions(conn, ctx, profile_id, user_id,
                                    overall_wr, resolved):
    """Sector cap: reduce when sector concentration accompanies bad days."""
    if not _safe_change_guarded(profile_id, "max_sector_positions"):
        return None

    # Use the same loss-cluster heuristic but coarser.
    if overall_wr < 35:
        current = getattr(ctx, "max_sector_positions", 5)
        if not isinstance(current, int):
            current = int(current)
        new_val = _bound("max_sector_positions", current - 1)
        if new_val == current:
            return None
        reason = (
            f"Overall WR {overall_wr:.0f}% — tighten sector cap to "
            f"avoid concentration drawdowns"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "sector_cap_tighten",
            "max_sector_positions", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Reduced {_label('max_sector_positions')} from {current} to {applied}{suffix} ({reason})"

    return None


def _optimize_drawdown_thresholds(conn, ctx, profile_id, user_id,
                                   overall_wr, resolved):
    """Drawdown thresholds: tighten when past pause/reduce events were
    followed by deeper drawdown (didn't catch the slide early enough)."""
    # We don't yet track drawdown-triggered events in a structured way.
    # For now: when overall_wr is in the deteriorating zone (35-45%) AND
    # we haven't tightened recently, tighten by one notch — slightly more
    # conservative defaults during stretches of underperformance.
    if not (35 <= overall_wr < 45):
        return None
    if not _safe_change_guarded(profile_id, "drawdown_pause_pct"):
        return None
    current = getattr(ctx, "drawdown_pause_pct", 0.20)
    new_val = _bound("drawdown_pause_pct", round(current - 0.02, 4))
    if new_val >= current:
        return None
    reason = (
        f"WR drifting at {overall_wr:.0f}% — tighten drawdown-pause "
        f"to catch deterioration sooner"
    )
    applied, _, suffix = _apply_param_change(
        profile_id, user_id, "drawdown_pause_tighten",
        "drawdown_pause_pct", current, new_val, reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return (f"Tightened drawdown-pause threshold from {current:.0%} to "
            f"{applied:.0%}{suffix} ({reason})")


def _optimize_drawdown_reduce(conn, ctx, profile_id, user_id,
                               overall_wr, resolved):
    """Mirror of pause-threshold but for the reduce-position trigger."""
    if not (35 <= overall_wr < 45):
        return None
    if not _safe_change_guarded(profile_id, "drawdown_reduce_pct"):
        return None
    current = getattr(ctx, "drawdown_reduce_pct", 0.10)
    new_val = _bound("drawdown_reduce_pct", round(current - 0.01, 4))
    if new_val >= current:
        return None
    reason = (
        f"WR drifting at {overall_wr:.0f}% — tighten drawdown-reduce "
        f"trigger so position-size cuts kick in earlier"
    )
    applied, _, suffix = _apply_param_change(
        profile_id, user_id, "drawdown_reduce_tighten",
        "drawdown_reduce_pct", current, new_val, reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return (f"Tightened drawdown-reduce threshold from {current:.0%} to "
            f"{applied:.0%}{suffix} ({reason})")


def _optimize_price_band(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """Tune min_price / max_price within 0.5x-2.0x of current (and the
    absolute floor/ceiling in PARAM_BOUNDS) when entries near the band
    edges consistently fail."""
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None

    current_min = float(getattr(ctx, "min_price", 1.0))
    current_max = float(getattr(ctx, "max_price", 20.0))

    # Bottom-of-band failure check: trades entered within 1.5× of min_price.
    # Phase 5e — CRITICAL fix. Without the data_quality filter, the
    # phantom-stop rows (price=$0.16-$1.48) all matched this bottom-
    # of-band query and triggered min_price RAISES based on their
    # 100% loss rate. The corrupt price field IS the trigger
    # condition itself; without filtering, self-tuning was
    # systematically WRONG for the past day.
    from journal import data_quality_clause
    _dq = data_quality_clause(conn)
    bottom_threshold = current_min * 1.5
    bot_row = conn.execute(
        f"SELECT COUNT(*) as cnt, "
        f" SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins "
        f"FROM trades WHERE pnl IS NOT NULL AND price <= ? "
        f"AND price > 0{_dq}",
        (bottom_threshold,),
    ).fetchone()
    if (bot_row and bot_row["cnt"] >= 5):
        bot_wr = (bot_row["wins"] or 0) / bot_row["cnt"] * 100
        if bot_wr < 30 and _safe_change_guarded(profile_id, "min_price"):
            # Raise floor — never above 2× current (identity guard) and
            # always within absolute bounds.
            candidate = min(current_min * 1.25, current_min * 2.0)
            new_min = _bound("min_price", round(candidate, 2))
            if new_min > current_min and new_min < current_max:
                reason = (
                    f"Bottom-of-band entries (≤${bottom_threshold:.2f}) "
                    f"win rate {bot_wr:.0f}% on {bot_row['cnt']} trades — "
                    f"raise {_label('min_price')} floor"
                )
                applied, _, suffix = _apply_param_change(
                    profile_id, user_id, "price_band_min_raise",
                    "min_price", current_min, new_min, reason,
                    win_rate_at_change=overall_wr,
                    predictions_resolved=resolved,
                )
                return (f"Raised {_label('min_price')} from ${current_min:.2f} to "
                        f"${applied:.2f}{suffix} ({reason})")

    # Top-of-band failure check: trades entered within 0.85× of max_price.
    # Phase 5e exclusion: same pattern as bottom-of-band.
    top_threshold = current_max * 0.85
    top_row = conn.execute(
        f"SELECT COUNT(*) as cnt, "
        f" SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins "
        f"FROM trades WHERE pnl IS NOT NULL AND price >= ?{_dq}",
        (top_threshold,),
    ).fetchone()
    if (top_row and top_row["cnt"] >= 5):
        top_wr = (top_row["wins"] or 0) / top_row["cnt"] * 100
        if top_wr < 30 and _safe_change_guarded(profile_id, "max_price"):
            candidate = max(current_max * 0.85, current_max * 0.5)
            new_max = _bound("max_price", round(candidate, 2))
            if new_max < current_max and new_max > current_min:
                reason = (
                    f"Top-of-band entries (≥${top_threshold:.2f}) "
                    f"win rate {top_wr:.0f}% on {top_row['cnt']} trades — "
                    f"lower {_label('max_price')} ceiling"
                )
                applied, _, suffix = _apply_param_change(
                    profile_id, user_id, "price_band_max_lower",
                    "max_price", current_max, new_max, reason,
                    win_rate_at_change=overall_wr,
                    predictions_resolved=resolved,
                )
                return (f"Lowered {_label('max_price')} from ${current_max:.2f} to "
                        f"${applied:.2f}{suffix} ({reason})")

    return None


def _optimize_avoid_earnings_days(conn, ctx, profile_id, user_id,
                                   overall_wr, resolved):
    # THRASH_OK: implicit neutral band via the 5pp threshold
    # (only fires when in-window WR differs from out-of-window WR
    # by ≥5pp). Within ±5pp of parity, the rule returns None
    # without changing anything — equivalent to a cooldown.
    """Earnings window: tighten when predictions made within
    `current_avoid_days` of earnings underperform predictions made
    outside that window; loosen when intra-window predictions are
    actually fine.

    Reads `days_to_earnings` from `features_json` (added 2026-04-27
    in trade_pipeline). Predictions made before that feature was
    captured (older history) report `days_to_earnings = -1` and are
    skipped from the buckets. The rule self-skips when fewer than
    10 in-window OR 10 out-of-window resolved samples exist.
    """
    import json as _json
    current = int(getattr(ctx, "avoid_earnings_days", 2) or 2)
    if current <= 0:
        # Already at minimum (no avoidance); only loosen-direction
        # via positive evidence; keep current behavior.
        pass
    try:
        rows = conn.execute(
            "SELECT features_json, actual_outcome FROM ai_predictions "
            "WHERE status='resolved' "
            "AND actual_outcome IN ('win', 'loss') "
            "AND features_json IS NOT NULL "
            "ORDER BY id ASC"
        ).fetchall()
    except Exception:
        return None

    in_w_total = in_w_wins = 0
    out_w_total = out_w_wins = 0
    for fjson, outcome in rows:
        try:
            f = _json.loads(fjson)
        except (TypeError, ValueError, _json.JSONDecodeError) as _jp_exc:
            # Per-row parse over a feature-blob list; skip malformed
            # rows but surface data-quality issue at DEBUG.
            logger.debug(
                "earnings-window scan skipped row, bad features_json: %s: %s",
                type(_jp_exc).__name__, _jp_exc,
            )
            continue
        d2e = f.get("days_to_earnings")
        if d2e is None or d2e < 0:
            continue
        is_win = 1 if outcome == "win" else 0
        if d2e <= current:
            in_w_total += 1
            in_w_wins += is_win
        else:
            out_w_total += 1
            out_w_wins += is_win

    if in_w_total < 10 or out_w_total < 10:
        return None

    in_wr = in_w_wins / in_w_total * 100
    out_wr = out_w_wins / out_w_total * 100

    # Underperformance band: in-window predictions notably worse than
    # out-of-window. Tighten the avoidance (raise avoid_earnings_days).
    if in_wr < out_wr - 5 and current < 7:
        new_val = _bound("avoid_earnings_days", current + 1)
        if new_val == current:
            return None
        reason = (
            f"In-window WR {in_wr:.0f}% < out-of-window {out_wr:.0f}% "
            f"by {out_wr - in_wr:.0f}pp"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "avoid_earnings_tighten",
            "avoid_earnings_days", current, new_val, reason,
            win_rate_at_change=overall_wr,
            predictions_resolved=resolved,
        )
        return (
            f"Tightened {_label('avoid_earnings_days')} from "
            f"{current} to {applied}{suffix} ({reason})"
        )
    # Outperformance band: in-window predictions actually do BETTER.
    # Loosen so we don't miss those setups.
    if in_wr > out_wr + 5 and current > 0:
        new_val = _bound("avoid_earnings_days", current - 1)
        if new_val == current:
            return None
        reason = (
            f"In-window WR {in_wr:.0f}% > out-of-window {out_wr:.0f}% "
            f"by {in_wr - out_wr:.0f}pp"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "avoid_earnings_loosen",
            "avoid_earnings_days", current, new_val, reason,
            win_rate_at_change=overall_wr,
            predictions_resolved=resolved,
        )
        return (
            f"Loosened {_label('avoid_earnings_days')} from "
            f"{current} to {applied}{suffix} ({reason})"
        )
    return None


def _optimize_skip_first_minutes(conn, ctx, profile_id, user_id,
                                  overall_wr, resolved):
    # THRASH_OK: implicit neutral band via the 5pp threshold
    # (only fires when early-window WR differs from late-window WR
    # by ≥5pp). Within ±5pp of parity, the rule returns None.
    """First-X-minutes filter: tighten when predictions made within
    the first `current` minutes of the trading session underperform;
    loosen when those are fine.

    Uses `ai_predictions.timestamp` directly — we don't need a new
    feature column. Equity market opens at 13:30 UTC; minutes_since_open
    is computed from the timestamp's HH:MM:SS within that day. Rows
    outside market hours (after-hours / weekends / pre-open) are
    skipped from the buckets. Self-skips when either bucket has < 10
    resolved samples.
    """
    current = int(getattr(ctx, "skip_first_minutes", 0) or 0)
    try:
        rows = conn.execute(
            "SELECT timestamp, actual_outcome FROM ai_predictions "
            "WHERE status='resolved' "
            "AND actual_outcome IN ('win', 'loss') "
            "ORDER BY id ASC"
        ).fetchall()
    except Exception:
        return None

    # Use a canonical "boundary" of 30 minutes for the comparison
    # (the param's max). If the param is currently 0 we're testing
    # whether enabling it would help; if non-zero we're testing
    # whether to widen/narrow.
    boundary = 30
    early_total = early_wins = 0
    late_total = late_wins = 0
    for ts_iso, outcome in rows:
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(ts_iso.replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except (AttributeError, ValueError, TypeError) as _ts_exc:
            # Per-row parse over a prediction list; skip malformed
            # rows but surface data quality issue at DEBUG.
            logger.debug(
                "early-close window scan skipped row (bad ts): %s: %s",
                type(_ts_exc).__name__, _ts_exc,
            )
            continue
        # Market open in UTC = 13:30 (during DST; 14:30 in winter).
        # Use 13:30 as the canonical anchor — drift of 1 hour over
        # 6 months of data is acceptable noise; we're bucketing by
        # 30-minute granularity.
        minutes_into_day = ts.hour * 60 + ts.minute
        market_open_min = 13 * 60 + 30
        minutes_since_open = minutes_into_day - market_open_min
        if minutes_since_open < 0 or minutes_since_open > 6 * 60 + 30:
            # Pre-open or post-close; skip.
            continue
        is_win = 1 if outcome == "win" else 0
        if minutes_since_open < boundary:
            early_total += 1
            early_wins += is_win
        else:
            late_total += 1
            late_wins += is_win

    if early_total < 10 or late_total < 10:
        return None

    early_wr = early_wins / early_total * 100
    late_wr = late_wins / late_total * 100

    # Early predictions notably worse → enable / extend the skip.
    if early_wr < late_wr - 5:
        new_val = _bound("skip_first_minutes", min(boundary, current + 5))
        if new_val == current:
            return None
        reason = (
            f"Opening {boundary}min WR {early_wr:.0f}% < later "
            f"{late_wr:.0f}% by {late_wr - early_wr:.0f}pp"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "skip_first_minutes_tighten",
            "skip_first_minutes", current, new_val, reason,
            win_rate_at_change=overall_wr,
            predictions_resolved=resolved,
        )
        return (
            f"Raised {_label('skip_first_minutes')} from {current} "
            f"to {applied}{suffix} ({reason})"
        )
    # Early predictions actually fine → reduce / disable the skip.
    if early_wr > late_wr - 1 and current > 0:
        new_val = _bound("skip_first_minutes", max(0, current - 5))
        if new_val == current:
            return None
        reason = (
            f"Opening {boundary}min WR {early_wr:.0f}% ≈ later "
            f"{late_wr:.0f}%"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "skip_first_minutes_loosen",
            "skip_first_minutes", current, new_val, reason,
            win_rate_at_change=overall_wr,
            predictions_resolved=resolved,
        )
        return (
            f"Lowered {_label('skip_first_minutes')} from {current} "
            f"to {applied}{suffix} ({reason})"
        )
    return None


# ---------------------------------------------------------------------------
# Wave 2 — entry filter optimizers (Layer 1 Group C). Pattern: bucket
# resolved predictions by which side of the threshold they fell on (read
# from features_json), tighten if marginal bucket underperforms, loosen
# if too many would-have-winners are filtered out. All rules degrade
# gracefully (no-op) when the relevant feature isn't present in
# features_json yet.
# ---------------------------------------------------------------------------

def _bucket_by_feature(conn, feature_name):
    """Iterate resolved predictions and yield (feature_value, outcome)
    tuples for those where features_json contains a numeric value for
    `feature_name`. Skips silently for predictions without the feature."""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_predictions)").fetchall()}
    if "features_json" not in cols:
        return
    rows = conn.execute(
        "SELECT actual_outcome, features_json FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome IN ('win','loss') "
        "  AND features_json IS NOT NULL"
    ).fetchall()
    import json as _j
    for r in rows:
        try:
            f = _j.loads(r["features_json"])
        except (TypeError, ValueError, _j.JSONDecodeError) as _jp_exc:
            # Per-row parse over a feature-blob list; skip malformed
            # rows but surface data-quality issue at DEBUG.
            logger.debug(
                "feature-iter skipped row, bad features_json: %s: %s",
                type(_jp_exc).__name__, _jp_exc,
            )
            continue
        v = f.get(feature_name)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        yield v, r["actual_outcome"]


def _filter_threshold_signal(conn, feature_name, threshold,
                              tighten_band_factor=1.5):
    """Return (marginal_wr, marginal_n) for predictions whose
    `feature_name` value fell within `tighten_band_factor` of `threshold`
    on the just-passing side. (None, 0) if not enough data."""
    band_top = threshold * tighten_band_factor
    wins = total = 0
    for v, outcome in _bucket_by_feature(conn, feature_name):
        if threshold <= v <= band_top:
            total += 1
            if outcome == "win":
                wins += 1
    if total < 5:
        return None, 0
    return (wins / total * 100.0), total


def _optimize_min_volume(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """Raise min_volume when bottom-band entries (just above threshold)
    are losing badly. Lower when the system is starved of candidates and
    overall WR is healthy."""
    if not _safe_change_guarded(profile_id, "min_volume"):
        return None
    current = int(getattr(ctx, "min_volume", 500_000))
    wr, n = _filter_threshold_signal(conn, "volume", current,
                                       tighten_band_factor=1.5)
    if wr is None:
        return None
    if wr < 30:
        new_val = _bound("min_volume", int(current * 1.50))
        if new_val == current:
            return None
        reason = (
            f"Marginal-volume entries (≤ 1.5× threshold) WR {wr:.0f}% on "
            f"{n} samples — raise {_label('min_volume')} floor"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "min_volume_raise",
            "min_volume", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Raised {_label('min_volume')} from {current:,} to {int(applied):,}{suffix} ({reason})"
    return None


def _optimize_volume_surge_multiplier(conn, ctx, profile_id, user_id,
                                       overall_wr, resolved):
    """Same shape, on volume_ratio (the multiple of average volume)."""
    if not _safe_change_guarded(profile_id, "volume_surge_multiplier"):
        return None
    current = float(getattr(ctx, "volume_surge_multiplier", 2.0))
    wr, n = _filter_threshold_signal(conn, "volume_ratio", current,
                                       tighten_band_factor=1.25)
    if wr is None:
        return None
    if wr < 35:
        new_val = _bound("volume_surge_multiplier", round(current + 0.25, 2))
        if new_val == current:
            return None
        reason = (
            f"Marginal volume-surge entries WR {wr:.0f}% on {n} samples — "
            f"require stronger surge for confirmation"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "volume_surge_tighten",
            "volume_surge_multiplier", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised {_label('volume_surge_multiplier')} from {current:.2f} to "
                f"{applied:.2f}{suffix} ({reason})")
    return None


def _optimize_breakout_volume_threshold(conn, ctx, profile_id, user_id,
                                         overall_wr, resolved):
    """Tighten the breakout-volume gate when marginal breakouts fail."""
    if not _safe_change_guarded(profile_id, "breakout_volume_threshold"):
        return None
    current = float(getattr(ctx, "breakout_volume_threshold", 1.0))
    wr, n = _filter_threshold_signal(conn, "volume_ratio", current,
                                       tighten_band_factor=1.5)
    if wr is None:
        return None
    if wr < 35:
        new_val = _bound("breakout_volume_threshold", round(current + 0.25, 2))
        if new_val == current:
            return None
        reason = (
            f"Marginal-breakout entries WR {wr:.0f}% on {n} samples — "
            f"require more confirmation volume"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "breakout_volume_tighten",
            "breakout_volume_threshold", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised {_label('breakout_volume_threshold')} from {current:.2f} to "
                f"{applied:.2f}{suffix} ({reason})")
    return None


def _optimize_gap_pct_threshold(conn, ctx, profile_id, user_id,
                                 overall_wr, resolved):
    """Increase the minimum gap when small-gap entries underperform."""
    if not _safe_change_guarded(profile_id, "gap_pct_threshold"):
        return None
    current = float(getattr(ctx, "gap_pct_threshold", 3.0))
    wr, n = _filter_threshold_signal(conn, "gap_pct", current,
                                       tighten_band_factor=1.2)
    if wr is None:
        return None
    if wr < 35:
        new_val = _bound("gap_pct_threshold", round(current + 0.5, 2))
        if new_val == current:
            return None
        reason = (
            f"Marginal-gap entries (within 1.2× threshold) WR {wr:.0f}% "
            f"on {n} samples — require larger gap"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "gap_threshold_tighten",
            "gap_pct_threshold", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised {_label('gap_pct_threshold')} from {current:.2f}% to "
                f"{applied:.2f}%{suffix} ({reason})")
    return None


def _optimize_momentum_5d(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """Tighten 5d-momentum entry threshold when marginal entries fail."""
    if not _safe_change_guarded(profile_id, "momentum_5d_gain"):
        return None
    current = float(getattr(ctx, "momentum_5d_gain", 3.0))
    wr, n = _filter_threshold_signal(conn, "momentum_5d", current,
                                       tighten_band_factor=1.3)
    if wr is None:
        return None
    if wr < 35:
        new_val = _bound("momentum_5d_gain", round(current + 0.5, 2))
        if new_val == current:
            return None
        reason = (
            f"Marginal 5d-momentum entries WR {wr:.0f}% on {n} samples — "
            f"require stronger momentum"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "momentum_5d_tighten",
            "momentum_5d_gain", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised {_label('momentum_5d_gain')} from {current:.2f}% to "
                f"{applied:.2f}%{suffix} ({reason})")
    return None


def _optimize_momentum_20d(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """Same as 5d but for 20-day window."""
    if not _safe_change_guarded(profile_id, "momentum_20d_gain"):
        return None
    current = float(getattr(ctx, "momentum_20d_gain", 5.0))
    wr, n = _filter_threshold_signal(conn, "momentum_20d", current,
                                       tighten_band_factor=1.3)
    if wr is None:
        return None
    if wr < 35:
        new_val = _bound("momentum_20d_gain", round(current + 0.5, 2))
        if new_val == current:
            return None
        reason = (
            f"Marginal 20d-momentum entries WR {wr:.0f}% on {n} samples — "
            f"require stronger momentum"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "momentum_20d_tighten",
            "momentum_20d_gain", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised {_label('momentum_20d_gain')} from {current:.2f}% to "
                f"{applied:.2f}%{suffix} ({reason})")
    return None


def _optimize_rsi_overbought(conn, ctx, profile_id, user_id,
                              overall_wr, resolved):
    """Raise the RSI overbought threshold when entries near the current
    threshold continued upward (i.e., the threshold was too sensitive)."""
    if not _safe_change_guarded(profile_id, "rsi_overbought"):
        return None
    current = float(getattr(ctx, "rsi_overbought", 85.0))
    # Bucket: predictions where rsi is within 5 points of current threshold
    band_lo, band_hi = current - 5, current + 5
    wins = total = 0
    for v, outcome in _bucket_by_feature(conn, "rsi"):
        if band_lo <= v <= band_hi:
            total += 1
            if outcome == "win":
                wins += 1
    if total < 5:
        return None
    wr = wins / total * 100
    # If near-overbought entries still won often, the threshold is too tight.
    if wr >= 55:
        new_val = _bound("rsi_overbought", round(current + 2, 1))
        if new_val == current:
            return None
        reason = (
            f"Near-overbought entries (RSI {band_lo:.0f}-{band_hi:.0f}) "
            f"won {wr:.0f}% on {total} samples — raise threshold"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "rsi_overbought_raise",
            "rsi_overbought", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised {_label('rsi_overbought')} from {current:.0f} to "
                f"{applied:.0f}{suffix} ({reason})")
    return None


def _optimize_rsi_oversold(conn, ctx, profile_id, user_id,
                            overall_wr, resolved):
    """Lower the RSI oversold threshold when near-oversold entries continued
    downward (threshold too sensitive)."""
    if not _safe_change_guarded(profile_id, "rsi_oversold"):
        return None
    current = float(getattr(ctx, "rsi_oversold", 25.0))
    band_lo, band_hi = current - 5, current + 5
    wins = total = 0
    for v, outcome in _bucket_by_feature(conn, "rsi"):
        if band_lo <= v <= band_hi:
            total += 1
            if outcome == "win":
                wins += 1
    if total < 5:
        return None
    wr = wins / total * 100
    if wr >= 55:
        new_val = _bound("rsi_oversold", round(current - 2, 1))
        if new_val == current:
            return None
        reason = (
            f"Near-oversold entries (RSI {band_lo:.0f}-{band_hi:.0f}) "
            f"won {wr:.0f}% on {total} samples — lower threshold"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "rsi_oversold_lower",
            "rsi_oversold", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Lowered {_label('rsi_oversold')} from {current:.0f} to "
                f"{applied:.0f}{suffix} ({reason})")
    return None


# ---------------------------------------------------------------------------
# Wave 3 — exit parameter optimizers (Layer 1 Group B). Each rule reads
# from the trades table to analyze actual exit behavior and tune the
# parameters that control where stops and take-profits are placed.
# ---------------------------------------------------------------------------

def _optimize_short_stop_loss(conn, ctx, profile_id, user_id,
                                 overall_wr, resolved):
    """Tune short_stop_loss_pct from observed short-side losses.

    P1.9b of LONG_SHORT_PLAN.md. Reads only short-direction trade
    outcomes so the long book doesn't drown the signal. Tightens
    stops when shorts get repeatedly stopped out at the current
    threshold (suggesting noise stops); loosens when shorts that
    eventually win first dip past the stop (whipsaw).
    """
    if not _safe_change_guarded(profile_id, "short_stop_loss_pct"):
        return None
    if not getattr(ctx, "enable_short_selling", False):
        return None
    rows = conn.execute(
        "SELECT pnl FROM trades "
        "WHERE pnl IS NOT NULL AND side IN ('short', 'cover')"
    ).fetchall()
    if len(rows) < 10:
        return None
    losses = [r["pnl"] for r in rows if r["pnl"] < 0]
    wins = [r["pnl"] for r in rows if r["pnl"] > 0]
    if not losses:
        return None
    loss_rate = len(losses) / len(rows)
    current = float(getattr(ctx, "short_stop_loss_pct", 0.08) or 0.08)

    # Loss rate >55% (most shorts losing): widen stop — current
    # threshold is acting as a noise stop, getting hit before the
    # thesis plays out.
    if loss_rate > 0.55 and len(rows) >= 20:
        new_val = _bound("short_stop_loss_pct", round(current * 1.15, 4))
        if new_val <= current:
            return None
        reason = (f"Short loss rate {loss_rate*100:.0f}% over {len(rows)} "
                  f"trades — widen stop from noise threshold")
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "short_stop_loss_widen",
            "short_stop_loss_pct", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Widened {_label('short_stop_loss_pct')} from "
                f"{current:.0%} to {applied:.0%}{suffix} ({reason})")

    # Loss rate <30% AND avg winner significantly bigger than avg
    # loser: shorts have edge — can tighten stop slightly to free
    # up capital faster on losers.
    if loss_rate < 0.30 and wins and abs(sum(wins) / len(wins)) > abs(sum(losses) / len(losses)) * 1.5:
        new_val = _bound("short_stop_loss_pct", round(current * 0.9, 4))
        if new_val >= current:
            return None
        reason = (f"Short loss rate {loss_rate*100:.0f}% with strong winners — "
                  f"tighten stop to recycle losers faster")
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "short_stop_loss_tighten",
            "short_stop_loss_pct", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Tightened {_label('short_stop_loss_pct')} from "
                f"{current:.0%} to {applied:.0%}{suffix} ({reason})")
    return None


def _optimize_short_max_position_pct(conn, ctx, profile_id, user_id,
                                       overall_wr, resolved):
    """Tune short_max_position_pct from observed short-side P&L.

    P1.9b of LONG_SHORT_PLAN.md. If shorts have positive profit
    factor we can size them up modestly. If shorts have negative
    profit factor (or PF < 1.0), shrink size. Independent of
    long-side performance.
    """
    if not _safe_change_guarded(profile_id, "short_max_position_pct"):
        return None
    if not getattr(ctx, "enable_short_selling", False):
        return None
    rows = conn.execute(
        "SELECT pnl FROM trades "
        "WHERE pnl IS NOT NULL AND side IN ('short', 'cover')"
    ).fetchall()
    if len(rows) < 15:
        return None
    wins_pnl = sum(r["pnl"] for r in rows if r["pnl"] > 0)
    losses_pnl = abs(sum(r["pnl"] for r in rows if r["pnl"] < 0))
    if losses_pnl <= 0:
        return None
    profit_factor = wins_pnl / losses_pnl
    long_max = float(getattr(ctx, "max_position_pct", 0.10) or 0.10)
    current = getattr(ctx, "short_max_position_pct", None)
    if current is None:
        current = long_max / 2  # default derivation
    current = float(current)

    # PF > 1.5 with >=20 trades: shrinks the gap to long-side cap by 10%
    if profit_factor > 1.5 and len(rows) >= 20:
        new_val = _bound("short_max_position_pct",
                          round(min(long_max, current + (long_max - current) * 0.1), 4))
        if new_val <= current:
            return None
        reason = (f"Short PF {profit_factor:.2f} > 1.5 over {len(rows)} trades "
                  f"— increase short cap toward long cap")
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "short_max_position_pct_up",
            "short_max_position_pct", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised {_label('short_max_position_pct')} from "
                f"{current:.0%} to {applied:.0%}{suffix} ({reason})")

    # PF < 0.8 with >=15 trades: shrink short cap, the edge isn't there
    if profit_factor < 0.8 and len(rows) >= 15:
        new_val = _bound("short_max_position_pct",
                          round(max(0.01, current * 0.7), 4))
        if new_val >= current:
            return None
        reason = (f"Short PF {profit_factor:.2f} < 0.8 over {len(rows)} trades "
                  f"— shrink position size while edge is unproven")
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "short_max_position_pct_down",
            "short_max_position_pct", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Reduced {_label('short_max_position_pct')} from "
                f"{current:.0%} to {applied:.0%}{suffix} ({reason})")
    return None


def _optimize_short_max_hold_days(conn, ctx, profile_id, user_id,
                                    overall_wr, resolved):
    """Tune short_max_hold_days from observed short hold-time distribution.

    P1.9b of LONG_SHORT_PLAN.md. If most winning shorts close in
    <5 days, tighten the time stop (recycle losers faster). If
    many were force-covered by the time stop with positive pnl,
    loosen (we were giving up on winners too early).
    """
    if not _safe_change_guarded(profile_id, "short_max_hold_days"):
        return None
    if not getattr(ctx, "enable_short_selling", False):
        return None
    # Look at short-cover rows (status=closed, side=cover) — these
    # are the actual completed shorts.
    rows = conn.execute(
        "SELECT pnl, timestamp FROM trades "
        "WHERE pnl IS NOT NULL AND side = 'cover' "
        "ORDER BY timestamp DESC LIMIT 100"
    ).fetchall()
    if len(rows) < 15:
        return None
    # Without entry timestamps we can't compute hold days here
    # without joining. Skip the tuning rule for now and let
    # P1.10's MFE data + a future days_held column support it.
    return None


def _optimize_short_take_profit(conn, ctx, profile_id, user_id,
                                  overall_wr, resolved):
    """Analogous to take_profit_pct rule but for shorts.
    Tighten when shorts hit TP and reversed (gave back gains);
    loosen when shorts ran well past TP before exit."""
    if not _safe_change_guarded(profile_id, "short_take_profit_pct"):
        return None
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None
    rows = conn.execute(
        "SELECT pnl, take_profit, price FROM trades "
        "WHERE pnl IS NOT NULL AND side IN ('short','cover') "
        "  AND take_profit IS NOT NULL AND price > 0"
    ).fetchall()
    if len(rows) < 5:
        return None
    current = float(getattr(ctx, "short_take_profit_pct", 0.08))
    # Compute average winner % vs TP target
    winning_pcts = []
    for r in rows:
        if r["pnl"] > 0 and r["take_profit"] and r["price"]:
            # For shorts, profit = (entry_price - cover_price) / entry_price
            # We approximate via pnl / (price * abs_qty), but simpler:
            # compare distance traveled to target distance.
            target_dist = abs(r["take_profit"] - r["price"]) / r["price"]
            if target_dist > 0:
                # We don't have the exit price, but pnl scaled by qty * price
                # gives % gain. Use a simpler heuristic: assume avg_winner_pct
                # = current * fraction_of_target_hit. With most TPs hit
                # exactly, fraction ≈ 1.0; lower fractions mean we exited early.
                # For tuning purposes: if many winners had small pnl relative
                # to target, the TP was too ambitious.
                winning_pcts.append(target_dist)
    if not winning_pcts:
        return None
    avg = sum(winning_pcts) / len(winning_pcts)
    # If average winner achieved < 50% of target, tighten TP (capture sooner)
    if avg < current * 0.5:
        new_val = _bound("short_take_profit_pct", round(current * 0.8, 4))
        if new_val >= current:
            return None
        reason = (
            f"Short TP avg {avg*100:.1f}% < 50% of target "
            f"{current*100:.1f}% — tighten to capture sooner"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "short_take_profit_tighten",
            "short_take_profit_pct", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Tightened {_label('short_take_profit_pct')} from {current:.0%} to "
                f"{applied:.0%}{suffix} ({reason})")
    return None


def _optimize_atr_multiplier_sl(conn, ctx, profile_id, user_id,
                                 overall_wr, resolved):
    """Widen ATR-based stop-loss when stops are being hit too tightly
    (>40% of losing trades cluster within 0.2× ATR of the stop). Only
    applies when use_atr_stops is on for the profile."""
    if not getattr(ctx, "use_atr_stops", True):
        return None
    if not _safe_change_guarded(profile_id, "atr_multiplier_sl"):
        return None
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None
    # Heuristic without per-trade ATR: count losers where pnl was very
    # close to the stop's expected loss size. Without ATR per trade we
    # rely on a simpler proxy: many losses with small magnitude (-1 to -2%)
    # vs current stop suggests too-tight stops.
    rows = conn.execute(
        "SELECT pnl, price, qty FROM trades "
        "WHERE pnl IS NOT NULL AND pnl < 0 AND price > 0 AND qty > 0"
    ).fetchall()
    if len(rows) < 10:
        return None
    # Compute % loss per trade
    losses_pct = [abs(r["pnl"]) / (r["price"] * abs(r["qty"]))
                  for r in rows if r["price"] * abs(r["qty"]) > 0]
    if not losses_pct:
        return None
    # Count losses at the "near-stop" band — magnitude within 20% of
    # the largest loss (cluster at the stop).
    max_loss = max(losses_pct)
    if max_loss <= 0:
        return None
    near_stop = sum(1 for p in losses_pct if p >= max_loss * 0.8)
    near_stop_rate = near_stop / len(losses_pct)
    current = float(getattr(ctx, "atr_multiplier_sl", 2.0))
    if near_stop_rate >= 0.4:
        new_val = _bound("atr_multiplier_sl", round(current + 0.25, 2))
        if new_val == current:
            return None
        reason = (
            f"{near_stop_rate:.0%} of losses cluster near the stop — "
            f"widen ATR-stop multiplier to give trades more room"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "atr_sl_widen",
            "atr_multiplier_sl", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Widened {_label('atr_multiplier_sl')} from {current:.2f} to "
                f"{applied:.2f}{suffix} ({reason})")
    return None


def _optimize_atr_multiplier_tp(conn, ctx, profile_id, user_id,
                                 overall_wr, resolved):
    """Tighten ATR take-profit when avg winner captures < 50% of the
    target distance. Mirror of the existing take_profit_pct rule."""
    if not getattr(ctx, "use_atr_stops", True):
        return None
    if not _safe_change_guarded(profile_id, "atr_multiplier_tp"):
        return None
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None
    rows = conn.execute(
        "SELECT pnl, price, qty FROM trades "
        "WHERE pnl IS NOT NULL AND pnl > 0 AND price > 0 AND qty > 0"
    ).fetchall()
    if len(rows) < 10:
        return None
    wins_pct = [r["pnl"] / (r["price"] * abs(r["qty"]))
                for r in rows if r["price"] * abs(r["qty"]) > 0]
    if not wins_pct:
        return None
    max_win = max(wins_pct)
    if max_win <= 0:
        return None
    avg_win = sum(wins_pct) / len(wins_pct)
    # If avg winner is well below the largest winner achievable, the TP
    # may be set too far — tighten so we capture more consistent wins.
    if avg_win < max_win * 0.5:
        current = float(getattr(ctx, "atr_multiplier_tp", 3.0))
        new_val = _bound("atr_multiplier_tp", round(current - 0.25, 2))
        if new_val >= current:
            return None
        reason = (
            f"Avg winner {avg_win*100:.1f}% well under best winner "
            f"{max_win*100:.1f}% — tighten ATR-TP to capture more"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "atr_tp_tighten",
            "atr_multiplier_tp", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Tightened {_label('atr_multiplier_tp')} from {current:.2f} to "
                f"{applied:.2f}{suffix} ({reason})")
    return None


def _optimize_trailing_atr_multiplier(conn, ctx, profile_id, user_id,
                                        overall_wr, resolved):
    """Tighten the trailing-stop multiplier when winning trades give
    back too much of their peak gain before exit; loosen when
    winners are getting whipsawed out close to their peak.

    Reads `max_favorable_excursion` (MFE) from the trades table —
    populated by `trader.check_exits` MFE updater every cycle.
    Computes give-back-pct per closed long trade as:
        (mfe - exit_fill_price) / mfe × 100

    Aggregates over the last 50+ closed long trades, compares
    average give-back to a sensible band:
    - > 50% give-back avg → tighten (current trailing is too loose,
      letting too much profit evaporate)
    - < 10% give-back avg AND avg pnl_pct positive → loosen
      (winners are exiting near peak, but maybe we're whipsawing
      out too early; let them run)

    Self-skips when fewer than 30 long trades have non-null MFE
    (data accumulating).
    """
    if not getattr(ctx, "use_trailing_stops", True):
        return None
    if not _safe_change_guarded(profile_id, "trailing_atr_multiplier"):
        return None
    current = float(getattr(ctx, "trailing_atr_multiplier", 1.5) or 1.5)
    try:
        rows = conn.execute(
            "SELECT max_favorable_excursion, fill_price, price, pnl "
            "FROM trades "
            "WHERE side = 'sell' AND status = 'closed' "
            "AND max_favorable_excursion IS NOT NULL "
            "AND max_favorable_excursion > 0 "
            "ORDER BY id DESC LIMIT 100"
        ).fetchall()
    except Exception:
        return None

    samples = []  # list of (give_back_pct, pnl)
    for mfe, fill_price, price, pnl in rows:
        # The "exit" price for a sell trade is fill_price (or price if
        # fill missing). MFE is the high-water mark during the position.
        exit_px = fill_price or price
        if not exit_px or exit_px <= 0:
            continue
        if mfe <= exit_px:
            # Position never went in our favor — give-back is 0 or
            # negative; skip from the give-back analysis.
            continue
        give_back_pct = (mfe - exit_px) / mfe * 100
        samples.append((give_back_pct, pnl or 0.0))

    if len(samples) < 30:
        return None

    avg_give_back = sum(g for g, _ in samples) / len(samples)
    avg_pnl = sum(p for _, p in samples) / len(samples)

    # Tighten when average give-back is excessive (winners are
    # evaporating before exit).
    if avg_give_back > 50.0:
        new_val = _bound("trailing_atr_multiplier",
                          round(max(0.5, current * 0.85), 2))
        if new_val == current:
            return None
        reason = (
            f"Avg give-back from peak {avg_give_back:.0f}% on "
            f"{len(samples)} closed longs — too much profit "
            f"evaporates before trailing stop fires"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "trailing_tighten",
            "trailing_atr_multiplier", current, new_val, reason,
            win_rate_at_change=overall_wr,
            predictions_resolved=resolved,
        )
        return (
            f"Tightened {_label('trailing_atr_multiplier')} from "
            f"{current:.2f} to {applied:.2f}{suffix} ({reason})"
        )

    # Loosen when give-back is small AND winners are still profitable
    # — trailing might be cutting them off too soon.
    if avg_give_back < 10.0 and avg_pnl > 0 and current < 3.0:
        new_val = _bound("trailing_atr_multiplier",
                          round(min(3.0, current * 1.15), 2))
        if new_val == current:
            return None
        reason = (
            f"Avg give-back {avg_give_back:.0f}% on {len(samples)} "
            f"closed longs — winners exiting near peak; loosen to "
            f"let them run"
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "trailing_loosen",
            "trailing_atr_multiplier", current, new_val, reason,
            win_rate_at_change=overall_wr,
            predictions_resolved=resolved,
        )
        return (
            f"Loosened {_label('trailing_atr_multiplier')} from "
            f"{current:.2f} to {applied:.2f}{suffix} ({reason})"
        )
    return None


def _optimize_stop_to_tp_ratio(conn, ctx, profile_id, user_id,
                                 overall_wr, resolved):
    """When the exit-strategy distribution skews way more toward
    stops than take-profits, the reward/risk asymmetry is inverted —
    losers run to the stop while winners get cut short by the
    trailing/fixed stop. Widens the ATR stop-loss multiplier AND
    tightens the ATR take-profit multiplier in the same pass so
    the next batch of trades has wider stops + closer TPs.

    Counts the `strategy` column on closed exit rows in the last
    N days:
      stops = strategy IN ('stop_loss', 'trailing_stop',
                            'short_stop_loss')
      tps   = strategy IN ('take_profit', 'short_take_profit')

    Acceptable band: 0.5 ≤ stops/tps ≤ 2.5. Outside that band,
    adjust both multipliers in the same direction the asymmetry
    needs to flip.

    Needs ≥30 closed exits with strategy attribution to fire
    (small samples are noise). 2026-05-12.
    """
    # Both multipliers must be in scope for this to mean anything —
    # short-circuit if ATR stops are off.
    if not getattr(ctx, "use_atr_stops", True):
        return None
    # Cooldown guard on BOTH targets — don't thrash either if
    # another rule touched it recently.
    if not _safe_change_guarded(profile_id, "atr_multiplier_sl"):
        return None
    if not _safe_change_guarded(profile_id, "atr_multiplier_tp"):
        return None
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None

    # 2026-05-12 — data_quality filter so phantom-stop incidents
    # don't drive the asymmetry calculation (a polluted SELL row
    # with strategy='stop_loss' would inflate the stop count).
    from journal import data_quality_clause
    _dq = data_quality_clause(conn)
    rows = conn.execute(
        f"""SELECT strategy, COUNT(*) as n FROM trades
            WHERE strategy IS NOT NULL
              AND side IN ('sell', 'cover')
              AND status = 'closed'
              AND timestamp >= datetime('now', '-30 days')
              {_dq}
            GROUP BY strategy"""
    ).fetchall()
    counts = {r[0]: r[1] for r in rows}
    stop_strats = ("stop_loss", "trailing_stop", "short_stop_loss")
    tp_strats = ("take_profit", "short_take_profit")
    stops = sum(counts.get(s, 0) for s in stop_strats)
    tps = sum(counts.get(s, 0) for s in tp_strats)
    total_attributed = stops + tps
    if total_attributed < 30:
        return None  # not enough signal

    ratio = stops / tps if tps > 0 else float("inf")

    # Acceptable band: 0.5 - 2.5. Tighter than the observed 4.5:1
    # but wider than 1:1 so the rule doesn't fire on noise.
    if 0.5 <= ratio <= 2.5:
        return None

    current_sl = float(getattr(ctx, "atr_multiplier_sl", 2.0))
    current_tp = float(getattr(ctx, "atr_multiplier_tp", 3.0))

    if ratio > 2.5:
        # Too many stops, not enough TPs. Wider stops (give trades
        # more room) + tighter TPs (lock in winners sooner).
        new_sl = _bound("atr_multiplier_sl", round(current_sl * 1.15, 2))
        new_tp = _bound("atr_multiplier_tp", round(current_tp * 0.90, 2))
        direction = "widen SL + tighten TP"
    else:  # ratio < 0.5
        # Way more TPs than stops — could be loosening too much.
        # Tighten the SL slightly + loosen TP slightly.
        new_sl = _bound("atr_multiplier_sl", round(current_sl * 0.90, 2))
        new_tp = _bound("atr_multiplier_tp", round(current_tp * 1.10, 2))
        direction = "tighten SL + loosen TP"

    changed_any = False
    reason = (
        f"stop-to-TP ratio {ratio:.1f} over {total_attributed} "
        f"exits (stops={stops}, tps={tps}) — {direction}"
    )
    applied_sl = current_sl
    applied_tp = current_tp
    if abs(new_sl - current_sl) > 1e-9:
        applied_sl, _, _ = _apply_param_change(
            profile_id, user_id, "stop_to_tp_rebalance",
            "atr_multiplier_sl", current_sl, new_sl, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        changed_any = True
    if abs(new_tp - current_tp) > 1e-9:
        applied_tp, _, _ = _apply_param_change(
            profile_id, user_id, "stop_to_tp_rebalance",
            "atr_multiplier_tp", current_tp, new_tp, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        changed_any = True

    if not changed_any:
        return None
    return (
        f"Stop-to-TP rebalance: ratio={ratio:.1f}, "
        f"SL {current_sl:.2f}→{applied_sl:.2f}, "
        f"TP {current_tp:.2f}→{applied_tp:.2f} "
        f"(stops={stops}, tps={tps}, {direction})"
    )


def _optimize_conviction_tp_override(conn, ctx, profile_id, user_id,
                                       overall_wr, resolved):
    """Auto-flip `use_conviction_tp_override` per profile based on
    the data. Replaces operator-set toggle with AI-driven decision.

    The flag, when ON, skips fixed take-profit firing for runaway
    winners (AI confidence ≥ 70 + ADX ≥ 25 + new highs all required)
    and lets the trailing stop manage the exit. Default flipped ON
    2026-05-12 after audit showed UNH-style trades being capped at
    initial AI targets while underlying ran 4-5% further.

    Decision rule:
      Flip ON when:  MFE capture < 50% AND stop-to-TP ratio > 1.5
                     (capping winners; let trailing stop manage)
      Flip OFF when: MFE capture > 70% AND stop-to-TP ratio < 1.5
                     (already capturing well; disciplined TP wins)

    Otherwise: no change. Avoid thrashing the flag on weak signal.

    2026-05-12.
    """
    if not _safe_change_guarded(profile_id, "use_conviction_tp_override"):
        return None
    db_path = getattr(ctx, "db_path", None)
    if not db_path:
        return None
    try:
        from mfe_capture import (
            compute_capture_ratio, compute_stop_to_tp_ratio,
        )
        cap = compute_capture_ratio(db_path, lookback=50)
        s2t = compute_stop_to_tp_ratio(db_path, window_days=30)
    except Exception:
        return None
    if not cap or not s2t:
        return None

    cap_pct = (cap.get("avg_capture_ratio") or 0) * 100
    n_trades = cap.get("n_trades") or 0
    ratio = s2t.get("ratio")
    if ratio is None or n_trades < 20:
        return None

    current = bool(getattr(ctx, "use_conviction_tp_override", False))

    # Flip ON: winners capped + stop-to-TP imbalanced.
    if not current and cap_pct < 50 and ratio > 1.5:
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, use_conviction_tp_override=1)
        reason = (
            f"MFE capture {cap_pct:.0f}% over {n_trades} trades + "
            f"stop-to-TP {ratio:.1f} — fixed TP is capping runaway "
            f"winners. Enabling override so trailing stop manages "
            f"exits on high-conviction names."
        )
        log_tuning_change(
            profile_id, user_id, "conviction_tp_enable",
            "use_conviction_tp_override", "0", "1", reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Enabled {_label('use_conviction_tp_override')} ({reason})"
        )

    # Flip OFF: already capturing well + stop-to-TP balanced.
    if current and cap_pct > 70 and ratio < 1.5:
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, use_conviction_tp_override=0)
        reason = (
            f"MFE capture {cap_pct:.0f}% over {n_trades} trades + "
            f"stop-to-TP {ratio:.1f} — disciplined fixed TP is "
            f"winning. Disabling override so we lock in gains at "
            f"the AI target."
        )
        log_tuning_change(
            profile_id, user_id, "conviction_tp_disable",
            "use_conviction_tp_override", "1", "0", reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Disabled {_label('use_conviction_tp_override')} ({reason})"
        )

    return None


def _optimize_options_pnl_cutoff(conn, ctx, profile_id, user_id,
                                   overall_wr, resolved):
    """Auto-flip `enable_options` based on rolling options-bucket P&L.

    Direct response to the 2026-05-13 episode where the system lost
    $200K+ on options because nothing made the bleeding visible to
    the AI; without a cutoff, options trading just kept running.

    Decision rule:
      DISABLE: ≥10 closed options trades in the last 30 days AND
               total realized P&L < -3% of initial_capital
      ENABLE:  enable_options has been OFF for ≥14 days
               (auto-expiry per the "self-tuner must drift toward
                confident trading" memory — no permanent off-state)
      Otherwise: no change.

    Skips when profile.is_virtual is False (broker-direct profiles
    have separate options governance) OR profile is crypto.
    """
    if not _safe_change_guarded(profile_id, "enable_options"):
        return None
    segment = (getattr(ctx, "segment", "") or "").lower()
    if "crypto" in segment:
        return None

    current = bool(getattr(ctx, "enable_options", True))
    initial_capital = float(getattr(ctx, "initial_capital", 100_000.0) or 100_000.0)
    if initial_capital <= 0:
        return None

    # Auto-re-enable branch: if it's been off for ≥14 days, flip
    # back on so the system can prove options are tradeable again.
    if not current:
        last_off = _get_recent_adjustment(
            profile_id, "enable_options", days=14,
        )
        if last_off is None:
            from models import update_trading_profile, log_tuning_change
            update_trading_profile(profile_id, enable_options=1)
            reason = (
                "Auto-re-enabling options: been disabled ≥14 days, "
                "drifting back to trading per the self-tuner's "
                "confident-trading bias. Will re-disable if rolling "
                "options P&L bleeds again."
            )
            log_tuning_change(
                profile_id, user_id, "options_re_enable",
                "enable_options", "0", "1", reason,
                win_rate_at_change=overall_wr,
                predictions_resolved=resolved,
            )
            return reason
        return None

    # Disable branch — need data. Filter data_quality-tagged rows
    # so a phantom-stop-class incident can't bias the cutoff
    # signal (the standing rule: corrupt rows never pool into
    # decision-driving aggregations).
    from journal import data_quality_clause
    _dq = data_quality_clause(conn, table="trades")
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0), COUNT(*) FROM trades "
            "WHERE status = 'closed' "
            "  AND occ_symbol IS NOT NULL "
            "  AND pnl IS NOT NULL "
            f"  AND timestamp >= datetime('now', '-30 days'){_dq}"
        ).fetchone()
    except Exception as exc:
        logger.warning(
            "options_pnl_cutoff: query failed for profile %s: %s",
            profile_id, exc,
        )
        return None

    if not row:
        return None
    options_pnl = float(row[0] or 0)
    options_count = int(row[1] or 0)
    if options_count < 10:
        return None  # insufficient data
    threshold_dollars = -0.03 * initial_capital
    if options_pnl >= threshold_dollars:
        return None  # within tolerance

    # Cooldown / prior-outcome guards (shared with other optimizers)
    if _get_recent_adjustment(profile_id, "enable_options", days=3):
        return None
    if _was_adjustment_effective(profile_id, "enable_options") == "worsened":
        return None

    from models import update_trading_profile, log_tuning_change
    update_trading_profile(profile_id, enable_options=0)
    pct_of_cap = (options_pnl / initial_capital) * 100.0
    reason = (
        f"30-day options realized P&L ${options_pnl:+,.2f} "
        f"({pct_of_cap:+.2f}% of capital) over {options_count} "
        f"closed contracts — below -3% threshold. Disabling new "
        f"options entries; will auto-re-enable in 14 days."
    )
    log_tuning_change(
        profile_id, user_id, "options_pnl_cutoff",
        "enable_options", "1", "0", reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return reason


def _optimize_short_selling_toggle(conn, ctx, profile_id, user_id,
                                     overall_wr, resolved):
    """Auto-flip `enable_short_selling` per profile based on the
    profile's actual short-side track record.

    Default flipped ON 2026-05-12 (was OFF since launch). Audit
    showed shorts had +4.04% avg return on 40 resolved predictions.
    But profile-level performance varies — some profiles may be bad
    at shorting their universe. This rule is the safety net: flip
    OFF when 30-day short-side avg return is materially negative
    AND sample size is sufficient.

    Decision rule:
      Flip OFF when: ≥10 resolved SHORT/STRONG_SELL predictions in
                     the last 30 days AND avg_return_pct < -0.5
      Flip ON  when: ≥10 resolved short predictions AND
                     avg_return_pct > +1.0 (proves capable)
      Otherwise: no change.

    Skips when profile is a crypto profile (can't short crypto).

    2026-05-12.
    """
    if not _safe_change_guarded(profile_id, "enable_short_selling"):
        return None
    # Don't fire on crypto profiles — Alpaca crypto can't short.
    segment = (getattr(ctx, "segment", "") or "").lower()
    if "crypto" in segment:
        return None
    try:
        rows = conn.execute(
            "SELECT actual_outcome, actual_return_pct FROM ai_predictions "
            "WHERE status = 'resolved' "
            "AND predicted_signal IN ('SHORT', 'STRONG_SELL', 'SELL') "
            "AND resolved_at IS NOT NULL "
            "AND resolved_at >= datetime('now', '-30 days')"
        ).fetchall()
    except Exception:
        return None
    rows = [(o, r) for o, r in rows
            if r is not None and abs(float(r)) < 100]
    if len(rows) < 10:
        return None
    returns = [float(r) for _, r in rows]
    avg_return = sum(returns) / len(returns)

    current = bool(getattr(ctx, "enable_short_selling", False))

    if current and avg_return < -0.5:
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, enable_short_selling=0)
        reason = (
            f"30-day short-side avg return {avg_return:+.2f}% over "
            f"{len(rows)} predictions — profile is losing on shorts. "
            f"Disabling new short opens."
        )
        log_tuning_change(
            profile_id, user_id, "short_selling_disable",
            "enable_short_selling", "1", "0", reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Disabled {_label('enable_short_selling')} ({reason})"
        )

    if not current and avg_return > 1.0:
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, enable_short_selling=1)
        reason = (
            f"30-day short-side avg return {avg_return:+.2f}% over "
            f"{len(rows)} predictions — profile is profitable on "
            f"shorts. Enabling new short opens."
        )
        log_tuning_change(
            profile_id, user_id, "short_selling_enable",
            "enable_short_selling", "0", "1", reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Enabled {_label('enable_short_selling')} ({reason})"
        )
    return None


def _optimize_fast_lane_retirement(conn, ctx, profile_id, user_id,
                                     overall_wr, resolved):
    """Fast-lane strategy retirement (2026-05-12).

    Existing alpha_decay deprecates strategies after 30+ days of
    Sharpe degradation — too slow for clearly-broken strategies.
    Example: `mean_reversion` was 0% win rate on 10 recent trades,
    bleeding money the entire time, and alpha_decay hadn't caught
    it because the lifetime Sharpe baseline includes older
    profitable periods.

    This rule runs daily on every profile, computes per-strategy
    rolling 10-trade win rate from resolved predictions, and:

      - DEPRECATE strategies whose 10-trade rolling wr < 25%
        AND samples ≥ 10. Tagged with reason
        "fast_lane: rolling-10 wr <25%" so it can be distinguished
        from alpha_decay deprecations.
      - RESTORE strategies that were deprecated by THIS rule (tag
        check) more than 14 days ago. Lets them re-prove
        themselves with fresh trades. Alpha-decay-tagged
        deprecations are left alone (they have their own
        Sharpe-recovery restore path).

    Independent of alpha_decay; the two systems run on different
    signals (fast wr vs slow Sharpe) and tag their deprecations
    distinctly so neither steps on the other.

    AI-tunable: threshold (25%), min samples (10), reactivation
    window (14d). Defaults chosen from the data audit.
    """
    if not _safe_change_guarded(profile_id, "fast_lane_retirement"):
        return None
    db_path = getattr(ctx, "db_path", None)
    if not db_path:
        return None
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='deprecated_strategies'"
        ).fetchone()
        if not rows:
            return None
    except Exception:
        return None

    actions = []  # human-readable messages

    # --- AUTO-RESTORE fast-lane-tagged deprecations older than 14d
    try:
        restored_rows = conn.execute(
            "SELECT strategy_type FROM deprecated_strategies "
            "WHERE restored_at IS NULL "
            "  AND reason LIKE 'fast_lane:%' "
            "  AND deprecated_at <= datetime('now', '-14 days')"
        ).fetchall()
    except Exception:
        restored_rows = []
    for (st,) in restored_rows:
        try:
            from alpha_decay import restore_strategy
            from models import log_tuning_change
            restore_strategy(db_path, st)
            log_tuning_change(
                profile_id, user_id, "fast_lane_restore",
                "strategy_active", st, st,
                f"Fast-lane deprecation aged 14 days; restoring "
                f"{st} for re-evaluation",
                win_rate_at_change=overall_wr,
                predictions_resolved=resolved,
            )
            actions.append(f"Restored {st} (14d aged)")
        except (ImportError, sqlite3.OperationalError, sqlite3.DatabaseError,
                AttributeError, KeyError, OSError) as _rs_exc:
            # Per-strategy restore loop; one failed restore shouldn't
            # kill the loop. Surface for follow-up.
            logger.debug(
                "fast_lane restore failed for %s: %s: %s",
                st, type(_rs_exc).__name__, _rs_exc,
            )
            continue

    # --- DEPRECATE strategies with rolling 10-trade wr < 25%
    try:
        strat_rows = conn.execute(
            "SELECT strategy_type, "
            "   SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) AS wins, "
            "   COUNT(*) AS n "
            "FROM ("
            "   SELECT strategy_type, actual_outcome FROM ai_predictions "
            "   WHERE status='resolved' "
            "     AND actual_outcome IN ('win', 'loss') "
            "     AND strategy_type IS NOT NULL AND strategy_type != '' "
            "   ORDER BY resolved_at DESC, id DESC"
            ") "
            "GROUP BY strategy_type"
        ).fetchall()
    except Exception:
        strat_rows = []

    # For each strategy, examine ONLY the last 10 resolved predictions
    # (not the full GROUP BY which counts all). Need a second query
    # per strategy. Skip strategies with <10 samples or rows
    # already fast-lane-deprecated (or alpha-decay deprecated).
    for st, _wins_full, _n_full in strat_rows:
        if not st:
            continue
        # Already deprecated? Skip (either by us or alpha_decay).
        try:
            dep = conn.execute(
                "SELECT 1 FROM deprecated_strategies "
                "WHERE strategy_type=? AND restored_at IS NULL",
                (st,),
            ).fetchone()
        except Exception:
            dep = None
        if dep:
            continue

        try:
            last10 = conn.execute(
                "SELECT actual_outcome FROM ai_predictions "
                "WHERE strategy_type=? "
                "  AND status='resolved' "
                "  AND actual_outcome IN ('win', 'loss') "
                "ORDER BY resolved_at DESC, id DESC LIMIT 10",
                (st,),
            ).fetchall()
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as _ls_exc:
            # Per-strategy last-10 lookup loop; one bad query
            # shouldn't kill the loop. Surface for follow-up.
            logger.debug(
                "fast_lane last10 lookup failed for %s: %s: %s",
                st, type(_ls_exc).__name__, _ls_exc,
            )
            continue
        if len(last10) < 10:
            continue
        wins = sum(1 for (o,) in last10 if o == "win")
        wr = wins / len(last10) * 100
        if wr >= 25:
            continue  # acceptable

        try:
            from alpha_decay import deprecate_strategy
            from models import log_tuning_change
            deprecate_strategy(
                db_path, st,
                {"reason": f"fast_lane: rolling-10 wr {wr:.0f}%",
                 "current_rolling_sharpe": None,
                 "lifetime_sharpe": None,
                 "consecutive_bad_days": 0},
            )
            log_tuning_change(
                profile_id, user_id, "fast_lane_deprecate",
                "strategy_active", st,
                f"{st} (deprecated 14d)",
                f"Rolling-10 wr {wr:.0f}% < 25% — auto-deprecating "
                f"for 14 days. Auto-restore after the cool-off.",
                win_rate_at_change=overall_wr,
                predictions_resolved=resolved,
            )
            actions.append(f"Deprecated {st} (rolling-10 wr {wr:.0f}%)")
        except (ImportError, sqlite3.OperationalError, sqlite3.DatabaseError,
                AttributeError, KeyError, OSError) as _dp_exc:
            # Per-strategy deprecation loop; one failed call
            # shouldn't kill the loop. Surface for follow-up.
            logger.debug(
                "fast_lane deprecation failed for %s: %s: %s",
                st, type(_dp_exc).__name__, _dp_exc,
            )
            continue

    if not actions:
        return None
    return "Fast-lane retirement: " + "; ".join(actions[:3]) + (
        f" (+{len(actions)-3} more)" if len(actions) > 3 else ""
    )


def _optimize_stop_out_blacklist(conn, ctx, profile_id, user_id,
                                   overall_wr, resolved):
    """Per-symbol stop-out blacklist (2026-05-12 — Wave 8c).

    For each symbol in this profile's recent trade history, count
    stop-out exits in the last 30 days. When count ≥ 3, add the
    symbol to `entry_blacklist` for 14 days. Stops the system from
    re-entering names that aren't working in the current regime.

    Stop-out exits = strategy IN ('stop_loss', 'trailing_stop',
    'short_stop_loss'). data_quality-tagged rows are excluded so
    phantom-stop incidents can never drive the blacklist.

    Idempotent — if a symbol already has an active blacklist entry,
    re-adding refreshes the 14-day window. Auto-expiry happens on
    the read path in `entry_blacklist.parse_blacklist`.

    AI-tunable: stop-out threshold count (3), window (30 days),
    cool-off (14 days). These are defaults for v1.
    """
    if not _safe_change_guarded(profile_id, "entry_blacklist"):
        return None
    db_path = getattr(ctx, "db_path", None)
    if not db_path:
        return None
    from journal import data_quality_clause
    _dq = data_quality_clause(conn)
    try:
        # DISPLAY_ONLY: 3 = blacklist definition (3 stop-outs in 30
        # days = blacklistable per AI-tunable rule), not a sample-size
        # for tightening evidence.
        rows = conn.execute(
            f"""SELECT symbol, COUNT(*) AS n FROM trades
                WHERE strategy IN ('stop_loss', 'trailing_stop',
                                    'short_stop_loss')
                  AND side IN ('sell', 'cover')
                  AND status = 'closed'
                  AND timestamp >= datetime('now', '-30 days')
                  AND symbol IS NOT NULL
                  {_dq}
                GROUP BY symbol
                HAVING COUNT(*) >= 3"""
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None

    # Read existing blacklist so we don't double-log "added" for
    # symbols that are already blacklisted (refresh is fine; log noise
    # isn't).
    try:
        from entry_blacklist import (
            parse_blacklist, add_to_blacklist,
        )
    except Exception:
        return None
    raw = getattr(ctx, "entry_blacklist", None) or "{}"
    existing = set(parse_blacklist(raw).keys())

    # Build the new blacklist dict in-memory then write ONCE via
    # update_trading_profile. The direct call makes this column's
    # tuning visible to the structural guardrail test
    # (tests/test_every_lever_is_tuned.py scans for
    # `update_trading_profile(pid, <col>=...)` patterns).
    import json
    from datetime import datetime, timedelta
    new_bl = dict(parse_blacklist(raw))  # active entries only
    added = []
    refreshed = []
    expiry = (datetime.utcnow() + timedelta(days=14)).isoformat()
    for sym, n in rows:
        sym_u = (sym or "").upper()
        if not sym_u:
            continue
        new_bl[sym_u] = expiry
        if sym_u in existing:
            refreshed.append((sym_u, n))
        else:
            added.append((sym_u, n))
    try:
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(
            profile_id, entry_blacklist=json.dumps(new_bl),
        )
        for sym_u, n in added:
            try:
                log_tuning_change(
                    profile_id, user_id, "stop_out_blacklist_add",
                    "entry_blacklist", "", sym_u,
                    f"{sym_u} stopped out {n}× in last 30 days — "
                    f"blacklisted for 14 days. Auto-expires.",
                    win_rate_at_change=overall_wr,
                    predictions_resolved=resolved,
                )
            except (sqlite3.OperationalError, sqlite3.DatabaseError,
                    OSError) as _au_exc:
                # Per-symbol audit-log loop; one failed write
                # shouldn't kill the loop. Surface for follow-up.
                logger.debug(
                    "stop_out_blacklist audit log write failed for %s: %s: %s",
                    sym_u, type(_au_exc).__name__, _au_exc,
                )
                continue
    except Exception:
        return None

    if not added and not refreshed:
        return None
    msg_parts = []
    if added:
        msg_parts.append(
            f"Blacklisted {len(added)}: "
            + ", ".join(f"{s}({n}×)" for s, n in added[:5])
        )
    if refreshed:
        msg_parts.append(f"refreshed {len(refreshed)}")
    return "Stop-out blacklist: " + "; ".join(msg_parts)


def _optimize_meta_pregate_threshold(conn, ctx, profile_id, user_id,
                                       overall_wr, resolved):
    """Auto-tune `meta_pregate_threshold` per profile based on the
    observed actionable-signal ratio (2026-05-13 — Wave 9a).

    The audit on 2026-05-13 found that the launch default (0.5) was
    structurally over-filtering: 68% of 1,985 candidates evaluated
    across 139 cycles got dropped before the AI ever saw them. The
    AI then "selected 0 trades" because the choices were
    pre-filtered, not because it judged them poor.

    Tuning signal: actionable-signal ratio over the last 5 days =
    (predictions where predicted_signal != 'HOLD') / total predictions.

    Decision rule:
      ratio < 5%  → LOWER threshold by 0.05 (filter is too tight;
                    AI never gets actionable candidates)
      ratio > 30% → RAISE threshold by 0.05 (filter is too loose;
                    can sharpen the cohort)
      5%-30%     → no change. Healthy operating band.

    Bounds: 0.15 (floor — essentially no filtering) to 0.70
    (ceiling — extreme selectivity). Per-profile cooldown via
    `_safe_change_guarded` prevents thrash.

    Needs ≥50 resolved+pending predictions in the 5-day window for
    a stable signal; below that, no change.
    """
    if not _safe_change_guarded(profile_id, "meta_pregate_threshold"):
        return None
    try:
        rows = conn.execute(
            "SELECT predicted_signal FROM ai_predictions "
            "WHERE timestamp >= datetime('now', '-5 days')"
        ).fetchall()
    except Exception:
        return None
    if len(rows) < 50:
        return None
    total = len(rows)
    actionable = sum(
        1 for (s,) in rows
        if s and s.upper() != "HOLD"
    )
    ratio = actionable / total * 100

    # Healthy band: 5%-30% actionable.
    current = float(getattr(ctx, "meta_pregate_threshold", 0.35) or 0.35)
    if 5.0 <= ratio <= 30.0:
        return None

    if ratio < 5.0:
        new_val = _bound("meta_pregate_threshold",
                          round(max(0.15, current - 0.05), 2))
        if new_val >= current:
            return None
        direction = "lowered"
        why = (
            f"Actionable-signal ratio {ratio:.1f}% over {total} "
            f"recent predictions — pre-AI filter is too tight; "
            f"loosen to let more candidates through"
        )
    else:  # ratio > 30
        new_val = _bound("meta_pregate_threshold",
                          round(min(0.70, current + 0.05), 2))
        if new_val <= current:
            return None
        direction = "raised"
        why = (
            f"Actionable-signal ratio {ratio:.1f}% over {total} "
            f"recent predictions — AI is firing aggressively; "
            f"sharpen the cohort with a tighter pre-filter"
        )
    applied, _, suffix = _apply_param_change(
        profile_id, user_id,
        ("meta_pregate_lower" if direction == "lowered"
         else "meta_pregate_raise"),
        "meta_pregate_threshold", current, new_val, why,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return (
        f"{direction.capitalize()} "
        f"{_label('meta_pregate_threshold')} from "
        f"{current:.2f} to {applied:.2f}{suffix} ({why})"
    )


def _optimize_skip_first_minutes_slippage(conn, ctx, profile_id, user_id,
                                            overall_wr, resolved):
    """Auto-adjust `skip_first_minutes` based on observed slippage
    in the first 15 minutes after market open vs rest-of-day.

    Default bumped 0→5 minutes on 2026-05-12; this rule tunes that
    per profile. Trades that filled inside the first 15 minutes are
    compared against trades filled later. When first-15-min slippage
    is materially worse, widen the skip window. When it's not,
    tighten back.

    Complements the existing win-rate-based `_optimize_skip_first_minutes`
    rule — slippage and win-rate are independent signals that both
    legitimately want to move this param. The cooldown guard
    prevents thrash by limiting one change per param per week.

    Decision rule:
      First-15-min |avg_slippage| > 1.5x rest-of-day avg → bump
        skip_first_minutes UP by 5 (capped at 30)
      First-15-min |avg_slippage| < rest-of-day avg → tighten DOWN
        by 5 (floor at 0)
      Between: no change

    Needs ≥20 trades total + ≥5 in each bucket. data_quality-
    excluded.

    2026-05-12.
    """
    if not _safe_change_guarded(profile_id, "skip_first_minutes"):
        return None
    from journal import data_quality_clause
    _dq = data_quality_clause(conn)
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None
    try:
        # timestamps are ET-isoformat; the open is 09:30 ET. Inside
        # the first 15 minutes ≤ 09:45.
        rows = conn.execute(
            f"""SELECT slippage_pct, timestamp FROM trades
                WHERE side IN ('buy', 'sell', 'short', 'cover')
                  AND slippage_pct IS NOT NULL
                  AND timestamp >= datetime('now', '-30 days')
                  {_dq}"""
        ).fetchall()
    except Exception:
        return None
    first_15 = []
    rest = []
    for slip, ts in rows:
        if not ts:
            continue
        try:
            tod = ts.split("T")[1][:5] if "T" in ts else None
        except Exception:
            tod = None
        if not tod:
            continue
        try:
            hh, mm = int(tod[:2]), int(tod[3:5])
            minute_of_day = hh * 60 + mm
        except (ValueError, TypeError, IndexError) as _td_exc:
            # Per-row TOD parse loop; skip malformed timestamps but
            # surface data-quality issue at DEBUG.
            logger.debug(
                "first-minutes-slip scan skipped row (bad ts): %s: %s",
                type(_td_exc).__name__, _td_exc,
            )
            continue
        if 9 * 60 + 30 <= minute_of_day <= 9 * 60 + 45:
            first_15.append(abs(float(slip)))
        elif 9 * 60 + 45 < minute_of_day <= 16 * 60:
            rest.append(abs(float(slip)))

    if len(first_15) < 5 or len(rest) < 5 or len(first_15) + len(rest) < 20:
        return None
    first_avg = sum(first_15) / len(first_15)
    rest_avg = sum(rest) / len(rest) if rest else 0
    if rest_avg <= 0:
        return None

    current = int(getattr(ctx, "skip_first_minutes", 5) or 5)

    if first_avg > rest_avg * 1.5 and current < 30:
        new_val = min(30, current + 5)
        reason = (
            f"First-15-min slippage {first_avg:.3f}% vs rest-of-day "
            f"{rest_avg:.3f}% ({first_avg/rest_avg:.1f}× worse). "
            f"Widen open-skip to {new_val} min."
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "skip_first_minutes_widen",
            "skip_first_minutes", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Widened {_label('skip_first_minutes')} "
            f"from {current} to {applied} min{suffix} ({reason})"
        )

    if first_avg < rest_avg and current > 0:
        new_val = max(0, current - 5)
        reason = (
            f"First-15-min slippage {first_avg:.3f}% no worse than "
            f"rest-of-day {rest_avg:.3f}% — tighten open-skip "
            f"to {new_val} min."
        )
        applied, _, suffix = _apply_param_change(
            profile_id, user_id, "skip_first_minutes_tighten",
            "skip_first_minutes", current, new_val, reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Tightened {_label('skip_first_minutes')} "
            f"from {current} to {applied} min{suffix} ({reason})"
        )
    return None


# ---------------------------------------------------------------------------
# Wave 4 — weighted signal intensity (Layer 2). Per-profile weights for
# every signal the AI sees. Tuner walks each weightable signal, buckets
# resolved predictions by whether the signal was materially present, and
# nudges weight up or down based on the differential WR. Same safety
# scaffolding as Layer 1.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Wave 5 — Layer 3 per-regime parameter overrides. The tuner detects
# parameters that perform meaningfully differently across regimes
# (bull / bear / sideways / volatile / crisis) and creates per-regime
# overrides so each regime gets the value that's empirically best for it.
# ---------------------------------------------------------------------------

# Which parameters are eligible for per-regime override creation. The
# tuner needs both (a) a clear way to detect "this parameter would be
# better at value X in regime R" and (b) the parameter must already be
# in the global tuning system. Limit to parameters whose regime-specific
# values are most likely to actually differ in practice.
_REGIME_TUNABLE_PARAMS = {
    "stop_loss_pct",
    "take_profit_pct",
    "max_position_pct",
    "ai_confidence_threshold",
    "max_total_positions",
    "atr_multiplier_sl",
    "atr_multiplier_tp",
}

_REGIME_MIN_SAMPLES = 10  # per regime — below this, fall back to global
_REGIME_DIFF_THRESHOLD = 12  # percentage points WR diff to act on


def _optimize_regime_overrides(conn, ctx, profile_id, user_id,
                                 overall_wr, resolved):
    """For each regime-tunable parameter, check if any one regime has a
    materially different win-rate pattern. If so, create a per-regime
    override that nudges the parameter in the regime-appropriate
    direction.

    Detection model: bucket resolved predictions by regime; if one regime
    has WR >=12pt below overall AND >=10 samples, push that regime's
    parameter toward the more-conservative end of its range. If WR is
    >=12pt above, push toward more-aggressive.

    Same safety scaffolding as Layer 1: cooldown keyed on
    `regime:<regime>:<param>`, reverse-if-worsened, bound clamping.
    """
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_predictions)").fetchall()}
    if "regime_at_prediction" not in cols:
        return None

    rows = conn.execute(
        "SELECT regime_at_prediction, COUNT(*) as total, "
        " SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
        "FROM ai_predictions "
        "WHERE status='resolved' AND regime_at_prediction IS NOT NULL "
        "GROUP BY regime_at_prediction"
    ).fetchall()
    if not rows:
        return None

    from regime_overrides import (
        RECOGNISED_REGIMES, set_override, parse_overrides, resolve_param
    )
    raw = getattr(ctx, "regime_overrides", None)
    overrides = parse_overrides(raw if isinstance(raw, str) else None)

    # Sort rows by regime name for deterministic iteration — RECOGNISED_REGIMES
    # is a set in regime_overrides, so without this the "first regime with
    # divergence wins" pick depends on database row order which varies.
    rows = sorted(rows, key=lambda r: r["regime_at_prediction"] or "")
    # For each regime with enough samples, find a parameter to tune.
    for r in rows:
        regime = r["regime_at_prediction"]
        if regime not in RECOGNISED_REGIMES:
            continue
        n = r["total"]
        if n < _REGIME_MIN_SAMPLES:
            continue
        regime_wr = (r["wins"] / n) * 100.0 if n > 0 else 0
        diff = regime_wr - overall_wr  # negative = underperforming

        if abs(diff) < _REGIME_DIFF_THRESHOLD:
            continue  # Regime not differentiated enough — no action

        # Pick the most impactful parameter to override per regime.
        # When a regime underperforms, the safest lever is position size
        # (less capital at risk). When it outperforms, raise the
        # confidence threshold so we ride the edge.
        if diff < 0:
            # Underperforming regime — reduce position size for it
            param_name = "max_position_pct"
            cool_key = f"regime:{regime}:{param_name}"
            if not _safe_change_guarded(profile_id, cool_key):
                continue
            current = resolve_param(ctx, param_name, regime,
                                    default=getattr(ctx, param_name, 0.10))
            new_val = round(max(_bound(param_name, current * 0.75), 0.03), 4)
            if new_val >= current:
                continue
            from models import log_tuning_change
            set_override(profile_id, param_name, regime, new_val)
            reason = (
                f"{regime.title()} regime WR {regime_wr:.0f}% on {n} samples "
                f"vs {overall_wr:.0f}% overall — reduce position size for "
                f"this regime only"
            )
            log_tuning_change(
                profile_id, user_id, "regime_override_down",
                cool_key, str(current), str(new_val), reason,
                win_rate_at_change=overall_wr, predictions_resolved=resolved,
            )
            return (
                f"Set {regime} regime override: "
                f"{_label(param_name)} {current:.0%} → {new_val:.0%} "
                f"({reason})"
            )

        # diff > 0: outperforming regime — raise confidence threshold
        # to focus on this regime's strongest setups
        param_name = "ai_confidence_threshold"
        cool_key = f"regime:{regime}:{param_name}"
        if not _safe_change_guarded(profile_id, cool_key):
            continue
        current = resolve_param(ctx, param_name, regime,
                                default=getattr(ctx, param_name, 25))
        new_val = int(_bound(param_name, current + 5))
        if new_val <= current:
            continue
        from models import log_tuning_change
        set_override(profile_id, param_name, regime, new_val)
        reason = (
            f"{regime.title()} regime WR {regime_wr:.0f}% on {n} samples "
            f"vs {overall_wr:.0f}% overall — raise confidence floor "
            f"for this regime to focus on strongest setups"
        )
        log_tuning_change(
            profile_id, user_id, "regime_override_up",
            cool_key, str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Set {regime} regime override: "
            f"{_label(param_name)} {current} → {new_val} ({reason})"
        )

    return None


# ---------------------------------------------------------------------------
# Wave 6 — Layer 4 per-time-of-day overrides. Mirror of regime tuning,
# bucketed by intraday window. TOD is derived from the prediction's
# timestamp (no new column needed).
# ---------------------------------------------------------------------------

_TOD_MIN_SAMPLES = 10
_TOD_DIFF_THRESHOLD = 12  # pts of WR divergence


def _optimize_tod_overrides(conn, ctx, profile_id, user_id,
                              overall_wr, resolved):
    """Bucket recent resolved predictions into open / midday / close
    based on their timestamp (UTC -> ET -> bucket). If any bucket has
    a materially different WR, create a per-TOD override for that
    bucket."""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_predictions)").fetchall()}
    if "timestamp" not in cols:
        return None

    rows = conn.execute(
        "SELECT timestamp, actual_outcome FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome IN ('win','loss') "
        "  AND timestamp IS NOT NULL"
    ).fetchall()
    if len(rows) < 30:
        return None

    from tod_overrides import (
        _bucket_for_minute, RECOGNISED_TODS, set_override, resolve_param
    )
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None

    # Deterministic iteration order — RECOGNISED_TODS is a set, so we
    # explicitly use the canonical chronological order. Without this the
    # tuner's "first bucket with divergence wins" picks differently on
    # each Python invocation depending on set hashing.
    _ORDERED_TODS = ("open", "midday", "close")
    buckets = {tod: {"wins": 0, "total": 0} for tod in _ORDERED_TODS}
    et = ZoneInfo("America/New_York")
    from datetime import datetime, timezone
    for r in rows:
        ts = r["timestamp"]
        try:
            # Predictions store UTC ISO timestamps
            dt = datetime.fromisoformat(ts.replace("Z", "")[:19])
            dt_utc = dt.replace(tzinfo=timezone.utc)
            dt_et = dt_utc.astimezone(et)
            if dt_et.weekday() >= 5:
                continue
            minutes = dt_et.hour * 60 + dt_et.minute
            bucket = _bucket_for_minute(minutes)
        except (AttributeError, ValueError, TypeError) as _ts_exc:
            # Per-row TOD bucketing loop; skip malformed timestamps
            # but surface data quality issue at DEBUG.
            logger.debug(
                "TOD override scan skipped row (bad ts): %s: %s",
                type(_ts_exc).__name__, _ts_exc,
            )
            continue
        if not bucket:
            continue
        buckets[bucket]["total"] += 1
        if r["actual_outcome"] == "win":
            buckets[bucket]["wins"] += 1

    for tod_name, b in buckets.items():
        n = b["total"]
        if n < _TOD_MIN_SAMPLES:
            continue
        bucket_wr = b["wins"] / n * 100.0
        diff = bucket_wr - overall_wr
        if abs(diff) < _TOD_DIFF_THRESHOLD:
            continue

        if diff < 0:
            # Underperforming bucket — reduce position size for it
            param_name = "max_position_pct"
            cool_key = f"tod:{tod_name}:{param_name}"
            if not _safe_change_guarded(profile_id, cool_key):
                continue
            current = resolve_param(ctx, param_name, tod_name,
                                     default=getattr(ctx, param_name, 0.10))
            new_val = round(max(_bound(param_name, current * 0.75), 0.03), 4)
            if new_val >= current:
                continue
            from models import log_tuning_change
            set_override(profile_id, param_name, tod_name, new_val)
            reason = (
                f"{tod_name.title()} bucket WR {bucket_wr:.0f}% on {n} samples "
                f"vs {overall_wr:.0f}% overall — reduce position size for "
                f"this time of day only"
            )
            log_tuning_change(
                profile_id, user_id, "tod_override_down",
                cool_key, str(current), str(new_val), reason,
                win_rate_at_change=overall_wr, predictions_resolved=resolved,
            )
            return (
                f"Set {tod_name} time-of-day override: "
                f"{_label(param_name)} {current:.0%} → {new_val:.0%} "
                f"({reason})"
            )

        # Outperforming bucket — raise confidence threshold for it
        param_name = "ai_confidence_threshold"
        cool_key = f"tod:{tod_name}:{param_name}"
        if not _safe_change_guarded(profile_id, cool_key):
            continue
        current = resolve_param(ctx, param_name, tod_name,
                                 default=getattr(ctx, param_name, 25))
        new_val = int(_bound(param_name, current + 5))
        if new_val <= current:
            continue
        from models import log_tuning_change
        set_override(profile_id, param_name, tod_name, new_val)
        reason = (
            f"{tod_name.title()} bucket WR {bucket_wr:.0f}% on {n} samples "
            f"vs {overall_wr:.0f}% overall — raise confidence floor for "
            f"this time of day"
        )
        log_tuning_change(
            profile_id, user_id, "tod_override_up",
            cool_key, str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Set {tod_name} time-of-day override: "
            f"{_label(param_name)} {current} → {new_val} ({reason})"
        )

    return None


# ---------------------------------------------------------------------------
# Wave 8 — Layer 7 per-symbol parameter overrides. The most-specific
# tier in the override chain. For symbols with materially different
# behavior than the profile baseline, create per-symbol overrides.
# Longer cooldown (7 days) because per-symbol samples are smaller and
# we want to avoid over-fitting day-to-day noise.
# ---------------------------------------------------------------------------

_SYMBOL_MIN_SAMPLES = 20  # per-symbol samples — high bar; per-symbol over-fitting risk is real
_SYMBOL_DIFF_THRESHOLD = 15  # WR pt divergence to act
_SYMBOL_COOLDOWN_DAYS = 7   # vs 3 for global / regime / TOD


def _optimize_symbol_overrides(conn, ctx, profile_id, user_id,
                                 overall_wr, resolved):
    """For symbols with >=20 individual resolved predictions, check if
    that symbol's win-rate diverges materially from the profile's
    overall WR. If so, create a per-symbol override on the most
    impactful parameter (max_position_pct for underperformers,
    ai_confidence_threshold for outperformers)."""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_predictions)").fetchall()}
    if "symbol" not in cols:
        return None

    rows = conn.execute(
        "SELECT symbol, COUNT(*) as total, "
        " SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins, "
        " (CAST(SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) "
        "  AS REAL) / COUNT(*)) as wr "
        "FROM ai_predictions "
        "WHERE status='resolved' AND symbol IS NOT NULL "
        "  AND actual_outcome IN ('win','loss') "
        "GROUP BY symbol HAVING total >= ? "
        # Worst-WR-first so capital-protective overrides (reduce position
        # size on underperformers) act before opportunity-capture ones
        # (raise confidence floor on outperformers).
        "ORDER BY wr ASC, symbol ASC",
        (_SYMBOL_MIN_SAMPLES,),
    ).fetchall()
    if not rows:
        return None

    from symbol_overrides import set_override, resolve_param as resolve_sym

    for r in rows:
        symbol = r["symbol"]
        n = r["total"]
        sym_wr = r["wins"] / n * 100.0
        diff = sym_wr - overall_wr

        if abs(diff) < _SYMBOL_DIFF_THRESHOLD:
            continue

        if diff < 0:
            # Underperforming symbol — reduce position size for it
            param_name = "max_position_pct"
            cool_key = f"symbol:{symbol}:{param_name}"
            if not _safe_change_guarded(profile_id, cool_key):
                continue
            current = resolve_sym(ctx, param_name, symbol,
                                   default=getattr(ctx, param_name, 0.10))
            new_val = round(max(_bound(param_name, current * 0.75), 0.03), 4)
            if new_val >= current:
                continue
            from models import log_tuning_change
            set_override(profile_id, param_name, symbol, new_val)
            reason = (
                f"{symbol} WR {sym_wr:.0f}% on {n} samples vs "
                f"{overall_wr:.0f}% overall — reduce position size "
                f"for this symbol only"
            )
            log_tuning_change(
                profile_id, user_id, "symbol_override_down",
                cool_key, str(current), str(new_val), reason,
                win_rate_at_change=overall_wr, predictions_resolved=resolved,
            )
            return (
                f"Set {symbol} symbol override: "
                f"{_label(param_name)} {current:.0%} → {new_val:.0%} "
                f"({reason})"
            )

        # Outperforming symbol — raise confidence floor for it
        param_name = "ai_confidence_threshold"
        cool_key = f"symbol:{symbol}:{param_name}"
        if not _safe_change_guarded(profile_id, cool_key):
            continue
        current = resolve_sym(ctx, param_name, symbol,
                               default=getattr(ctx, param_name, 25))
        new_val = int(_bound(param_name, current + 5))
        if new_val <= current:
            continue
        from models import log_tuning_change
        set_override(profile_id, param_name, symbol, new_val)
        reason = (
            f"{symbol} WR {sym_wr:.0f}% on {n} samples vs "
            f"{overall_wr:.0f}% overall — raise confidence floor "
            f"for this symbol"
        )
        log_tuning_change(
            profile_id, user_id, "symbol_override_up",
            cool_key, str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (
            f"Set {symbol} symbol override: "
            f"{_label(param_name)} {current} → {new_val} ({reason})"
        )

    return None


# ---------------------------------------------------------------------------
# False-negative analysis — when the AI rejected a candidate (predicted
# HOLD) but the price subsequently moved enough that we missed an
# opportunity. HOLD predictions resolve as 'loss' when |return_pct| >=
# 2%, so a HOLD-loss IS a missed opportunity. If many of these cluster
# just below the current confidence threshold, the threshold is too
# tight — recommend lowering it.
# ---------------------------------------------------------------------------

_FALSE_NEG_MIN_SAMPLES = 10        # min HOLD-losses to bother analyzing
_FALSE_NEG_BAND_BELOW = 10         # within X confidence-points below threshold
_FALSE_NEG_FRAC_TRIGGER = 0.6      # X% of misses within the marginal band


def _optimize_false_negatives(conn, ctx, profile_id, user_id,
                                overall_wr, resolved):
    """Detect rejected trades that would have won. If a meaningful
    fraction of the AI's HOLD-losses had confidence just below the
    current threshold, the threshold is rejecting trades it shouldn't.
    Lower it by 5 (with the standard cooldown + reverse safety)."""
    if not _safe_change_guarded(profile_id, "ai_confidence_threshold"):
        return None

    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_predictions)").fetchall()}
    if "confidence" not in cols:
        return None

    threshold = int(getattr(ctx, "ai_confidence_threshold", 25))
    if threshold <= 10:
        return None  # Already at/near floor; nothing to lower

    band_lo = max(0, threshold - _FALSE_NEG_BAND_BELOW)

    # All HOLD predictions resolved as loss in the trailing 30 days
    # (each one is a missed opportunity — the price moved enough to
    # have made a trade profitable).
    row = conn.execute(
        "SELECT COUNT(*) as total, "
        " SUM(CASE WHEN confidence >= ? AND confidence < ? "
        "          THEN 1 ELSE 0 END) as marginal "
        "FROM ai_predictions "
        "WHERE status='resolved' AND predicted_signal='HOLD' "
        "  AND actual_outcome='loss' "
        "  AND datetime(resolved_at) >= datetime('now', '-30 days') "
        "  AND confidence IS NOT NULL",
        (band_lo, threshold),
    ).fetchone()

    if not row or (row["total"] or 0) < _FALSE_NEG_MIN_SAMPLES:
        return None

    total = row["total"]
    marginal = row["marginal"] or 0
    frac = marginal / total

    if frac < _FALSE_NEG_FRAC_TRIGGER:
        return None

    new_threshold = max(10, threshold - 5)
    if new_threshold >= threshold:
        return None

    reason = (
        f"False-negative analysis: {marginal}/{total} ({frac:.0%}) of "
        f"the AI's HOLD-losses had confidence in the marginal band "
        f"{band_lo}-{threshold} — threshold rejecting trades that "
        f"would have won. Lower from {threshold} to {new_threshold}."
    )
    applied, _, suffix = _apply_param_change(
        profile_id, user_id, "false_negative_loosen",
        "ai_confidence_threshold", threshold, new_threshold, reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return (
        f"Lowered {_label('ai_confidence_threshold')} from {threshold} "
        f"to {applied}{suffix} ({reason})"
    )


# ---------------------------------------------------------------------------
# Wave 11 — Layer 8 self-commissioned new strategies. The tuner
# identifies coverage gaps — patterns where the AI made correct calls
# but no existing strategy fired — and triggers Phase 7's
# strategy_proposer with a focused brief. Cost-gated heavily because
# strategy generation costs real LLM tokens. Rate-limited to one
# commission per profile per week.
# ---------------------------------------------------------------------------

_COMMISSION_COOLDOWN_DAYS = 7
_COMMISSION_MIN_GAPS = 5      # minimum no-strategy winners to bother
_COMMISSION_EST_USD = 0.05    # rough cost of one strategy_proposal call


def _optimize_commission_strategy(conn, ctx, profile_id, user_id,
                                    overall_wr, resolved):
    """Detect strategy coverage gaps and commission a new strategy via
    Phase 7 generator if the gap is meaningful and cost-affordable."""
    if not _safe_change_guarded(profile_id, "self_commission"):
        return None

    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_predictions)").fetchall()}
    if "strategy_type" not in cols:
        return None

    # Gaps = resolved winning entry predictions where no strategy
    # fired. 2026-05-12 fix: previously listed only ('BUY', 'SELL')
    # — missed STRONG_BUY/WEAK_BUY/SHORT/STRONG_SELL/WEAK_SELL/COVER
    # winners. Same partial-list class as the HOLD-exclusion fix.
    # HOLD intentionally excluded — gap-detection looks for missed
    # ENTRY opportunities (rows where AI conviction was high enough
    # to commit), not no-trade decisions.
    gap_rows = conn.execute(
        "SELECT symbol, predicted_signal, actual_return_pct "
        "FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome='win' "
        "  AND predicted_signal IN ('BUY', 'STRONG_BUY', 'WEAK_BUY', "
        "                            'SELL', 'STRONG_SELL', 'WEAK_SELL', "
        "                            'SHORT', 'COVER') "
        "  AND (strategy_type IS NULL OR strategy_type = '') "
        "  AND datetime(timestamp) >= datetime('now', '-30 days') "
        "ORDER BY timestamp DESC LIMIT 50"
    ).fetchall()

    if len(gap_rows) < _COMMISSION_MIN_GAPS:
        return None

    # Cost gate — strategy generation makes an LLM call. Always check.
    try:
        from cost_guard import can_afford_action, format_cost_recommendation
        if not can_afford_action(user_id, _COMMISSION_EST_USD):
            return format_cost_recommendation(
                f"commission new strategy "
                f"(detected {len(gap_rows)} no-strategy winners in last 30d)",
                user_id, _COMMISSION_EST_USD,
            )
    except (ImportError, AttributeError, TypeError) as _cg_exc:
        # Cost-gate import failure falls open — strategy generation
        # proceeds. Surface for follow-up so we don't quietly lose
        # spend visibility.
        logger.warning(
            "cost-gate check failed for commission_strategy, falling open: %s: %s",
            type(_cg_exc).__name__, _cg_exc,
        )

    # Build a focused brief describing the gap.
    sample_symbols = [r["symbol"] for r in gap_rows[:5]]
    avg_return = (sum(r["actual_return_pct"] or 0 for r in gap_rows)
                   / len(gap_rows))
    ctx_summary = (
        f"Strategy coverage gap detected: {len(gap_rows)} winning "
        f"AI predictions over the last 30 days had no strategy fire "
        f"on them (avg return {avg_return:+.1f}%). Sample symbols: "
        f"{', '.join(sample_symbols)}. Propose 1-2 new strategies "
        f"that could systematically catch these patterns."
    )

    # Trigger the Phase 7 strategy_proposer.
    try:
        from strategy_proposer import propose_strategies
        from strategy_generator import save_spec
        ai_provider = getattr(ctx, "ai_provider", "anthropic")
        ai_model = getattr(ctx, "ai_model", "claude-haiku-4-5-20251001")
        # ai_api_key on ctx is always the decrypted form (decryption
        # happens in build_user_context_from_profile). Earlier code
        # had a dead-fallback to the encrypted-form attribute that
        # never existed on UserContext. Removed 2026-04-28 after the
        # ctx-round-trip test surfaced the silent-disconnect class.
        ai_api_key = getattr(ctx, "ai_api_key", None)
        if not ai_api_key:
            # Fall back to environment-configured default
            import os
            ai_api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not ai_api_key:
            return None

        # ctx.segment is the canonical market-type field. Earlier
        # code had a dead first-try to a non-existent UserContext
        # attribute; cleaned up 2026-04-28.
        market_type = getattr(ctx, "segment", None)
        market_types = [market_type] if market_type else None

        # P1.13 of LONG_SHORT_PLAN.md — direction mix on shorts-enabled
        # profiles. When shorts are enabled but recent commissions have
        # all been bullish, alternate to a SELL-direction proposal so
        # the strategy library actually grows in both directions.
        direction_mix = None
        if getattr(ctx, "enable_short_selling", False):
            # Check the most-recent commissioned strategy's direction
            # to alternate; absent prior, request SELL first since
            # that's the under-built direction.
            try:
                last_dir = conn.execute(
                    "SELECT direction FROM auto_generated_strategies "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                next_dir = ("BUY" if (last_dir and last_dir["direction"] == "SELL")
                            else "SELL")
            except Exception:
                next_dir = "SELL"
            direction_mix = {next_dir: 1}

        proposals = propose_strategies(
            ctx_summary=ctx_summary,
            recent_performance=[],  # no per-strategy stats needed
            n_proposals=1,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_api_key=ai_api_key,
            market_types=market_types,
            db_path=ctx.db_path,
            direction_mix=direction_mix,
        )
        if not proposals:
            return None

        spec_ids = []
        for spec in proposals:
            try:
                spec_id = save_spec(ctx.db_path, spec)
                spec_ids.append(spec_id)
            except Exception as save_exc:
                logger.warning("save_spec failed: %s", save_exc)

        if not spec_ids:
            return None

        from models import log_tuning_change
        reason = (
            f"Commissioned {len(spec_ids)} new strategy from "
            f"{len(gap_rows)} no-strategy winners"
        )
        log_tuning_change(
            profile_id, user_id, "self_commission",
            "self_commission", "0", str(len(spec_ids)), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Commissioned new strategy proposal ({reason})"
    except Exception as exc:
        logger.warning("Strategy commission failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Wave 10 — Layer 6 adaptive AI prompt structure. Periodically rotate
# one section's verbosity to test whether the AI does better with
# brief / normal / detailed framing. Cost-gated: any move toward
# 'detailed' that would push spend over the daily ceiling becomes a
# recommendation, not an auto-action.
# ---------------------------------------------------------------------------

# Rotate at most once every 14 days per section so each variant has
# enough resolved-prediction throughput to attribute outcomes cleanly.
_PROMPT_ROTATE_COOLDOWN_DAYS = 14


def _optimize_prompt_layout(conn, ctx, profile_id, user_id,
                              overall_wr, resolved):
    """Pick one section to rotate to a new verbosity. Cooldown is
    long (14 days) so each variant has enough cycles to attribute
    outcomes."""
    # Need a reasonable baseline of resolved predictions before
    # experimenting with the prompt itself.
    if resolved < 50:
        return None

    # Cooldown is per-rotation across all sections — this is a
    # whole-prompt experiment, not per-parameter tuning.
    if _get_recent_adjustment(profile_id, "prompt_layout_rotate",
                                days=_PROMPT_ROTATE_COOLDOWN_DAYS):
        return None

    try:
        from prompt_layout import (
            pick_rotation, set_verbosity, display_label,
            estimate_daily_cost_delta,
        )
    except ImportError:
        return None

    section, current, new = pick_rotation(ctx)
    cost_delta = estimate_daily_cost_delta(current, new)

    # Cost gate: any rotation that would add cost gets checked
    # against the daily ceiling. Cost-saving rotations
    # (brief shifts) always pass.
    if cost_delta > 0:
        try:
            from cost_guard import can_afford_action, format_cost_recommendation
            if not can_afford_action(user_id, cost_delta):
                return format_cost_recommendation(
                    f"rotate {display_label(section)} from "
                    f"{current} to {new}",
                    user_id, cost_delta,
                )
        except (ImportError, AttributeError, TypeError) as _cg_exc:
            # Cost-gate import failure falls open — rotation
            # proceeds. Surface for follow-up so spend visibility
            # isn't silently dropped.
            logger.warning(
                "cost-gate check failed for prompt-rotate, falling open: %s: %s",
                type(_cg_exc).__name__, _cg_exc,
            )

    set_verbosity(profile_id, section, new)
    from models import log_tuning_change
    reason = (
        f"Rotating prompt verbosity (cost delta ${cost_delta:+.4f}/day) "
        f"to test whether {new} framing improves WR"
    )
    log_tuning_change(
        profile_id, user_id, "prompt_layout_rotate",
        f"layout:{section}", current, new, reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return (
        f"Rotated {display_label(section)}: {current} → {new} ({reason})"
    )


def _optimize_signal_weights(conn, ctx, profile_id, user_id,
                              overall_wr, resolved):
    """Walk every weightable signal; for each, compute the win rate of
    predictions where the signal was materially present vs the global
    baseline. Nudge weight DOWN when present-WR is materially below
    baseline, UP when present-WR has recovered above baseline.

    Returns a single string describing the change (or None if no change).
    The orchestrator's one-change-per-cycle pattern means we evaluate
    signals in canonical order and act on the first one with a clear
    signal — preserving clean reversal attribution."""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_predictions)").fetchall()}
    if "features_json" not in cols:
        return None

    rows = conn.execute(
        "SELECT actual_outcome, features_json FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome IN ('win','loss') "
        "  AND features_json IS NOT NULL"
    ).fetchall()
    if len(rows) < 30:
        return None

    import json as _j
    from signal_weights import (
        WEIGHTABLE_SIGNALS, get_weight, nudge_down, nudge_up,
        display_label, WEIGHT_LADDER,
    )

    # Pre-decode all features once.
    feature_rows = []
    for r in rows:
        try:
            f = _j.loads(r["features_json"])
        except (TypeError, ValueError, _j.JSONDecodeError) as _jp_exc:
            # Per-row parse over a feature-blob list; skip malformed
            # rows but surface data-quality issue at DEBUG.
            logger.debug(
                "weight-tuning skipped row, bad features_json: %s: %s",
                type(_jp_exc).__name__, _jp_exc,
            )
            continue
        feature_rows.append((f, r["actual_outcome"]))

    # Walk each signal and find one with a clear nudge signal.
    for sig_name, _label_text, predicate in WEIGHTABLE_SIGNALS:
        # Cooldown is keyed on `weight:<sig>` so each signal has its
        # own 3-day window.
        cool_key = f"weight:{sig_name}"
        if not _safe_change_guarded(profile_id, cool_key):
            continue

        present = absent = 0
        present_wins = absent_wins = 0
        for feats, outcome in feature_rows:
            try:
                active = bool(predicate(feats))
            except (KeyError, TypeError, ValueError, AttributeError) as _pred_exc:
                # Per-feature predicate eval; skip rows where the
                # predicate doesn't apply but surface for follow-up.
                logger.debug(
                    "weight-tuning predicate eval skipped row (%s): %s: %s",
                    sig_name, type(_pred_exc).__name__, _pred_exc,
                )
                continue
            if active:
                present += 1
                if outcome == "win":
                    present_wins += 1
            else:
                absent += 1
                if outcome == "win":
                    absent_wins += 1

        if present < 10:
            continue  # Insufficient sample for this signal

        present_wr = present_wins / present * 100.0
        absent_wr = (absent_wins / absent * 100.0) if absent > 0 else overall_wr
        diff = present_wr - absent_wr  # >0 signal-helps, <0 signal-hurts
        current_weight = get_weight(ctx, sig_name)

        # NUDGE DOWN when signal-present materially underperforms baseline
        if diff <= -10 and current_weight > 0.0:
            new_weight = nudge_down(profile_id, sig_name)
            if new_weight is None:
                continue
            from models import log_tuning_change
            from display_names import display_name as _dn
            reason = (
                f"{display_label(sig_name)} won {present_wr:.0f}% on "
                f"{present} samples vs {absent_wr:.0f}% without ("
                f"{diff:+.0f} pt) — reduce intensity"
            )
            log_tuning_change(
                profile_id, user_id, "signal_weight_down",
                cool_key, str(current_weight), str(new_weight), reason,
                win_rate_at_change=overall_wr, predictions_resolved=resolved,
            )
            return (
                f"Reduced intensity of {display_label(sig_name)} "
                f"from {current_weight:.1f} to {new_weight:.1f} ({reason})"
            )

        # NUDGE UP when signal-present materially outperforms AND we
        # previously reduced it — recovery signal.
        if diff >= 5 and current_weight < 1.0:
            # Cost guard: re-including a previously-omitted signal
            # (weight 0.0 → 0.4) means longer prompts → more tokens →
            # higher cost. Estimate generously: each restored signal
            # adds about 1¢/day in token cost at typical scan rate.
            from cost_guard import can_afford_action, format_cost_recommendation
            estimated_extra_per_day = 0.01  # ~1¢/day per re-included signal
            if not can_afford_action(user_id, estimated_extra_per_day):
                # Surface as recommendation; don't auto-apply.
                return format_cost_recommendation(
                    f"restore intensity of {display_label(sig_name)} "
                    f"from {current_weight:.1f} to "
                    f"{(current_weight + 0.3):.1f}",
                    user_id, estimated_extra_per_day,
                )
            new_weight = nudge_up(profile_id, sig_name)
            if new_weight is None:
                continue
            from models import log_tuning_change
            reason = (
                f"{display_label(sig_name)} recovered to {present_wr:.0f}% "
                f"on {present} samples vs {absent_wr:.0f}% without "
                f"({diff:+.0f} pt) — restore intensity"
            )
            log_tuning_change(
                profile_id, user_id, "signal_weight_up",
                cool_key, str(current_weight), str(new_weight), reason,
                win_rate_at_change=overall_wr, predictions_resolved=resolved,
            )
            return (
                f"Restored intensity of {display_label(sig_name)} "
                f"from {current_weight:.1f} to {new_weight:.1f} ({reason})"
            )

    return None


def _optimize_maga_mode(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """maga_mode binary: auto-disable if predictions made when ON
    materially underperform the global baseline. Auto-enable is left as
    a recommendation (per the no-recommendation-only allowlist's
    asymmetric-on-purpose rule for high-impact feature flags).

    For W1 binary auto-disable only. W4 (Layer 2) makes it weighted.
    """
    if not _safe_change_guarded(profile_id, "maga_mode"):
        return None

    if not getattr(ctx, "maga_mode", False):
        return None  # Already off; nothing to disable

    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_predictions)").fetchall()}
    if "features_json" not in cols:
        return None

    # Bucket resolved predictions by whether features_json includes a
    # political-context signal.
    rows = conn.execute(
        "SELECT actual_outcome, features_json FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome IN ('win','loss') "
        "  AND features_json IS NOT NULL"
    ).fetchall()

    on_total = on_wins = 0
    for r in rows:
        try:
            import json as _j
            feats = _j.loads(r["features_json"]) if r["features_json"] else {}
        except (TypeError, ValueError, _j.JSONDecodeError) as _jp_exc:
            # Per-row parse over a feature-blob list; skip malformed
            # rows but surface data-quality issue at DEBUG.
            logger.debug(
                "maga-mode scan skipped row, bad features_json: %s: %s",
                type(_jp_exc).__name__, _jp_exc,
            )
            continue
        if feats.get("political_context") or feats.get("maga_mode"):
            on_total += 1
            if r["actual_outcome"] == "win":
                on_wins += 1

    if on_total < 20:
        return None  # Not enough data to judge

    on_wr = on_wins / on_total * 100
    diff = on_wr - overall_wr
    # Auto-disable threshold: clearly underperforming the baseline.
    if diff <= -10:
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, maga_mode=0)
        reason = (
            f"Predictions with political context active won {on_wr:.0f}% "
            f"vs {overall_wr:.0f}% overall "
            f"({on_wins}/{on_total} samples) — disable"
        )
        log_tuning_change(
            profile_id, user_id, "maga_disable",
            "maga_mode", "1", "0", reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Disabled MAGA mode ({reason})"

    return None


# ---------------------------------------------------------------------------
# Batch context for AI-first pipeline
# ---------------------------------------------------------------------------

def _analyze_failure_patterns(db_path):
    """Find patterns in losing predictions — what conditions lead to losses.

    Queries ai_predictions joined with regime/strategy data to find patterns
    like "breakout signals in volatile markets: 15% win rate."

    Returns list of up to 5 pattern strings.
    """
    try:
        conn = _get_conn(db_path)
    except Exception:
        return []

    patterns = []
    try:
        # Check if regime/strategy columns exist
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_predictions)").fetchall()}
        has_regime = "regime_at_prediction" in cols
        has_strategy = "strategy_type" in cols

        if not has_regime and not has_strategy:
            return []

        # Pattern 1: Win rate by regime
        # DISPLAY_ONLY: builds AI-prompt context strings (patterns
        # are surfaced to the AI, not used to mechanically tighten).
        # Humanize the raw regime identifier (e.g. `strong_bull` ->
        # `Strong Bull`) before substituting into the displayed
        # pattern string — pre-2026-05-16 we leaked snake_case
        # straight from the DB into the learned-patterns API payload.
        from display_names import display_name as _dn
        if has_regime:
            rows = conn.execute(
                "SELECT regime_at_prediction, COUNT(*) as total, "
                "SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
                "FROM ai_predictions WHERE status='resolved' AND regime_at_prediction IS NOT NULL "
                "GROUP BY regime_at_prediction HAVING COUNT(*) >= 5"
            ).fetchall()
            overall = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) "
                "FROM ai_predictions WHERE status='resolved'"
            ).fetchone()
            overall_wr = (overall[1] / overall[0] * 100) if overall[0] > 0 else 50

            for r in rows:
                regime = _dn(r[0])
                total = r[1]
                wr = r[2] / total * 100
                if wr < overall_wr - 15:  # Significantly worse than average
                    patterns.append(
                        f"Predictions in {regime} markets: {wr:.0f}% win rate "
                        f"(vs {overall_wr:.0f}% overall, {total} trades). Be extra cautious."
                    )
                elif wr > overall_wr + 15:  # Significantly better
                    patterns.append(
                        f"Predictions in {regime} markets: {wr:.0f}% win rate "
                        f"(vs {overall_wr:.0f}% overall, {total} trades). This is your edge."
                    )

        # Pattern 2: Win rate by strategy type
        # DISPLAY_ONLY: AI-prompt pattern strings (informational).
        if has_strategy:
            from display_names import display_name as _dn
            rows = conn.execute(
                "SELECT strategy_type, COUNT(*) as total, "
                "SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
                "FROM ai_predictions WHERE status='resolved' AND strategy_type IS NOT NULL "
                "GROUP BY strategy_type HAVING COUNT(*) >= 5"
            ).fetchall()
            for r in rows:
                stype = _dn(r[0])
                total = r[1]
                wr = r[2] / total * 100
                if wr < 30:
                    patterns.append(
                        f"{stype} signals: {wr:.0f}% win rate ({total} trades). Avoid this pattern."
                    )
                elif wr > 60:
                    patterns.append(
                        f"{stype} signals: {wr:.0f}% win rate ({total} trades). Favor this pattern."
                    )

        # Pattern 3: Win rate by time of day (hour)
        # DISPLAY_ONLY: AI-prompt pattern strings (informational).
        rows = conn.execute(
            "SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour, "
            "COUNT(*) as total, "
            "SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
            "FROM ai_predictions WHERE status='resolved' "
            "GROUP BY hour HAVING COUNT(*) >= 5"
        ).fetchall()
        for r in rows:
            hour = r[0]
            total = r[1]
            wr = r[2] / total * 100
            if wr < 25:
                label = f"{hour}:00-{hour+1}:00"
                patterns.append(
                    f"Predictions at {label}: {wr:.0f}% win rate ({total} trades). "
                    f"Avoid trading this hour."
                )

        return patterns[:5]  # Max 5 patterns

    except Exception as exc:
        logger.warning("Failed to analyze failure patterns: %s", exc)
        return []
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError as _cl_exc:
            # Cleanup close — conn may already be closed.
            logger.debug(
                "self_tuning conn close: %s: %s",
                type(_cl_exc).__name__, _cl_exc,
            )


def get_batch_context_data(ctx, symbols=None):
    """Gather performance context for the AI batch prompt.

    Returns dict with:
        overall_win_rate: float or None
        total_resolved: int
        symbol_records: {symbol: {wins, losses, win_rate, avg_return}}
        profile_summary: str or None (1-line summary)
    """
    db_path = getattr(ctx, "db_path", None)
    if not db_path:
        return {"overall_win_rate": None, "total_resolved": 0,
                "symbol_records": {}, "profile_summary": None}

    try:
        with closing(_get_conn(db_path)) as conn:
            wr, total = _get_current_win_rate(conn)
    except Exception:
        wr, total = 0.0, 0

    symbol_records = get_symbol_reputation(db_path, min_predictions=1)

    summary = None
    # DISPLAY_ONLY: builds a human/AI summary string, not a tightener.
    if total >= 5:
        quality = "Good accuracy." if wr >= 45 else "Be more selective."
        summary = f"Overall: {wr:.0f}% win rate ({total} resolved). {quality}"

    # Pattern learning — what conditions lead to wins/losses
    failure_patterns = _analyze_failure_patterns(db_path)

    return {
        # DISPLAY_ONLY: gates whether the win-rate is reported in the
        # AI-prompt context dict; consumed by AI as informational, not
        # by a tightener.
        "overall_win_rate": wr if total >= 5 else None,
        "total_resolved": total,
        "symbol_records": symbol_records,
        "profile_summary": summary,
        "learned_patterns": failure_patterns,
    }
