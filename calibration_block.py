"""Per-profile track record for the AI prompt — in-context learning.

docs/25 step 4.2 (2026-08-23). A frozen-weight model learns one way: by
being shown what it got right and wrong. Every batch decision now
carries THIS profile's own resolved record — win rate and mean move by
confidence band, by call type, by strategy, by regime — with the
sample size on every number so a 5-sample bucket can't read like a
500-sample one. Absent (not "0%") when there isn't enough history.

Every shadow arm receives the same prompt, so the block is identical
across arms; it changes only as the profile's own outcomes accrue.

Definitions (the same ones the /ai page uses; see
calculation_verification/ai.md):
  * resolved = status='resolved' AND actual_outcome IN ('win','loss'),
    data_quality-tagged rows excluded;
  * win rate = wins / (wins + losses) — scratch outcomes are not in
    either count; mean move = mean(actual_return_pct) over the bucket.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MIN_TOTAL_FOR_BLOCK = 20     # below this, say so instead of showing numbers
MIN_BUCKET_N = 10            # buckets smaller than this are listed as (n<10)
RECENT_DAYS = 30
_BANDS: List[Tuple[str, int, int]] = [
    ("0-25", 0, 25), ("25-50", 25, 50), ("50-75", 50, 75), ("75-100", 75, 101),
]
_SIGNAL_LABELS = {
    "BUY": "BUY", "STRONG_BUY": "BUY", "SELL": "SELL", "STRONG_SELL": "SELL",
    "SHORT": "SHORT", "HOLD": "HOLD",
}

_BASE_WHERE = (
    "status='resolved' AND actual_outcome IN ('win','loss') "
    "AND (data_quality IS NULL OR data_quality='')"
)
# The headline, the confidence bands, strategy and regime lines are
# DIRECTIONAL only — the /ai page's definition. HOLD rows are stored
# at confidence 0, so without this filter they flood the 0–25 band and
# the all-time line (live check 2026-08-23: 2,579 HOLDs in a "3,700
# resolved" headline). HOLD gets its own line under "By call".
_DIRECTIONAL_WHERE = (
    "UPPER(predicted_signal) IN ('BUY','STRONG_BUY','SELL','STRONG_SELL','SHORT')"
)


def _model_scope(conn: sqlite3.Connection, ai_model: Optional[str]
                 ) -> Tuple[str, Tuple]:
    """SQL fragment scoping rows to the profile's CURRENT model
    (docs/25 5.4). Without attribution (no column, or no model given)
    the scope is everything — the pre-08-23 behaviour."""
    if not ai_model:
        return "1=1", ()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_predictions)")}
    if "ai_model" not in cols:
        return "1=1", ()
    return "ai_model = ?", (ai_model,)


def _bucket(conn: sqlite3.Connection, where: str, params: Tuple = (),
            scope: Tuple[str, Tuple] = ("1=1", ())
            ) -> Tuple[int, int, Optional[float]]:
    """(n, wins, mean_return) for DIRECTIONAL rows matching `where`
    inside the model `scope`."""
    row = conn.execute(
        f"SELECT COUNT(*), SUM(actual_outcome='win'), AVG(actual_return_pct) "
        f"FROM ai_predictions WHERE {_BASE_WHERE} AND {_DIRECTIONAL_WHERE} "
        f"AND ({scope[0]}) AND ({where})", tuple(scope[1]) + tuple(params),
    ).fetchone()
    n = int(row[0] or 0)
    wins = int(row[1] or 0)
    mean = float(row[2]) if row[2] is not None and n else None
    return n, wins, mean


def compute_track_record(db_path: str,
                         ai_model: Optional[str] = None) -> Dict[str, Any]:
    """Raw numbers for the block (and for tests / the register).
    Returns {"total": n, "overall": (n, wins, mean), "recent": (...),
    "by_band": [...], "by_signal": [...], "by_strategy": [...],
    "by_regime": [...], "other_models": n} — each list item
    (label, n, wins, mean). With `ai_model`, every bucket is scoped to
    rows that model produced; `other_models` counts this profile's
    resolved directional rows made by OTHER (or unattributed) models —
    stated, never blended (docs/25 5.4)."""
    out: Dict[str, Any] = {"total": 0, "other_models": 0}
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        scope = _model_scope(conn, ai_model)
        n, w, m = _bucket(conn, "1=1", scope=scope)
        out["total"] = n
        out["overall"] = (n, w, m)
        if scope[0] != "1=1":
            out["other_models"] = int(conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions WHERE {_BASE_WHERE} "
                f"AND {_DIRECTIONAL_WHERE} AND NOT (ai_model = ?)"
                f" OR ({_BASE_WHERE} AND {_DIRECTIONAL_WHERE} AND ai_model IS NULL)",
                (ai_model,)).fetchone()[0] or 0)
        if n == 0:
            return out
        out["recent"] = _bucket(
            conn, "datetime(resolved_at) >= datetime('now', ?)",
            (f"-{RECENT_DAYS} days",), scope=scope)
        out["by_band"] = [
            (label,) + _bucket(conn, "confidence >= ? AND confidence < ?",
                               (lo, hi), scope=scope)
            for label, lo, hi in _BANDS
        ]
        sig: Dict[str, List[int]] = {}
        for raw, n_, w_, m_ in conn.execute(
                f"SELECT UPPER(predicted_signal), COUNT(*), "
                f"SUM(actual_outcome='win'), AVG(actual_return_pct) "
                f"FROM ai_predictions WHERE {_BASE_WHERE} AND ({scope[0]}) "
                f"GROUP BY UPPER(predicted_signal)", tuple(scope[1])):
            label = _SIGNAL_LABELS.get(raw or "", None)
            if label is None:
                continue
            acc = sig.setdefault(label, [0, 0, 0.0])
            acc[0] += int(n_ or 0)
            acc[1] += int(w_ or 0)
            acc[2] += float(m_ or 0.0) * int(n_ or 0)
        out["by_signal"] = [
            (label, v[0], v[1], (v[2] / v[0]) if v[0] else None)
            for label, v in sorted(sig.items(), key=lambda kv: -kv[1][0])
        ]
        out["by_strategy"] = [
            (str(s), int(n_ or 0), int(w_ or 0),
             float(m_) if m_ is not None else None)
            for s, n_, w_, m_ in conn.execute(
                f"SELECT COALESCE(strategy_type, '?'), COUNT(*), "
                f"SUM(actual_outcome='win'), AVG(actual_return_pct) "
                f"FROM ai_predictions WHERE {_BASE_WHERE} AND {_DIRECTIONAL_WHERE} "
                f"AND ({scope[0]}) "
                f"GROUP BY strategy_type ORDER BY COUNT(*) DESC LIMIT 6",
                tuple(scope[1]))
        ]
        out["by_regime"] = [
            (str(r), int(n_ or 0), int(w_ or 0),
             float(m_) if m_ is not None else None)
            for r, n_, w_, m_ in conn.execute(
                f"SELECT COALESCE(regime_at_prediction, '?'), COUNT(*), "
                f"SUM(actual_outcome='win'), AVG(actual_return_pct) "
                f"FROM ai_predictions WHERE {_BASE_WHERE} AND {_DIRECTIONAL_WHERE} "
                f"AND ({scope[0]}) "
                f"GROUP BY regime_at_prediction ORDER BY COUNT(*) DESC LIMIT 4",
                tuple(scope[1]))
        ]
    return out


def _fmt(label: str, n: int, wins: int, mean: Optional[float]) -> str:
    if n < MIN_BUCKET_N:
        return f"{label}: n<{MIN_BUCKET_N} (not enough to judge)"
    wr = wins / n * 100.0
    mv = f", mean move {mean:+.1f}%" if mean is not None else ""
    return f"{label}: {wr:.0f}% win rate on {n}{mv}"


def render_track_record(db_path: str, ai_model: Optional[str] = None) -> str:
    """The prompt block. Empty string only when the DB can't be read
    (logged); a thin record is stated as such, never hidden. With
    `ai_model`, the record is the CURRENT model's own; any history this
    profile has under other models is stated as a count, not blended."""
    try:
        rec = compute_track_record(db_path, ai_model=ai_model)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("calibration block: could not read %s: %s: %s",
                       db_path, type(exc).__name__, exc)
        return ""
    total = rec.get("total", 0)
    other = rec.get("other_models", 0)
    other_line = (
        f"\n    (This profile also has {other} resolved directional "
        "predictions made by a previous model — not yours, not counted.)"
        if other else "")
    if total < MIN_TOTAL_FOR_BLOCK:
        return (
            "\n  YOUR TRACK RECORD (this profile, this model): only "
            f"{total} resolved directional predictions so far — too few "
            "to calibrate on. Rate confidence honestly; every prediction "
            "is scored." + other_line
        )
    n, w, m = rec["overall"]
    rn, rw, rm = rec["recent"]
    lines = [
        "\n  YOUR TRACK RECORD (this profile's own resolved DIRECTIONAL "
        "predictions; wins/(wins+losses), n on every number — small n "
        "means little; HOLDs are scored separately under 'By call'):",
        "    " + _fmt("All-time", n, w, m),
        "    " + _fmt(f"Last {RECENT_DAYS} days", rn, rw, rm),
        "    By stated confidence: " + "; ".join(
            _fmt(f"{lab}", n_, w_, m_).replace(f"{lab}: ", f"{lab}→ ")
            for lab, n_, w_, m_ in rec["by_band"]),
        "    By call: " + "; ".join(
            _fmt(lab, n_, w_, m_) for lab, n_, w_, m_ in rec["by_signal"]),
    ]
    if rec.get("by_strategy"):
        lines.append("    By strategy: " + "; ".join(
            _fmt(lab, n_, w_, m_) for lab, n_, w_, m_ in rec["by_strategy"]))
    if rec.get("by_regime"):
        lines.append("    By regime: " + "; ".join(
            _fmt(lab, n_, w_, m_) for lab, n_, w_, m_ in rec["by_regime"]))
    lines.append(
        "    Use this: if a band or call type has underperformed, say so "
        "in your reasoning and rate accordingly — your stated confidence "
        "is scored against outcomes and feeds this profile's calibration.")
    if other_line:
        lines.append(other_line.strip("\n"))
    return "\n".join(lines)
