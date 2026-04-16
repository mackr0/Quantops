"""Reconstruct cycle_data_<id>.json from per-profile DB history.

The dashboard's "AI Brain" panel reads `cycle_data_<profile_id>.json`,
which is a snapshot of the most recent scan-and-trade cycle's decisions
+ shortlist + AI reasoning. The file is written at the end of every
trade cycle (`trade_pipeline._save_cycle_data`).

If the file is missing (e.g. wiped by a deploy with --delete + missing
exclude), the dashboard shows "Waiting for first cycle..." until the
next live scan runs. This script bridges the gap by reconstructing a
plausible cycle_data file from the most recent ai_predictions in the
per-profile DB.

Usage:
    python recover_cycle_data.py                 # all profiles
    python recover_cycle_data.py <profile_id>    # one profile
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from typing import Dict, List


def _profile_db(profile_id: int) -> str:
    return f"quantopsai_profile_{profile_id}.db"


def _list_profiles() -> List[Dict]:
    conn = sqlite3.connect("quantopsai.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, name, market_type FROM trading_profiles"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def reconstruct(profile_id: int, profile_name: str = "",
                force: bool = False, max_age_hours: float = 2.0) -> bool:
    """Build cycle_data_<id>.json from recent ai_predictions. Returns True if written.

    Skips reconstruction when a fresh live cycle file already exists
    (younger than `max_age_hours`) — the live data is always richer than
    the reconstruction.
    """
    out_path = f"cycle_data_{profile_id}.json"
    if not force and os.path.exists(out_path):
        age_seconds = time.time() - os.path.getmtime(out_path)
        if age_seconds < max_age_hours * 3600:
            print(f"[skip] {out_path} is fresh ({age_seconds/60:.0f}m old) — use --force to overwrite")
            return False

    db = _profile_db(profile_id)
    if not os.path.exists(db):
        print(f"[skip] no DB for profile {profile_id}: {db}")
        return False

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        # Most-recent prediction's timestamp serves as the cycle timestamp
        latest = conn.execute(
            "SELECT timestamp FROM ai_predictions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not latest:
            print(f"[skip] profile {profile_id} has no predictions")
            return False
        try:
            ts_struct = time.strptime(latest["timestamp"][:19], "%Y-%m-%dT%H:%M:%S")
            timestamp = time.mktime(ts_struct)
        except Exception:
            try:
                ts_struct = time.strptime(latest["timestamp"][:19], "%Y-%m-%d %H:%M:%S")
                timestamp = time.mktime(ts_struct)
            except Exception:
                timestamp = time.time()

        # Last cycle's predictions: take the most-recent batch (within ~1 hour
        # of the latest, which approximates one cycle's worth)
        rows = conn.execute(
            """SELECT symbol, predicted_signal, confidence, reasoning,
                      price_at_prediction, strategy_type, status,
                      actual_return_pct
               FROM ai_predictions
               WHERE timestamp >= datetime((SELECT MAX(timestamp) FROM ai_predictions),
                                            '-1 hour')
               ORDER BY id DESC
               LIMIT 30""",
        ).fetchall()
        if not rows:
            print(f"[skip] profile {profile_id} has no recent predictions")
            return False

        trades_selected = []
        shortlist = []
        for r in rows:
            sig = r["predicted_signal"] or "HOLD"
            if sig in ("BUY", "STRONG_BUY", "SHORT", "SELL", "STRONG_SELL"):
                trades_selected.append({
                    "symbol": r["symbol"],
                    "action": sig,
                    "size_pct": 5.0,   # unknown — placeholder
                    "confidence": int(r["confidence"] or 0),
                    "reasoning": (r["reasoning"] or "")[:300],
                })
            shortlist.append({
                "symbol": r["symbol"],
                "signal": sig,
                "score": 1,
                "rsi": None, "adx": None, "mfi": None,
                "volume_ratio": None,
                "pct_from_52w_high": None,
                "squeeze": False,
                "track_record": "",
                "news": [],
                "insider": "neutral",
                "short_pct": 0,
                "options_signal": "neutral",
                "reddit_mentions": 0,
                "options_oracle_summary": None,
                "sec_alert_severity": None,
            })
    finally:
        conn.close()

    summary_line = (
        f"[Reconstructed from prediction history at "
        f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime())}] "
        f"Latest cycle had {len(rows)} predictions, "
        f"{len(trades_selected)} non-HOLD signals."
    )

    cycle_data = {
        "profile_id": profile_id,
        "profile_name": profile_name,
        "timestamp": timestamp,
        "ai_reasoning": summary_line,
        "trades_selected": trades_selected[:5],
        "shortlist": shortlist[:15],
        "regime": "unknown",
        "vix": 0,
        "sector_rotation": {},
        "learned_patterns": [],
        "meta_model": {"loaded": False, "suppressed": 0, "adjusted": 0},
        "ensemble": {"enabled": False},
        "_reconstructed": True,
    }

    with open(out_path, "w") as f:
        json.dump(cycle_data, f)
    print(f"[ok]   wrote {out_path} ({len(rows)} predictions, "
          f"{len(trades_selected)} trades)")
    return True


def main() -> None:
    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if not a.startswith("--")]

    if args:
        ids = [int(a) for a in args]
        profiles = [{"id": i, "name": "", "market_type": ""} for i in ids]
    else:
        profiles = _list_profiles()
        print(f"Found {len(profiles)} profiles")

    for p in profiles:
        reconstruct(p["id"], p.get("name", ""), force=force)


if __name__ == "__main__":
    main()
