"""Cross-profile insight propagation — Layer 5 of autonomous tuning.

When the tuner makes a change that turns out to improve a profile's
win rate (review_past_adjustments marks `outcome_after = 'improved'`),
the same detection rule is run against every OTHER enabled profile
belonging to the same user. If their data also supports the change,
they get the change applied automatically.

Critical: NO value-copying. The peer profile's own data must
independently trigger the detection rule. This ensures we don't apply
a parameter value that worked for "Mid Cap" to "Small Cap" just
because Mid Cap saw an improvement — Small Cap might have completely
different optimal values. The fleet learns which CHANGES tend to work,
but each profile's tuning is still based on its own data.

No additional API cost. All analysis is from existing resolved
predictions in each profile's DB.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _peer_profiles(source_profile_id: int) -> List[Dict[str, Any]]:
    """Return all OTHER enabled profiles belonging to the same user as
    `source_profile_id`. Used to fan out an insight from one profile
    to its siblings."""
    try:
        from models import _get_conn, get_trading_profile
        source = get_trading_profile(source_profile_id)
        if not source:
            return []
        user_id = source.get("user_id")
        if user_id is None:
            return []
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM trading_profiles "
            "WHERE user_id = ? AND id != ? "
            "  AND COALESCE(enabled, 1) = 1",
            (user_id, source_profile_id),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("peer enumeration failed: %s", exc)
        return []


# Map adjustment_type values to the _optimize_* function in self_tuning
# that should be re-run on peer profiles. Only adjustments with a
# clean detection-rule mapping are eligible to propagate. Adjustments
# without a clear rule (e.g., user-initiated changes) are skipped.
def _detector_for(change_type: str) -> Optional[Callable]:
    """Return the _optimize_* function whose detection logic produced
    `change_type`, or None if the type isn't propagatable."""
    import self_tuning as st
    mapping = {
        "confidence_threshold": st._optimize_confidence_threshold_upward,
        "confidence_threshold_optimization": st._optimize_confidence_threshold_upward,
        "regime_position_sizing": st._optimize_regime_position_sizing,
        "strategy_toggle": st._optimize_strategy_toggles,
        "strategy_deprecate": st._optimize_strategy_toggles,
        "stop_take_profit": st._optimize_stop_take_profit,
        "concentration_reduce": st._optimize_max_total_positions,
        "concentration_increase": st._optimize_max_total_positions,
        "correlation_tighten": st._optimize_max_correlation,
        "correlation_loosen": st._optimize_max_correlation,
        "sector_cap_tighten": st._optimize_max_sector_positions,
        "drawdown_pause_tighten": st._optimize_drawdown_thresholds,
        "drawdown_reduce_tighten": st._optimize_drawdown_reduce,
        "price_band_min_raise": st._optimize_price_band,
        "price_band_max_lower": st._optimize_price_band,
        "maga_disable": st._optimize_maga_mode,
        "min_volume_raise": st._optimize_min_volume,
        "volume_surge_tighten": st._optimize_volume_surge_multiplier,
        "breakout_volume_tighten": st._optimize_breakout_volume_threshold,
        "gap_threshold_tighten": st._optimize_gap_pct_threshold,
        "momentum_5d_tighten": st._optimize_momentum_5d,
        "momentum_20d_tighten": st._optimize_momentum_20d,
        "rsi_overbought_raise": st._optimize_rsi_overbought,
        "rsi_oversold_lower": st._optimize_rsi_oversold,
        "short_take_profit_tighten": st._optimize_short_take_profit,
        "atr_sl_widen": st._optimize_atr_multiplier_sl,
        "atr_tp_tighten": st._optimize_atr_multiplier_tp,
        "signal_weight_down": st._optimize_signal_weights,
        "signal_weight_up": st._optimize_signal_weights,
    }
    return mapping.get(change_type)


def propagate_insight(source_profile_id: int,
                       change_type: str,
                       parameter_name: str) -> List[str]:
    """Run the detection rule that produced `change_type` against every
    peer profile's own data. Apply the change to peers where the
    detection independently triggers.

    Returns a list of human-readable messages — one per peer where the
    change was applied. Empty list if no peers triggered (or if
    change_type isn't propagatable).
    """
    detector = _detector_for(change_type)
    if detector is None:
        return []

    peers = _peer_profiles(source_profile_id)
    if not peers:
        return []

    applied = []
    import os
    import sqlite3
    from types import SimpleNamespace

    for peer in peers:
        try:
            peer_id = peer["id"]
            db_path = f"quantopsai_profile_{peer_id}.db"
            if not os.path.exists(db_path):
                continue

            # Build a minimal context for the peer. We use SimpleNamespace
            # rather than a full UserContext (which would require API
            # keys, market data clients, etc. to construct). The
            # detection rules only read scalar parameters from ctx, so
            # a duck-typed namespace suffices.
            ctx = SimpleNamespace(**peer)
            ctx.profile_id = peer_id
            ctx.user_id = peer.get("user_id", 0)
            ctx.db_path = db_path
            ctx.enable_self_tuning = bool(peer.get("enable_self_tuning", 1))

            # Compute peer's current overall WR
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                table_check = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='ai_predictions'"
                ).fetchone()
                if not table_check:
                    conn.close()
                    continue
                row = conn.execute(
                    "SELECT COUNT(*) as total, "
                    " SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
                    "FROM ai_predictions WHERE status='resolved'"
                ).fetchone()
                if not row or (row["total"] or 0) < 20:
                    conn.close()
                    continue
                overall_wr = (row["wins"] / row["total"] * 100) if row["total"] else 0
                resolved = row["total"]

                # Run the detection rule on peer's data.
                msg = detector(conn, ctx, peer_id, ctx.user_id,
                               overall_wr, resolved)
                if msg:
                    applied.append(
                        f"[{peer.get('name', f'profile {peer_id}')}] {msg}"
                    )
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("propagation to peer %s failed: %s",
                         peer.get("id"), exc)
            continue

    return applied
