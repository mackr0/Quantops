"""Comparative-returns time series for the experiment dashboard.

Reads `daily_snapshots.equity` from every active profile's DB, normalizes
to "% return since first snapshot," and tags each series by its
`strategy_type` so the chart can render the buy_hold and random profiles
as visually distinct baselines.

The chart at /api/comparative-returns is consumed by Chart.js on the
dashboard (templates/dashboard.html). See docs/15.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _resolve_db(profile_id: int) -> Optional[str]:
    """Same prod/local-dev fallback used elsewhere."""
    for candidate in (
        f"/opt/quantopsai/quantopsai_profile_{profile_id}.db",
        f"quantopsai_profile_{profile_id}.db",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _read_equity_series(db_path: str) -> List[Tuple[str, float]]:
    """Return [(date_iso, equity), ...] sorted ascending. Empty list
    if the table is missing or has no rows."""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT date, equity FROM daily_snapshots "
                "WHERE equity IS NOT NULL "
                "ORDER BY date ASC"
            ).fetchall()
    except sqlite3.OperationalError as exc:
        # Table missing on a fresh DB is fine; anything else surfaces.
        if "no such table" in str(exc).lower():
            return []
        logger.warning(
            "comparative_returns: read failed for %s: %s", db_path, exc,
        )
        return []
    return [(r[0], float(r[1])) for r in rows]


def _normalize_to_returns(series: List[Tuple[str, float]],
                          ) -> List[Tuple[str, float]]:
    """Convert equity → cumulative % return relative to first snapshot.
    Empty / single-point series return as-is (zero return)."""
    if not series:
        return []
    base = series[0][1]
    if base <= 0:
        return [(d, 0.0) for d, _ in series]
    return [(d, ((e / base) - 1.0) * 100.0) for d, e in series]


def build_payload(user_id: int) -> Dict[str, Any]:
    """Build the JSON payload for /api/comparative-returns.

    Shape:
        {
          "series": [
            {
              "profile_id": int,
              "profile_name": str,
              "strategy_type": "ai" | "buy_hold" | "random",
              "initial_capital": float,
              "points": [{"date": "YYYY-MM-DD", "return_pct": float}, ...]
            }, ...
          ],
          "empty_state": bool,    # true if no series have any points
          "empty_message": str,   # explanation if empty_state is True
        }
    """
    from models import get_user_profiles
    profiles = [p for p in get_user_profiles(user_id) if p.get("enabled")]

    series: List[Dict[str, Any]] = []
    any_points = False
    for p in profiles:
        db = _resolve_db(p["id"])
        if not db:
            continue
        raw = _read_equity_series(db)
        normalized = _normalize_to_returns(raw)
        if normalized:
            any_points = True
        series.append({
            "profile_id": p["id"],
            "profile_name": p["name"],
            "strategy_type": p.get("strategy_type", "ai") or "ai",
            "initial_capital": float(p.get("initial_capital", 0.0)),
            "points": [{"date": d, "return_pct": round(r, 4)}
                       for d, r in normalized],
        })

    payload: Dict[str, Any] = {"series": series, "empty_state": False,
                               "empty_message": ""}
    if not any_points:
        payload["empty_state"] = True
        payload["empty_message"] = (
            "No equity snapshots yet. The first row is captured at the "
            "end of the next trading session's snapshot task — once that "
            "runs, profile returns will appear here and overlay against "
            "the Buy-Hold SPY and Random Stock-of-Day baselines."
        )
    return payload
