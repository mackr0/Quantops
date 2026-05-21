"""Fine-tune model registry — CRUD over the `finetune_models` table.

The registry is the source of truth for which fine-tuned model (if
any) is promoted for live use, and the lifecycle of every model the
weekly job has produced (created → shadow → live → retired). docs/20
§6.4.

The table lives on the MASTER db (quantopsai.db) — fine-tune is a
fleet-wide concern (pooled training), not per-profile. Created
lazily via `ensure_schema()` so importing this module never assumes
the table exists.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MASTER_DB = "quantopsai.db"


def _conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or _MASTER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(db_path: Optional[str] = None) -> None:
    """Create finetune_models + finetune_evaluations if absent.
    Idempotent (CREATE TABLE IF NOT EXISTS)."""
    with closing(_conn(db_path)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS finetune_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                openai_model_id TEXT NOT NULL UNIQUE,
                parent_model_id TEXT,
                training_window_start TEXT,
                training_window_end TEXT,
                training_token_count INTEGER,
                training_cost_usd REAL,
                validation_loss REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                promoted_to_shadow_at TEXT,
                promoted_to_live_at TEXT,
                retired_at TEXT,
                retirement_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_finetune_models_created
                ON finetune_models(created_at DESC);

            CREATE TABLE IF NOT EXISTS finetune_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id TEXT NOT NULL,
                eval_set_size INTEGER,
                win_rate_finetuned REAL,
                win_rate_base REAL,
                win_rate_lift_pct REAL,
                agreement_with_base_pct REAL,
                per_direction_win_rate_json TEXT,
                by_strategy_lift_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_finetune_evals_model
                ON finetune_evaluations(model_id);
        """)
        conn.commit()


def register_model(
    openai_model_id: str,
    *,
    parent_model_id: Optional[str] = None,
    training_window_start: Optional[str] = None,
    training_window_end: Optional[str] = None,
    training_token_count: Optional[int] = None,
    training_cost_usd: Optional[float] = None,
    validation_loss: Optional[float] = None,
    db_path: Optional[str] = None,
) -> int:
    """Insert a newly-trained model. Returns its row id. Idempotent
    on openai_model_id (INSERT OR IGNORE then fetch)."""
    ensure_schema(db_path)
    with closing(_conn(db_path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO finetune_models "
            "(openai_model_id, parent_model_id, training_window_start, "
            " training_window_end, training_token_count, "
            " training_cost_usd, validation_loss) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (openai_model_id, parent_model_id, training_window_start,
             training_window_end, training_token_count,
             training_cost_usd, validation_loss),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM finetune_models WHERE openai_model_id = ?",
            (openai_model_id,),
        ).fetchone()
    return int(row["id"]) if row else -1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def promote_to_shadow(openai_model_id: str,
                      db_path: Optional[str] = None) -> bool:
    return _stamp(openai_model_id, "promoted_to_shadow_at", db_path)


def promote_to_live(openai_model_id: str,
                    db_path: Optional[str] = None) -> bool:
    return _stamp(openai_model_id, "promoted_to_live_at", db_path)


def retire_model(openai_model_id: str, reason: str,
                 db_path: Optional[str] = None) -> bool:
    ensure_schema(db_path)
    with closing(_conn(db_path)) as conn:
        cur = conn.execute(
            "UPDATE finetune_models SET retired_at = ?, "
            "retirement_reason = ? WHERE openai_model_id = ?",
            (_now(), reason, openai_model_id),
        )
        conn.commit()
        return cur.rowcount > 0


def _stamp(openai_model_id: str, column: str,
           db_path: Optional[str]) -> bool:
    ensure_schema(db_path)
    with closing(_conn(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE finetune_models SET {column} = ? "
            f"WHERE openai_model_id = ?",
            (_now(), openai_model_id),
        )
        conn.commit()
        return cur.rowcount > 0


def latest_live_model(db_path: Optional[str] = None) -> Optional[str]:
    """The most-recently promoted-to-live, not-retired model id, or
    None. This is what inference routes to when a profile has
    use_finetuned_ai=1."""
    ensure_schema(db_path)
    with closing(_conn(db_path)) as conn:
        row = conn.execute(
            "SELECT openai_model_id FROM finetune_models "
            "WHERE promoted_to_live_at IS NOT NULL "
            "AND retired_at IS NULL "
            "ORDER BY promoted_to_live_at DESC LIMIT 1"
        ).fetchone()
    return row["openai_model_id"] if row else None


def list_models(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_schema(db_path)
    with closing(_conn(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM finetune_models ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]
