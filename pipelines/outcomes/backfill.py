"""Phase 5d of pipeline refactor (2026-05-11) — historical option
prediction backfill.

Pre-Phase-5c historical option rows in `ai_predictions` were
resolved using the broken `_resolve_one` math
(`(current_price - pred_price) / pred_price * 100` where
`current_price` was the underlying stock price and `pred_price`
was the option premium — produces nonsense like 4067% returns).
Phase 5a's `pipeline_kind` tag isolated those rows from stock
tuning, but option calibration / specialist learning is still
contaminated by them.

Phase 5d backfills:
  1. Find historical option rows where
     `pipeline_kind = 'option' AND status = 'resolved' AND
      option_order_id IS NULL AND occ_symbol IS NULL`.
  2. For each row, find the matching trade in the `trades` table
     (same symbol, same signal class, within ±60 minutes of the
     prediction timestamp).
  3. Populate `option_order_id` (multileg) or `occ_symbol`
     (single-leg) from the trade row.
  4. Reset the prediction to 'pending' (clearing the wrong
     `actual_return_pct` / `actual_outcome` values) so the Phase
     5c option-aware resolver re-resolves it correctly on the
     next cycle.

Idempotency: gated by `journal.is_migration_done` /
`mark_migration_done` markers — runs once per DB. The internal
WHERE clauses also self-gate (`option_order_id IS NULL`) so even
a forced re-run is safe.

Auto-runs at scheduler startup. No manual intervention required
(per the AI-driven-system policy).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)


MIGRATION_KEY = "phase_5d_option_backfill"
MATCH_WINDOW_MINUTES = 60   # ±60 min between prediction and trade


def backfill_historical_option_predictions(
    db_path: str,
    force: bool = False,
) -> Dict[str, int]:
    """Run the Phase 5d backfill on a single profile DB.

    Returns counts: {'scanned', 'linked_multileg', 'linked_single_leg',
    'no_match', 'skipped_already_done'}. Marker is set on first
    successful run; subsequent calls return immediately unless
    `force=True`.
    """
    counts = {
        "scanned": 0,
        "linked_multileg": 0,
        "linked_single_leg": 0,
        "no_match": 0,
        "skipped_already_done": 0,
    }
    if not db_path:
        return counts

    from journal import is_migration_done, mark_migration_done

    if not force and is_migration_done(db_path, MIGRATION_KEY):
        counts["skipped_already_done"] = 1
        return counts

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Find pre-Phase-5c historical option rows that need
            # re-resolution. The WHERE clause is itself idempotent —
            # any row already linked (option_order_id OR occ_symbol
            # populated) is skipped.
            rows = conn.execute(
                """SELECT id, symbol, predicted_signal, timestamp
                   FROM ai_predictions
                   WHERE pipeline_kind = 'option'
                   AND status = 'resolved'
                   AND option_order_id IS NULL
                   AND occ_symbol IS NULL"""
            ).fetchall()

            for row in rows:
                counts["scanned"] += 1
                signal = (row["predicted_signal"] or "").upper()
                pred_id = row["id"]
                symbol = row["symbol"]
                ts = row["timestamp"]

                if signal == "MULTILEG_OPEN":
                    combo_id = _find_multileg_combo_for_prediction(
                        conn, symbol, ts,
                    )
                    if combo_id:
                        _link_and_reset(conn, pred_id,
                                          option_order_id=combo_id)
                        counts["linked_multileg"] += 1
                    else:
                        counts["no_match"] += 1
                elif signal in ("OPTIONS", "OPTION_EXERCISE"):
                    occ = _find_single_leg_occ_for_prediction(
                        conn, symbol, ts,
                    )
                    if occ:
                        _link_and_reset(conn, pred_id, occ_symbol=occ)
                        counts["linked_single_leg"] += 1
                    else:
                        counts["no_match"] += 1
                else:
                    # Unknown option signal — leave alone.
                    counts["no_match"] += 1

            conn.commit()
        finally:
            conn.close()

        mark_migration_done(
            db_path, MIGRATION_KEY,
            details=(
                f"scanned={counts['scanned']} "
                f"linked_multileg={counts['linked_multileg']} "
                f"linked_single_leg={counts['linked_single_leg']} "
                f"no_match={counts['no_match']}"
            ),
        )
        logger.info(
            "Phase 5d backfill on %s: scanned=%d linked_multileg=%d "
            "linked_single_leg=%d no_match=%d",
            db_path, counts["scanned"], counts["linked_multileg"],
            counts["linked_single_leg"], counts["no_match"],
        )
    except Exception as exc:
        # Non-fatal — backfill failure means historical rows keep
        # their wrong values. Production keeps running.
        logger.warning("Phase 5d backfill failed on %s: %s", db_path, exc)

    return counts


def _find_multileg_combo_for_prediction(
    conn: sqlite3.Connection,
    symbol: str,
    pred_timestamp: str,
) -> Optional[str]:
    """Find the combo_order_id for a multileg trade that matches the
    prediction (same underlying symbol, executed within
    MATCH_WINDOW_MINUTES of the prediction).

    Multileg legs all carry signal_type='MULTILEG' in the trades
    table. We pick the combo that minimizes |trade_ts - pred_ts|.
    """
    cutoff_min, cutoff_max = _window_cutoffs(pred_timestamp)
    if not cutoff_min:
        return None
    # 2026-05-12 — exclude data_quality-tagged rows (phantom-stop
    # cascade artifacts). Without this filter, a future incident
    # that pollutes a MULTILEG leg row could resurface during
    # backfill and link to an ai_prediction, polluting alpha_decay
    # / strategy_lifecycle decisions downstream.
    from journal import data_quality_clause
    _dq = data_quality_clause(conn)
    # The legs share the underlying ticker (the `symbol` column of
    # the trade row is the underlying for multileg legs).
    rows = conn.execute(
        f"""SELECT order_id, reason, timestamp
           FROM trades
           WHERE signal_type = 'MULTILEG'
           AND symbol = ?
           AND timestamp BETWEEN ? AND ?{_dq}
           ORDER BY timestamp ASC""",
        (symbol.upper(), cutoff_min, cutoff_max),
    ).fetchall()
    if not rows:
        return None
    # Combo path: every leg of the same combo shares the same
    # order_id (parent combo id). Sequential path: each leg has a
    # distinct order_id but the parent combo id is in the reason
    # string `(combo=<id>)`. Prefer the parent id from reason when
    # present; fall back to the leg's own order_id.
    import re
    pattern = re.compile(r"\(combo=([^\)]+)\)")
    for r in rows:
        m = pattern.search(r["reason"] or "")
        if m:
            return m.group(1)
        if r["order_id"]:
            return r["order_id"]
    return None


def _find_single_leg_occ_for_prediction(
    conn: sqlite3.Connection,
    symbol: str,
    pred_timestamp: str,
) -> Optional[str]:
    """Find an OCC symbol from the trades table matching a single-leg
    option prediction (same underlying, has occ_symbol, signal_type
    = 'OPTIONS' or null with non-null occ_symbol, within window).
    """
    cutoff_min, cutoff_max = _window_cutoffs(pred_timestamp)
    if not cutoff_min:
        return None
    # 2026-05-12 — same defense as the multileg path above: exclude
    # data_quality-tagged rows so a future incident can't pollute
    # the OPTIONS-prediction-to-trades linkage during backfill.
    from journal import data_quality_clause
    _dq = data_quality_clause(conn)
    rows = conn.execute(
        f"""SELECT occ_symbol
           FROM trades
           WHERE symbol = ?
           AND occ_symbol IS NOT NULL
           AND signal_type != 'MULTILEG'
           AND timestamp BETWEEN ? AND ?{_dq}
           ORDER BY timestamp ASC LIMIT 1""",
        (symbol.upper(), cutoff_min, cutoff_max),
    ).fetchone()
    if rows and rows["occ_symbol"]:
        return rows["occ_symbol"]
    return None


def _link_and_reset(
    conn: sqlite3.Connection,
    pred_id: int,
    option_order_id: Optional[str] = None,
    occ_symbol: Optional[str] = None,
) -> None:
    """UPDATE the prediction row: set linkage fields, reset to
    pending, clear the wrong actual_* values so Phase 5c's
    resolver re-resolves on the next cycle."""
    sets = ["status = 'pending'", "actual_outcome = NULL",
             "actual_return_pct = NULL", "resolved_at = NULL",
             "resolution_price = NULL"]
    vals = []
    if option_order_id:
        sets.append("option_order_id = ?")
        vals.append(str(option_order_id))
    if occ_symbol:
        sets.append("occ_symbol = ?")
        vals.append(str(occ_symbol))
    vals.append(pred_id)
    conn.execute(
        f"UPDATE ai_predictions SET {', '.join(sets)} WHERE id = ?",
        vals,
    )


def _window_cutoffs(pred_timestamp: str):
    """Return (min_ts, max_ts) ISO strings spanning the match window
    around the prediction timestamp."""
    if not pred_timestamp:
        return None, None
    try:
        pred_dt = datetime.fromisoformat(pred_timestamp)
    except (ValueError, TypeError):
        return None, None
    delta = timedelta(minutes=MATCH_WINDOW_MINUTES)
    return (
        (pred_dt - delta).isoformat(),
        (pred_dt + delta).isoformat(),
    )
