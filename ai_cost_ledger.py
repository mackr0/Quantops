"""AI cost ledger — records every AI call and produces spend summaries.

Writes one row to `ai_cost_ledger` per `call_ai` invocation. Token counts
come from the provider SDK's usage dict (Anthropic's `usage.input_tokens` /
`usage.output_tokens`, OpenAI's `usage.prompt_tokens` / `completion_tokens`).

Rows are per-profile. For a cross-profile spend view the dashboard
aggregates over every `quantopsai_profile_*.db` on disk.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional

from ai_pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

# Once-per-process flag for the legacy-table INSERT fallback warning.
_LEGACY_LEDGER_WARNED = False


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def log_ai_call(
    db_path: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    purpose: str = "",
    call_id: Optional[str] = None,
    cached_tokens: int = 0,
) -> None:
    """Persist a single AI call. Non-raising — a ledger failure must never
    break the calling pipeline.

    `call_id` joins this row to any ai_shadow_calls rows produced by
    the shadow dispatcher for the same primary invocation.

    `cached_tokens` (2026-07-02): prompt tokens the provider served from its
    implicit cache — a SUBSET of input_tokens, billed at ~10% of the input
    rate. Priced honestly here so a cache hit isn't overstated ~10x in the
    ledger. Stored so caching claims are measurable, never assumed.
    """
    if not db_path:
        return
    try:
        cached_tokens = max(0, min(int(cached_tokens or 0),
                                   int(input_tokens or 0)))
        cost = estimate_cost_usd(model, input_tokens, output_tokens,
                                 cached_tokens=cached_tokens)
        conn = sqlite3.connect(db_path)
        try:
            try:
                conn.execute(
                    """INSERT INTO ai_cost_ledger
                         (provider, model, input_tokens, output_tokens,
                          purpose, estimated_cost_usd, call_id,
                          cached_tokens)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (provider, model, int(input_tokens or 0),
                     int(output_tokens or 0), purpose, cost, call_id,
                     cached_tokens),
                )
            except sqlite3.OperationalError:
                # Pre-migration table (hand-built test DBs / a DB that
                # hasn't run init_db yet) — write the legacy shape rather
                # than dropping the row. Warn ONCE per process so schema
                # drift can't silently disable cached-token telemetry.
                global _LEGACY_LEDGER_WARNED
                if not _LEGACY_LEDGER_WARNED:
                    _LEGACY_LEDGER_WARNED = True
                    logger.warning(
                        "ai_cost_ledger missing cached_tokens column on %s "
                        "— writing legacy rows; run init_db to migrate "
                        "(cached-token telemetry not recorded)", db_path,
                    )
                conn.execute(
                    """INSERT INTO ai_cost_ledger
                         (provider, model, input_tokens, output_tokens,
                          purpose, estimated_cost_usd, call_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (provider, model, int(input_tokens or 0),
                     int(output_tokens or 0), purpose, cost, call_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("ai cost ledger write failed: %s", exc)


# ---------------------------------------------------------------------------
# Read path — spend summaries
# ---------------------------------------------------------------------------

def by_model_today(db_path: str) -> List[Dict[str, Any]]:
    """Per-(provider, model) spend for TODAY (ET) on one profile DB.

    Returns [{provider, model, calls, usd}] sorted by usd desc. This is the
    raw material for the dashboard's "cost per LLM" breakdown — it shows the
    operator how much each model (primary vs. fallback) is actually costing.
    Safe on missing table / db: returns [].
    """
    out: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return out
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et_today = datetime.now(
            ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT provider, model, COUNT(*) AS n, "
            "       COALESCE(SUM(estimated_cost_usd), 0) AS usd "
            "FROM ai_cost_ledger WHERE timestamp >= ? "
            "GROUP BY provider, model ORDER BY usd DESC",
            (et_today,),
        ).fetchall()
        out = [{"provider": r["provider"], "model": r["model"],
                "calls": int(r["n"] or 0),
                "usd": round(float(r["usd"] or 0), 4)}
               for r in rows]
    except sqlite3.OperationalError:
        pass  # table not created yet — return []
    finally:
        conn.close()
    return out


def merge_model_breakdowns(
        breakdowns: "List[List[Dict[str, Any]]]") -> List[Dict[str, Any]]:
    """Combine per-profile ``by_model_today()`` lists into one book-wide
    breakdown, summed per (provider, model), sorted by usd desc. Used by the
    dashboard to show total cost-per-LLM across all profiles."""
    agg: Dict[tuple, Dict[str, Any]] = {}
    for bd in breakdowns:
        for r in bd or []:
            key = (r.get("provider"), r.get("model"))
            a = agg.setdefault(key, {"provider": r.get("provider"),
                                     "model": r.get("model"),
                                     "calls": 0, "usd": 0.0})
            a["calls"] += int(r.get("calls") or 0)
            a["usd"] += float(r.get("usd") or 0)
    for a in agg.values():
        a["usd"] = round(a["usd"], 4)
    return sorted(agg.values(), key=lambda x: -x["usd"])


def spend_summary(db_path: str) -> Dict[str, Any]:
    """Return 1d / 7d / 30d spend totals + call counts for one profile DB.

    Safe on missing table / empty db: returns zeros.
    """
    result = {
        "today": {"calls": 0, "usd": 0.0, "input_tokens": 0, "output_tokens": 0},
        "7d":    {"calls": 0, "usd": 0.0, "input_tokens": 0, "output_tokens": 0},
        "30d":   {"calls": 0, "usd": 0.0, "input_tokens": 0, "output_tokens": 0},
        "by_purpose_30d": [],
        "by_model_30d": [],
    }
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return result

    try:
        # Use ET date boundaries so "today" means today in Eastern Time,
        # not UTC (which flips to the next day at 7/8 PM ET).
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        for key, window in (("today", "start of day"),
                            ("7d",    "-7 days"),
                            ("30d",   "-30 days")):
            if key == "today":
                sql_where = f"timestamp >= '{et_today}'"
            else:
                sql_where = "timestamp >= datetime('now', '%s')" % window
            row = conn.execute(
                """SELECT COUNT(*) AS n,
                          COALESCE(SUM(estimated_cost_usd), 0) AS usd,
                          COALESCE(SUM(input_tokens), 0)  AS in_tok,
                          COALESCE(SUM(output_tokens), 0) AS out_tok
                   FROM ai_cost_ledger
                   WHERE """ + sql_where,
            ).fetchone()
            if row:
                result[key] = {
                    "calls": int(row["n"] or 0),
                    "usd": float(row["usd"] or 0),
                    "input_tokens": int(row["in_tok"] or 0),
                    "output_tokens": int(row["out_tok"] or 0),
                }

        by_purpose = conn.execute(
            """SELECT COALESCE(purpose, '') AS purpose,
                      COUNT(*) AS n,
                      COALESCE(SUM(estimated_cost_usd), 0) AS usd
               FROM ai_cost_ledger
               WHERE timestamp >= datetime('now', '-30 days')
               GROUP BY purpose
               ORDER BY usd DESC""",
        ).fetchall()
        result["by_purpose_30d"] = [
            {"purpose": r["purpose"] or "uncategorized",
             "calls": int(r["n"]),
             "usd": round(float(r["usd"]), 4)}
            for r in by_purpose
        ]

        by_model = conn.execute(
            """SELECT model,
                      COUNT(*) AS n,
                      COALESCE(SUM(estimated_cost_usd), 0) AS usd
               FROM ai_cost_ledger
               WHERE timestamp >= datetime('now', '-30 days')
               GROUP BY model
               ORDER BY usd DESC""",
        ).fetchall()
        result["by_model_30d"] = [
            {"model": r["model"],
             "calls": int(r["n"]),
             "usd": round(float(r["usd"]), 4)}
            for r in by_model
        ]
    except sqlite3.OperationalError:
        # Table doesn't exist yet — return zeros
        pass
    finally:
        conn.close()

    return result
