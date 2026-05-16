"""Auto-expire gate-tightening tuning changes that haven't proven
their value.

Architectural maxim (Mack, 2026-05-16):
  "It shouldn't make it harder to trade — it should make it harder
   to make BAD trades."

A gate-tightening is JUSTIFIED if it's reducing the rate of bad
trades. It's UNJUSTIFIED if it's just reducing the rate of trades.
Auto-expiry is the mechanism that distinguishes the two: if a
tightening has accumulated real evidence (>=20 resolved predictions
in its window) and has NOT visibly improved win rate, the gate is
making it harder to trade without making it harder to make bad
trades. Therefore, revert.

Background. The 2026-05-14 over-restriction collapse was caused by 14
days of compounding gate-tightenings (correlation caps, strategy
deprecations, raised confidence thresholds, etc.) — each one passed
its own sanity check but the cumulative drift killed stock entries.
The fourth permanent guardrail listed in
`project_self_tuner_overcorrection_2026_05_14.md` was auto-expiry on
restrictions: a tightening should revert automatically if it doesn't
show evidence of improving win rate within a window.

Decision rule (per Mack, 2026-05-16 — evidence-based, not time-based):

  For each tuning_history row where:
    category(adjustment_type) == 'gate_tighten'
    AND timestamp >= 7 days ago
    AND no subsequent row already changed the same parameter
    AND >= 20 predictions resolved since the change
    AND outcome_after != 'improved' (so: worsened, unchanged, or
      still 'pending' despite having 20+ samples)
  → revert the parameter to old_value
  → log a new tuning_history row tagged 'auto_expiry_revert'
    (categorized as 'neutral' so it doesn't itself auto-expire)
  → update the original row's outcome_after to 'auto_expired'

The 20-sample minimum is the key evidence gate: a change with <20
samples in its window doesn't HAVE evidence yet; we defer the
revert decision (a future cycle will revisit when samples accrue).
Without this gate the auto-expiry would itself become the kind of
mechanical-over-time restriction the system is trying to escape.

Special handling per parameter shape:
  - Numeric column on trading_profiles (most cases): cast old_value
    and pass to update_trading_profile(pid, **{col: val}).
  - `deprecate:<strategy_name>`: delete the row from per-profile
    `deprecated_strategies` table (un-deprecate).
  - `weight:<signal_name>`: update the signal_weights JSON column —
    remove the override (signal returns to default 1.0).
  - `enable_self_tuning`: boolean cast.

Unrecognized parameter shapes are SKIPPED (logged at INFO), never
guessed at. Auto-reverting a parameter we don't know how to write
back is worse than leaving the gate in place.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from tuning_categories import categorize as _categorize

logger = logging.getLogger(__name__)


# Defaults — exposed for tests; production values rarely tuned.
DEFAULT_TTL_DAYS = 7
DEFAULT_MIN_SAMPLES_SINCE_CHANGE = 20


def _count_resolved_predictions_since(
    profile_db_path: str, since_timestamp: str,
) -> int:
    """Count predictions in this profile's DB that resolved AFTER the
    given timestamp. This is the 'samples accrued since the tuning
    change' figure that gates the auto-expiry decision."""
    try:
        with closing(sqlite3.connect(profile_db_path)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status = 'resolved' AND resolved_at >= ?",
                (since_timestamp,),
            ).fetchone()
            return int(row[0] or 0) if row else 0
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        logger.debug(
            "resolved-predictions count failed for %s since %s: %s: %s",
            profile_db_path, since_timestamp, type(exc).__name__, exc,
        )
        return 0


def _is_eligible_for_revert(
    row: Dict[str, Any], samples_since: int,
    ttl_days: int, min_samples: int,
) -> Tuple[bool, str]:
    """Return (eligible, reason). reason is empty when eligible,
    otherwise explains why the row was skipped."""
    if _categorize(row.get("adjustment_type")) != "gate_tighten":
        return False, "not a gate_tighten"
    if row.get("outcome_after") == "improved":
        return False, "outcome_after=improved"
    if row.get("outcome_after") == "auto_expired":
        return False, "already auto-expired"
    if samples_since < min_samples:
        return (
            False,
            f"insufficient samples ({samples_since} < {min_samples}) "
            "since change; defer until more data accrues",
        )
    try:
        ts = datetime.fromisoformat(row.get("timestamp", "").replace("Z", ""))
    except (TypeError, ValueError):
        return False, "unparseable timestamp"
    if ts > datetime.utcnow() - timedelta(days=ttl_days):
        return False, f"too recent (TTL is {ttl_days}d)"
    return True, ""


