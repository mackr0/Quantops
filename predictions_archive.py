"""Predictions archive — persist ai_predictions + ai_cycles + specialist
outcomes to JSONL before each experiment reset (2026-05-19 Phase B1).

Per memory rule [Data integrity is paramount] + the structural
need for a fine-tune dataset that survives the wipe-and-restart
cycle:

  - `reset_for_clean_experiment.py` wipes ai_predictions on every
    reset. Today's reset alone threw away ~20K predictions
    accumulated over the prior month.
  - Without archiving, the fine-tune dataset can never grow past
    one experiment's worth of data.
  - Archiving before wipe gives us a cumulative corpus across all
    experiments, format-stable per profile + reset event, ready
    for the fine-tune pipeline whenever it ships (Phase 4b).

Archive layout:
    predictions_archive/
      {profile_id}/
        {reset_yyyymmdd_hhmmss}/
          predictions.jsonl       # one ai_predictions row per line
          cycles.jsonl             # one ai_cycles row per line
          specialist_outcomes.jsonl  # one specialist verdict per line

Format: each line is the full row as a dict, JSON-encoded. No
schema gymnastics — the dataset builder later parses whatever
columns exist.

Call from the reset scripts BEFORE the journal wipe:

    from predictions_archive import archive_predictions
    archive_predictions(
        db_path=f"quantopsai_profile_{pid}.db",
        profile_id=pid,
        archive_root="predictions_archive",
    )
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    """UTC timestamp suitable for a directory name (no colons)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _dump_table_to_jsonl(conn: sqlite3.Connection, table: str,
                          out_path: Path) -> int:
    """Read every row from `table`, write one JSON object per line to
    `out_path`. Returns the row count written. If the table doesn't
    exist, writes an empty file and returns 0 — the reset workflow
    expects the archive directory to be complete regardless of which
    tables happen to be populated on a given profile DB."""
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
    except sqlite3.OperationalError as exc:
        logger.info("archive: table %s missing on this DB (%s); writing empty file",
                     table, exc)
        out_path.write_text("")
        return 0
    cols = [d[0] for d in cur.description]
    n = 0
    with out_path.open("w") as f:
        for row in cur:
            obj = {c: row[i] for i, c in enumerate(cols)}
            f.write(json.dumps(obj, default=str) + "\n")
            n += 1
    return n


def archive_predictions(db_path: str, profile_id: int,
                         archive_root: str = "predictions_archive",
                         reset_timestamp: Optional[str] = None,
                         ) -> Dict[str, int]:
    """Archive ai_predictions + ai_cycles + specialist_outcomes +
    option_proposal_outcomes (veto counterfactuals) for one profile to JSONL
    files under archive_root/{profile_id}/{ts}/.

    Returns {table_name: rows_archived} so callers can verify the
    archive landed before wiping the source. Defensive: each table
    is dumped independently — a missing table doesn't abort the
    archive of others (a profile might predate ai_cycles even though
    most don't).

    The default reset_timestamp uses UTC-now formatted as YYYYMMDD_HHMMSS
    so multiple resets on the same day get distinct directories.
    """
    if not os.path.exists(db_path):
        logger.info(
            "archive: db_path %s missing — nothing to archive for profile %s",
            db_path, profile_id,
        )
        return {}
    reset_timestamp = reset_timestamp or _iso_now()
    out_dir = Path(archive_root) / str(profile_id) / reset_timestamp
    out_dir.mkdir(parents=True, exist_ok=True)

    counts: Dict[str, int] = {}
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            counts["predictions"] = _dump_table_to_jsonl(
                conn, "ai_predictions", out_dir / "predictions.jsonl",
            )
            counts["cycles"] = _dump_table_to_jsonl(
                conn, "ai_cycles", out_dir / "cycles.jsonl",
            )
            counts["specialist_outcomes"] = _dump_table_to_jsonl(
                conn, "specialist_outcomes",
                out_dir / "specialist_outcomes.jsonl",
            )
            # Selection-engine veto counterfactuals — the AI's proposed-then-
            # vetoed spreads + their would-be P&L. High-value decision-quality
            # data for the fine-tune corpus; archive it before a reset wipes it.
            counts["option_proposal_outcomes"] = _dump_table_to_jsonl(
                conn, "option_proposal_outcomes",
                out_dir / "option_proposal_outcomes.jsonl",
            )
    except Exception as exc:
        logger.warning(
            "archive: profile %s archive failed (%s: %s) — "
            "DO NOT WIPE the source DB until investigating",
            profile_id, type(exc).__name__, exc,
        )
        raise
    logger.info(
        "archive: profile %s → %s : %s",
        profile_id, out_dir, counts,
    )
    return counts


def archive_all_active_profiles(archive_root: str = "predictions_archive",
                                  reset_timestamp: Optional[str] = None,
                                  ) -> Dict[int, Dict[str, int]]:
    """Archive predictions for every active profile in one batch.

    Useful as a single call from the reset scripts. Iterates active
    profile ids via models.get_active_profile_ids; archives each
    profile's per-profile DB. Returns {profile_id: counts}.
    """
    from models import get_active_profile_ids
    ts = reset_timestamp or _iso_now()
    out: Dict[int, Dict[str, int]] = {}
    for pid in get_active_profile_ids():
        db_path = f"quantopsai_profile_{pid}.db"
        try:
            out[pid] = archive_predictions(
                db_path=db_path, profile_id=pid,
                archive_root=archive_root,
                reset_timestamp=ts,
            )
        except Exception as exc:
            logger.warning(
                "archive_all: profile %s SKIPPED (%s: %s)",
                pid, type(exc).__name__, exc,
            )
            out[pid] = {"error": str(exc)}
    return out
