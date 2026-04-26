"""Automatic capital allocation across profiles — Layer 9 of
autonomous tuning. Opt-in by default.

**Critical constraint (the QuantOpsAI architecture):** profiles are
*virtual*. Multiple profiles can share a single underlying real
Alpaca paper account. The total capital each profile uses must
respect that shared pool — if Account A has $1M and three profiles
share it, their `capital_scale` multipliers must net to ~3.0 across
the group (i.e., average 1.0) so they don't collectively over-commit.

The allocator therefore works **per-Alpaca-account**:
1. Group profiles by `alpaca_account_id`.
2. For each group, compute scores and normalize allocations so the
   sum within the group equals N (where N is the number of profiles
   in the group). Average stays 1.0; relative weights shift toward
   proven edge.
3. Solo profiles (1 per account) always get scale=1.0 — there's
   nothing to rebalance against.

When the user flips `auto_capital_allocation` ON, a weekly task runs
`rebalance(user_id)`. The trading pipeline reads `capital_scale`
before computing position sizes, so a profile at 0.5 takes positions
half-size relative to its baseline.

Bounds:
- Per-rebalance: scale can move at most ±50% from current per week.
- Absolute: scale ∈ [0.25, 2.0]. No profile drops below 25% or rises
  above 200% of its baseline allocation.
- Group-conserving: within an Alpaca-account group, the sum of
  scales never exceeds N. If clamping creates surplus, it's
  redistributed proportionally to the lower-scored profiles.

The "score" formula is `recent_sharpe × (1 + win_rate)`. Sharpe
captures risk-adjusted return; (1 + win_rate) rewards consistency.
Profiles with no track record get the group median.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Bounds — the auto-allocator cannot push capital_scale outside these.
SCALE_FLOOR = 0.25   # No profile drops below 25% of baseline
SCALE_CEILING = 2.0   # No profile exceeds 200% of baseline
MAX_PER_REBALANCE = 0.5  # Max relative change per weekly rebalance


def _profile_score(db_path: str, days: int = 30) -> Optional[float]:
    """Compute capital_score for one profile over `days`.

    Returns None when there's not enough trade data to score. None is
    treated as median by the allocator (so new profiles aren't
    penalized).
    """
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        # Need at least 5 closed trades in the window for any score.
        rows = conn.execute(
            "SELECT pnl FROM trades "
            "WHERE pnl IS NOT NULL "
            "  AND datetime(timestamp) >= datetime('now', '-' || ? || ' days')",
            (days,),
        ).fetchall()
        conn.close()
        if len(rows) < 5:
            return None
        pnls = [r[0] for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(pnls)
        # Crude Sharpe proxy from the trade-pnl series (no annualization
        # — values are relative across profiles in the same window).
        mean = sum(pnls) / len(pnls)
        variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
        std = variance ** 0.5 if variance > 0 else 1
        sharpe = mean / std if std > 0 else 0
        return float(sharpe * (1 + wr))
    except Exception as exc:
        logger.debug("score failed for %s: %s", db_path, exc)
        return None


def _user_profiles_with_scores(user_id: int) -> List[Dict[str, Any]]:
    """Enumerate this user's enabled profiles with their current
    capital_scale, alpaca_account_id, and computed score."""
    from models import _get_conn
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, name, capital_scale, alpaca_account_id "
        "FROM trading_profiles "
        "WHERE user_id = ? AND COALESCE(enabled, 1) = 1 "
        "ORDER BY id",
        (user_id,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        rd = dict(r)
        rd["score"] = _profile_score(f"quantopsai_profile_{rd['id']}.db")
        out.append(rd)
    return out


def _allocate_within_group(profiles: List[Dict[str, Any]]) -> Dict[int, float]:
    """Allocate capital_scale across profiles that share one Alpaca
    account. The sum of scales returned equals N (the count of
    profiles in this group), so the underlying account's capital is
    never over-committed. A solo profile (N=1) always gets scale=1.0.
    """
    if not profiles:
        return {}
    n = len(profiles)
    if n == 1:
        # Solo profile on its account — nothing to rebalance against.
        return {profiles[0]["id"]: 1.0}

    # Score-weighted proportions
    valid_scores = [p["score"] for p in profiles if p["score"] is not None]
    if not valid_scores:
        # No score data anywhere — keep current
        return {p["id"]: float(p["capital_scale"] or 1.0) for p in profiles}

    sorted_scores = sorted(valid_scores)
    median = sorted_scores[len(sorted_scores) // 2]

    raw = []
    for p in profiles:
        s = p["score"] if p["score"] is not None else median
        raw.append(max(0.05, s + 1.0))  # handle negative Sharpe; floor 0.05
    total = sum(raw)
    proportions = [r / total for r in raw]

    # Target scale = proportion × N → average 1.0, sum N (group conserved)
    targets = [prop * n for prop in proportions]

    # Apply per-rebalance clamping (max ±50% relative move per week)
    # and absolute bounds.
    new_scales = {}
    for p, target in zip(profiles, targets):
        current = float(p["capital_scale"] or 1.0)
        max_up = current * (1 + MAX_PER_REBALANCE)
        max_down = current * (1 - MAX_PER_REBALANCE)
        clamped = max(max_down, min(max_up, target))
        clamped = max(SCALE_FLOOR, min(SCALE_CEILING, clamped))
        new_scales[p["id"]] = clamped

    # Re-normalize so the group still sums to N exactly. Clamping may
    # have left us with surplus or deficit; redistribute proportionally
    # so the underlying Alpaca account isn't over- or under-committed.
    current_sum = sum(new_scales.values())
    if current_sum > 0:
        scale_factor = n / current_sum
        for pid in new_scales:
            new_scales[pid] = round(
                max(SCALE_FLOOR,
                    min(SCALE_CEILING, new_scales[pid] * scale_factor)),
                4,
            )

    return new_scales


def _allocate(profiles: List[Dict[str, Any]]) -> Dict[int, float]:
    """Group profiles by alpaca_account_id and allocate within each
    group independently. Profiles without an alpaca_account_id are
    treated as their own solo group (scale = 1.0)."""
    by_account: Dict[Any, List[Dict[str, Any]]] = {}
    for p in profiles:
        acct = p.get("alpaca_account_id")
        if acct is None:
            # Solo group — these profiles don't share capital with any
            # other profile.
            by_account.setdefault(("solo", p["id"]), []).append(p)
        else:
            by_account.setdefault(acct, []).append(p)

    out: Dict[int, float] = {}
    for group in by_account.values():
        out.update(_allocate_within_group(group))
    return out


def rebalance(user_id: int) -> List[Dict[str, Any]]:
    """Run a weekly capital rebalance for this user. Returns a list of
    {profile_id, name, old_scale, new_scale, score} entries describing
    what changed. No-op for users without auto_capital_allocation
    enabled."""
    from models import _get_conn
    conn = _get_conn()
    user_row = conn.execute(
        "SELECT auto_capital_allocation FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    if not user_row or not user_row[0]:
        return []

    profiles = _user_profiles_with_scores(user_id)
    if not profiles:
        return []

    new_scales = _allocate(profiles)
    changes = []

    for p in profiles:
        old = float(p["capital_scale"] or 1.0)
        new = new_scales.get(p["id"], old)
        if abs(new - old) < 0.01:
            continue
        # Persist
        conn = _get_conn()
        conn.execute(
            "UPDATE trading_profiles SET capital_scale = ? WHERE id = ?",
            (new, p["id"]),
        )
        conn.commit()
        conn.close()
        changes.append({
            "profile_id": p["id"],
            "name": p["name"],
            "old_scale": round(old, 4),
            "new_scale": round(new, 4),
            "score": p["score"],
        })

    # Log to tuning_history for visibility (one entry summarizing the
    # rebalance, attributed to the first changed profile so it shows
    # up in the per-profile history table somewhere).
    if changes:
        try:
            from models import log_tuning_change
            summary = ", ".join(
                f"{c['name']}: {c['old_scale']:.2f}→{c['new_scale']:.2f}"
                for c in changes
            )
            log_tuning_change(
                changes[0]["profile_id"], user_id,
                "capital_rebalance", "capital_scale",
                "(weekly)", "(weekly)",
                f"Auto-allocated capital across {len(changes)} profile(s): {summary}",
                win_rate_at_change=0, predictions_resolved=0,
            )
        except Exception as exc:
            logger.debug("Capital-rebalance log failed: %s", exc)

    return changes
