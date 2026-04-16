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
) -> None:
    """Persist a single AI call. Non-raising — a ledger failure must never
    break the calling pipeline."""
    if not db_path:
        return
    try:
        cost = estimate_cost_usd(model, input_tokens, output_tokens)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO ai_cost_ledger
                     (provider, model, input_tokens, output_tokens,
                      purpose, estimated_cost_usd)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (provider, model, int(input_tokens or 0),
                 int(output_tokens or 0), purpose, cost),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("ai cost ledger write failed: %s", exc)


# ---------------------------------------------------------------------------
# Read path — spend summaries
# ---------------------------------------------------------------------------

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
        for key, window in (("today", "-1 day"),
                            ("7d",    "-7 days"),
                            ("30d",   "-30 days")):
            row = conn.execute(
                """SELECT COUNT(*) AS n,
                          COALESCE(SUM(estimated_cost_usd), 0) AS usd,
                          COALESCE(SUM(input_tokens), 0)  AS in_tok,
                          COALESCE(SUM(output_tokens), 0) AS out_tok
                   FROM ai_cost_ledger
                   WHERE timestamp >= datetime('now', ?)""",
                (window,),
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