def _newer_change_exists_for_param(
    history: List[Dict[str, Any]],
    profile_id: int, parameter_name: str, change_timestamp: str,
) -> bool:
    """Check whether ANY later tuning row already touched the same
    parameter on this profile. If so, the auto-expiry should skip —
    the system has already moved on. Avoids double-reverting."""
    for h in history:
        if h.get("profile_id") != profile_id:
            continue
        if h.get("parameter_name") != parameter_name:
            continue
        if h.get("timestamp", "") <= change_timestamp:
            continue
        return True
    return False


def _cast_old_value(old_value: Any) -> Any:
    """Best-effort cast of stored old_value string back to its native
    type. SQLite stored it as TEXT but most columns are INTEGER /
    REAL / TEXT — try int → float → str."""
    if old_value is None or old_value == "":
        return None
    s = str(old_value).strip()
    if s.lower() in ("true", "false"):
        return 1 if s.lower() == "true" else 0
    try:
        if "." not in s and "e" not in s.lower():
            return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _revert_to_old_value(
    profile_id: int, parameter_name: str, old_value: Any,
    profile_db_path: str,
) -> Tuple[bool, str]:
    """Apply the revert. Returns (success, detail).

    Three shape families:
      - `deprecate:<strategy>` → un-deprecate in per-profile DB
      - `weight:<signal>`      → remove key from signal_weights JSON
      - everything else        → numeric/text update_trading_profile
    """
    if not parameter_name:
        return False, "empty parameter_name"

    # 1. Strategy un-deprecation.
    if parameter_name.startswith("deprecate:"):
        strategy_type = parameter_name.split(":", 1)[1]
        try:
            with closing(sqlite3.connect(profile_db_path)) as conn:
                conn.execute(
                    "UPDATE deprecated_strategies "
                    "SET restored_at = datetime('now') "
                    "WHERE strategy_type = ? AND restored_at IS NULL",
                    (strategy_type,),
                )
                conn.commit()
            return True, f"un-deprecated {strategy_type}"
        except Exception as exc:
            return False, f"un-deprecate failed: {type(exc).__name__}: {exc}"

    # 2. Signal-weight removal (Layer-2 weight returns to default 1.0).
    if parameter_name.startswith("weight:"):
        signal_name = parameter_name.split(":", 1)[1]
        try:
            from models import get_trading_profile, update_trading_profile
            prof = get_trading_profile(profile_id) or {}
            raw = prof.get("signal_weights") or "{}"
            weights = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if signal_name in weights:
                del weights[signal_name]
            update_trading_profile(profile_id, signal_weights=json.dumps(weights))
            return True, f"removed weight override for {signal_name}"
        except Exception as exc:
            return False, f"weight revert failed: {type(exc).__name__}: {exc}"

    # 3. Direct column update on trading_profiles.
    cast = _cast_old_value(old_value)
    if cast is None:
        return False, f"cannot cast old_value {old_value!r}"
    try:
        from models import update_trading_profile
        update_trading_profile(profile_id, **{parameter_name: cast})
        return True, f"reverted {parameter_name} to {cast!r}"
    except Exception as exc:
        return False, f"column revert failed: {type(exc).__name__}: {exc}"


