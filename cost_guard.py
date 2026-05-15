"""Cost guard — cross-cutting daily-spend ceiling enforcement.

Two enforcement paths:

1. SELF-TUNER (advisory). Tuner actions (commission strategy, apply
   parameter tuning, expand guardrails) call `can_afford_action(...)`
   before proceeding; if False the action is queued as a
   "Recommendation: cost-gated" instead of auto-applied. This is the
   ONLY legitimate use of the "Recommendation:" prefix allowed by the
   no-recommendation-only guardrail test.

2. PIPELINE (hard block — added 2026-05-15). Every AI call routed
   through `ai_providers.call_ai` / `call_ai_structured` is gated by
   `can_afford_action(...)` against a worst-case cost estimate
   (len(prompt)/3 input tokens + max_tokens output, priced via
   ai_pricing). If the call would push today's spend past the
   ceiling, the call raises `CostCapExceeded` instead of hitting the
   provider — caught by the trade pipeline's existing exception
   handler, logged to activity_log, and surfaced as a dashboard
   banner. Today's running spend is recomputed every call so the
   block fires at the boundary, not after.

The ceiling is per-user. User can set an explicit override on the
settings page (`users.daily_cost_ceiling_usd`); without one, the
ceiling auto-computes as `max($5, trailing_7d_avg × 1.5)`.
"""

from __future__ import annotations

import glob
import logging
import os
import sqlite3
from contextlib import closing
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
        with closing(_get_conn()) as conn:
            rows = conn.execute(
                "SELECT id FROM trading_profiles "
                "WHERE user_id = ? AND COALESCE(enabled, 1) = 1",
                (user_id,),
            ).fetchall()
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
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _sp_exc:
            # Per-DB spend aggregation loop; one bad DB shouldn't
            # kill cross-profile cost trailing-avg. Surface for follow-up.
            logger.debug(
                "cost_guard spend aggregation failed: %s: %s",
                type(_sp_exc).__name__, _sp_exc,
            )
            continue
    return total / max(days, 1)


def daily_ceiling_usd(user_id: int) -> float:
    """Compute today's ceiling. User-configured override wins if set;
    else auto-compute as trailing-7-day-avg × 1.5, floored at $5."""
    # User override takes precedence — they've explicitly chosen a cap.
    try:
        from models import _get_conn
        with closing(_get_conn()) as conn:
            row = conn.execute(
                "SELECT daily_cost_ceiling_usd FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row and row[0] is not None and float(row[0]) > 0:
            return float(row[0])
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            KeyError, ValueError, TypeError, OSError) as _uo_exc:
        # User-override read; falls through to auto-derived ceiling.
        # Surface for follow-up.
        logger.debug(
            "cost_guard user-override ceiling read failed: %s: %s",
            type(_uo_exc).__name__, _uo_exc,
        )
    avg = trailing_avg_daily_spend(user_id, days=7)
    return max(_FLOOR_DAILY_USD, avg * _DEFAULT_CEILING_MULTIPLIER)


def ceiling_source(user_id: int) -> str:
    """Returns 'user' if the ceiling is user-set, else 'auto' for the
    auto-computed default. Useful for the UI to show provenance."""
    try:
        from models import _get_conn
        with closing(_get_conn()) as conn:
            row = conn.execute(
                "SELECT daily_cost_ceiling_usd FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row and row[0] is not None and float(row[0]) > 0:
            return "user"
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            KeyError, ValueError, TypeError, OSError) as _uo_exc:
        # User-override read; falls through to auto attribution.
        # Surface for follow-up.
        logger.debug(
            "cost_guard user-override attribution read failed: %s: %s",
            type(_uo_exc).__name__, _uo_exc,
        )
    return "auto"


def today_spend(user_id: int) -> float:
    """Sum of today's USD spend across this user's profiles."""
    from ai_cost_ledger import spend_summary
    total = 0.0
    for db_path in _user_profile_dbs(user_id):
        try:
            s = spend_summary(db_path)
            total += float(s.get("today", {}).get("usd", 0))
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _ts_exc:
            # Per-DB today-spend aggregation loop; one bad DB
            # shouldn't kill cross-profile total. Surface for follow-up.
            logger.debug(
                "cost_guard today-spend aggregation failed: %s: %s",
                type(_ts_exc).__name__, _ts_exc,
            )
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


class CostCapExceeded(Exception):
    """Raised when an AI call would push today's spend past the user's
    daily ceiling. Caught by the trade pipeline's existing exception
    handler so the cycle skips this call instead of crashing. Carries
    a `recommendation` string suitable for the activity log / dashboard
    banner."""

    def __init__(self, user_id: int, estimated_cost_usd: float,
                 action_summary: str = "AI call"):
        self.user_id = user_id
        self.estimated_cost_usd = estimated_cost_usd
        self.action_summary = action_summary
        self.recommendation = format_cost_recommendation(
            action_summary, user_id, estimated_cost_usd,
        )
        super().__init__(self.recommendation)


def user_id_for_db_path(db_path: str) -> Optional[int]:
    """Map a profile DB path (e.g. 'quantopsai_profile_4.db') to the
    owning user_id. Returns None if the path doesn't carry a numeric
    profile id or the lookup fails — callers must treat None as
    "cannot enforce per-user cap on this call."

    The cap-enforcement path is the only caller; lookup failures fall
    open (call proceeds without the cap check) rather than blocking
    legitimate work on a path-parsing edge case."""
    if not db_path:
        return None
    import re
    m = re.search(r"profile_(\d+)\.db$", db_path)
    if not m:
        return None
    pid = int(m.group(1))
    try:
        from models import _get_conn
        with closing(_get_conn()) as conn:
            row = conn.execute(
                "SELECT user_id FROM trading_profiles WHERE id = ?", (pid,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            ValueError, TypeError, OSError) as exc:
        logger.debug(
            "user_id_for_db_path lookup failed for %s: %s: %s",
            db_path, type(exc).__name__, exc,
        )
        return None


def status(user_id: int) -> Dict[str, Any]:
    """A quick snapshot for the UI / activity feed."""
    today = today_spend(user_id)
    ceiling = daily_ceiling_usd(user_id)
    avg = trailing_avg_daily_spend(user_id)
    return {
        "today_usd": round(today, 4),
        "ceiling_usd": round(ceiling, 4),
        "headroom_usd": round(max(0.0, ceiling - today), 4),
        "trailing_7d_avg_usd": round(avg, 4),
        "ceiling_source": ceiling_source(user_id),
    }
