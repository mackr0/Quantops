"""Cost guard — cross-cutting daily-spend ceiling enforcement.

Every autonomous action that could increase API spend (Layer 2 weight
changes that re-include omitted signals, Layer 6 adaptive prompt
verbosity, Layer 8 strategy generation, Layer 7 per-symbol overrides
that turn signals back on) calls `can_afford_action(user_id,
estimated_extra_cost_usd)` before proceeding. If False, the action is
queued as a recommendation surfacing the cost estimate — the ONLY
legitimate use of "Recommendation: cost-gated" allowed by the
no-recommendation-only guardrail test.

The ceiling is a per-user dollar amount. Defaults to the user's
trailing-7-day average spend × 1.5 — generous enough that normal
operation is never blocked, tight enough that runaway autonomous
expansion gets caught before it bills you for thousands.

The user can configure their own ceiling via a per-user settings
column (added in a later wave); for now the auto-computed default
suffices.
"""

from __future__ import annotations

import glob
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Floor under any auto-computed ceiling so a brand-new user with no
# spend history isn't immediately blocked. $5/day handles typical
# small-cap profile load even on first use.
_FLOOR_DAILY_USD = 5.0
# Multiplier on trailing-7-day average. 1.5x means spending up to 50%
# above your normal rate is allowed without surfacing a recommendation.
_DEFAULT_CEILING_MULTIPLIER = 1.5


def _user_profile_dbs(user_id: int) -> List[str]:
    """Return the list of profile DB paths for this user. Reads from
    the master DB to enumerate."""
    try:
        from models import _get_conn
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id FROM trading_profiles "
            "WHERE user_id = ? AND COALESCE(enabled, 1) = 1",
            (user_id,),
        ).fetchall()
        conn.close()
        out = []
        for r in rows:
            pid = r["id"] if hasattr(r, "keys") else r[0]
            path = f"quantopsai_profile_{pid}.db"
            if os.path.exists(path):
                out.append(path)
        return out
    except Exception as exc:
        logger.debug("user profile DB enumeration failed: %s", exc)
        return []


def trailing_avg_daily_spend(user_id: int, days: int = 7) -> float:
    """Average daily USD spend across this user's profiles over the
    trailing N days. 0 if no spend history."""
    from ai_cost_ledger import spend_summary
    total = 0.0
    for db_path in _user_profile_dbs(user_id):
        try:
            s = spend_summary(db_path)
            # spend_summary uses 7d / 30d windows. Use the matching key.
            key = "7d" if days == 7 else f"{days}d"
            window = s.get(key, {}) or {}
            total += float(window.get("usd", 0))
        except Exception:
            continue
    return total / max(days, 1)


def daily_ceiling_usd(user_id: int) -> float:
    """Compute today's ceiling. Trailing-7-day-avg × 1.5, floored at
    $5. A user-configurable override would go here in the future."""
    avg = trailing_avg_daily_spend(user_id, days=7)
    return max(_FLOOR_DAILY_USD, avg * _DEFAULT_CEILING_MULTIPLIER)


def today_spend(user_id: int) -> float:
    """Sum of today's USD spend across this user's profiles."""
    from ai_cost_ledger import spend_summary
    total = 0.0
    for db_path in _user_profile_dbs(user_id):
        try:
            s = spend_summary(db_path)
            total += float(s.get("today", {}).get("usd", 0))
        except Exception:
            continue
    return total


def projected_daily_spend(user_id: int,
                           extra_cost_usd: float = 0.0) -> float:
    """Best estimate of where today's spend will land if the action
    that costs `extra_cost_usd` proceeds. Today's actual spend so far
    + the projected extra cost. Conservative: doesn't try to forecast
    remaining cycles, since those vary by market hours."""
    return today_spend(user_id) + max(0.0, extra_cost_usd)


def can_afford_action(user_id: int,
                       estimated_extra_cost_usd: float = 0.0) -> bool:
    """True if the projected spend (today's so far + the estimated
    extra cost) stays at-or-below today's ceiling. False if the action
    would push us over.

    Callers should treat False as "queue this as a recommendation, not
    an auto-action" — see `format_cost_recommendation` for the
    user-facing string."""
    projected = projected_daily_spend(user_id, estimated_extra_cost_usd)
    ceiling = daily_ceiling_usd(user_id)
    return projected <= ceiling


def format_cost_recommendation(action_summary: str, user_id: int,
                                  estimated_extra_cost_usd: float) -> str:
    """The standard "cost-gated recommendation" string format. Use
    this when can_afford_action returned False — the resulting string
    starts with "Recommendation: cost-gated " which is the ONLY
    Recommendation prefix allowed by the
    `test_no_recommendation_only` guardrail."""
    today_so_far = today_spend(user_id)
    ceiling = daily_ceiling_usd(user_id)
    return (
        f"Recommendation: cost-gated — {action_summary} "
        f"(would add ${estimated_extra_cost_usd:.2f}; today's spend "
        f"${today_so_far:.2f} of ${ceiling:.2f} ceiling). "
        f"Manual approval required."
    )


def status(user_id: int) -> Dict[str, float]:
    """A quick snapshot for the UI / activity feed."""
    today = today_spend(user_id)
    ceiling = daily_ceiling_usd(user_id)
    avg = trailing_avg_daily_spend(user_id)
    return {
        "today_usd": round(today, 4),
        "ceiling_usd": round(ceiling, 4),
        "headroom_usd": round(max(0.0, ceiling - today), 4),
        "trailing_7d_avg_usd": round(avg, 4),
    }