def _mark_row_auto_expired(row_id: int) -> None:
    """Set outcome_after='auto_expired' on the original tuning_history
    row so subsequent passes don't reprocess."""
    try:
        from models import _get_conn
        with closing(_get_conn()) as conn:
            conn.execute(
                "UPDATE tuning_history SET outcome_after = 'auto_expired', "
                "reviewed_at = datetime('now') WHERE id = ?",
                (row_id,),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(
            "mark_row_auto_expired failed for row %s: %s: %s",
            row_id, type(exc).__name__, exc,
        )


def revert_expired_gate_tightens(
    profile_id: int, user_id: int, profile_db_path: str,
    ttl_days: int = DEFAULT_TTL_DAYS,
    min_samples: int = DEFAULT_MIN_SAMPLES_SINCE_CHANGE,
) -> List[Dict[str, Any]]:
    """Process auto-expiry for ONE profile. Returns list of revert
    actions taken (or attempted), one dict per evaluated candidate."""
    from models import get_tuning_history, log_tuning_change

    history = get_tuning_history(profile_id, limit=500)
    # Sort newest-first; we iterate oldest-first for processing.
    history_sorted_oldest = sorted(
        history, key=lambda h: h.get("timestamp", ""),
    )

    actions = []
    for row in history_sorted_oldest:
        # Pre-filters before the expensive sample-count.
        if _categorize(row.get("adjustment_type")) != "gate_tighten":
            continue
        if row.get("outcome_after") in ("improved", "auto_expired"):
            continue
        try:
            ts = datetime.fromisoformat(
                (row.get("timestamp") or "").replace("Z", ""),
            )
        except (TypeError, ValueError):
            continue
        if ts > datetime.utcnow() - timedelta(days=ttl_days):
            continue
        if _newer_change_exists_for_param(
            history_sorted_oldest, profile_id,
            row.get("parameter_name"), row.get("timestamp"),
        ):
            actions.append({
                "row_id": row.get("id"),
                "parameter_name": row.get("parameter_name"),
                "action": "skip",
                "reason": "newer change already moved this parameter",
            })
            continue

        samples = _count_resolved_predictions_since(
            profile_db_path, row.get("timestamp"),
        )
        eligible, why = _is_eligible_for_revert(
            row, samples, ttl_days, min_samples,
        )
        if not eligible:
            actions.append({
                "row_id": row.get("id"),
                "parameter_name": row.get("parameter_name"),
                "action": "skip",
                "reason": why,
                "samples_since": samples,
            })
            continue

        # Eligible. Apply revert.
        ok, detail = _revert_to_old_value(
            profile_id, row.get("parameter_name"),
            row.get("old_value"), profile_db_path,
        )
        if not ok:
            actions.append({
                "row_id": row.get("id"),
                "parameter_name": row.get("parameter_name"),
                "action": "revert_failed",
                "reason": detail,
                "samples_since": samples,
            })
            continue

        # Log the revert as a new tuning_history row.
        try:
            log_tuning_change(
                profile_id=profile_id, user_id=user_id,
                adjustment_type="auto_expiry_revert",
                parameter_name=row.get("parameter_name"),
                old_value=str(row.get("new_value")),
                new_value=str(row.get("old_value")),
                reason=(
                    f"Auto-expiry: gate-tightening from "
                    f"{(row.get('timestamp') or '')[:10]} did not "
                    f"improve win rate after {samples} resolved "
                    f"predictions (outcome was "
                    f"{row.get('outcome_after') or 'pending'}). "
                    f"Reverting to allow default-bias-toward-trading."
                ),
            )
        except Exception as exc:
            logger.warning(
                "auto_expiry_revert log failed for row %s: %s: %s",
                row.get("id"), type(exc).__name__, exc,
            )
        _mark_row_auto_expired(row.get("id"))

        actions.append({
            "row_id": row.get("id"),
            "parameter_name": row.get("parameter_name"),
            "action": "reverted",
            "detail": detail,
            "samples_since": samples,
            "original_outcome": row.get("outcome_after"),
        })

    return actions


def run_auto_expiry_for_all_profiles(
    ttl_days: int = DEFAULT_TTL_DAYS,
    min_samples: int = DEFAULT_MIN_SAMPLES_SINCE_CHANGE,
) -> Dict[int, List[Dict[str, Any]]]:
    """Process all enabled profiles. Returns {profile_id: actions[]}."""
    from models import get_active_profiles
    profiles = get_active_profiles()
    results: Dict[int, List[Dict[str, Any]]] = {}
    for prof in profiles:
        pid = prof.get("id")
        if not pid or not prof.get("enabled"):
            continue
        db_path = f"quantopsai_profile_{pid}.db"
        try:
            results[pid] = revert_expired_gate_tightens(
                profile_id=pid,
                user_id=prof.get("user_id", 1),
                profile_db_path=db_path,
                ttl_days=ttl_days,
                min_samples=min_samples,
            )
        except Exception as exc:
            logger.warning(
                "auto_expiry for profile %s failed: %s: %s",
                pid, type(exc).__name__, exc,
            )
            results[pid] = []
    return results
