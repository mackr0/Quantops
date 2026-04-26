"""Losing-week post-mortems — automated pattern extraction from
underperforming periods.

When a profile has a materially worse week than its long-term baseline,
we shouldn't just notice it (the digest already does that). We should
*learn* from it. This module clusters the losing trades by feature
signature, identifies the dominant pattern, and stores it as a
`learned_pattern` that the AI prompt will inject next week. The
prompt-builder already pulls learned_patterns into every batch decision
context, so the pattern propagates automatically — no extra wiring.

This closes the post-mortem feedback loop: bad week → pattern detected
→ AI is told about it → AI factors it into next week's decisions.
The next post-mortem can verify whether the pattern's been learned
(WR on similar setups should improve) or persists (then we tighten
the relevant tuning rules).

Storage: patterns are persisted to a `learned_patterns` table on each
profile DB. The trade pipeline already loads them into batch context.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Bad-week threshold: WR must be below long-term baseline by this many
# percentage points before we trigger a post-mortem. Avoids noisy weeks
# triggering false-positive patterns.
_BAD_WEEK_DROP_THRESHOLD = 10.0

# Minimum number of losing predictions in the past week before we
# bother trying to find a pattern. Below this, "patterns" would be
# noise.
_MIN_LOSSES_TO_ANALYZE = 5

# A feature value is "discriminating" for the losses if it shows up in
# at least this fraction of them.
_DOMINANT_FEATURE_THRESHOLD = 0.6

# Stop-words / fields that are noisy or already covered by existing
# tuning layers — not useful as post-mortem patterns.
_SKIP_FEATURES = {
    "rsi", "stochrsi", "adx", "atr", "obv", "mfi", "cmf",  # raw indicators
    "price", "qty", "volume", "score", "confidence",        # meta
    "pe_trailing", "rel_strength_vs_sector",                # too varied
    "momentum_5d", "momentum_20d", "gap_pct",                # already tuned
    "volume_ratio",
}


def init_post_mortem_db(db_path: str) -> None:
    """Idempotent table creation for learned_patterns storage."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS learned_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                pattern_text TEXT NOT NULL,
                losing_trade_count INTEGER NOT NULL,
                dominant_features TEXT NOT NULL,
                period_wr REAL NOT NULL,
                baseline_wr REAL NOT NULL,
                still_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _categorical_value(v: Any) -> Optional[str]:
    """Return a stable string label for a feature value if it's
    something worth clustering on (categorical or sentiment-bucketed
    numeric). Returns None for noise."""
    if v is None or v == "" or v == "neutral" or v == "flat":
        return None
    if isinstance(v, str):
        return v.lower()
    if isinstance(v, bool):
        return "true" if v else None
    if isinstance(v, (int, float)):
        # Bucket numerics so close values cluster together
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if abs(f) < 0.01:
            return None
        if f > 0.5:
            return "high"
        if f > 0:
            return "moderate"
        return "low"
    return None


def _detect_dominant_features(
    losing_features: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Find features that appear with the same value in at least
    `_DOMINANT_FEATURE_THRESHOLD` of the losing trades. These are the
    common-thread patterns to call out."""
    if not losing_features:
        return []

    # Tally (feature, value) pairs across the losing predictions
    pair_counts: Counter = Counter()
    feature_seen: Counter = Counter()
    for feats in losing_features:
        if not isinstance(feats, dict):
            continue
        for k, v in feats.items():
            if k in _SKIP_FEATURES or k.startswith("vote_"):
                continue
            label = _categorical_value(v)
            if label is None:
                continue
            pair_counts[(k, label)] += 1
            feature_seen[k] += 1

    n = len(losing_features)
    threshold = max(2, int(n * _DOMINANT_FEATURE_THRESHOLD))
    dominant = []
    for (feat, val), count in pair_counts.most_common():
        if count < threshold:
            continue
        dominant.append({
            "feature": feat,
            "value": val,
            "count": count,
            "frac": round(count / n, 2),
        })
        if len(dominant) >= 4:  # Cap to top 4 to keep patterns concise
            break
    return dominant


def _format_pattern_text(
    period_wr: float,
    baseline_wr: float,
    losing_count: int,
    dominant: List[Dict[str, Any]],
) -> str:
    """Render a one-line pattern that reads naturally in the AI
    prompt."""
    if not dominant:
        return (
            f"This week's WR was {period_wr:.0f}% (vs {baseline_wr:.0f}% "
            f"baseline) on {losing_count} losing predictions. No clear "
            f"common thread — be extra selective on confidence."
        )
    parts = []
    from display_names import display_name
    for d in dominant:
        # Render the feature/value naturally
        parts.append(f"{display_name(d['feature'])}={d['value']} "
                     f"({int(d['frac']*100)}%)")
    threads = " AND ".join(parts)
    return (
        f"Recent losing-week pattern: {losing_count} losses had a common "
        f"thread of {threads}. WR on these setups was {period_wr:.0f}% "
        f"vs {baseline_wr:.0f}% baseline. Be extra cautious when these "
        f"signals stack."
    )


def analyze_recent_week(db_path: str) -> Optional[Dict[str, Any]]:
    """Run the post-mortem on the last 7 days of resolved predictions
    for one profile. Returns a pattern dict if a learning was extracted,
    None if the week was healthy or there wasn't enough data.

    The pattern is also persisted to the `learned_patterns` table so
    the prompt builder can inject it into future cycles.
    """
    init_post_mortem_db(db_path)
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        logger.debug("post-mortem db open failed: %s", exc)
        return None

    try:
        # Long-term baseline WR (all resolved predictions)
        baseline_row = conn.execute(
            "SELECT COUNT(*) as total, "
            " SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
            "FROM ai_predictions WHERE status='resolved' "
            "  AND actual_outcome IN ('win', 'loss')"
        ).fetchone()
        if not baseline_row or baseline_row["total"] < 30:
            return None  # Not enough history to compare against
        baseline_wr = baseline_row["wins"] / baseline_row["total"] * 100.0

        # Last 7 days
        period_row = conn.execute(
            "SELECT COUNT(*) as total, "
            " SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
            "FROM ai_predictions WHERE status='resolved' "
            "  AND actual_outcome IN ('win', 'loss') "
            "  AND datetime(resolved_at) >= datetime('now', '-7 days')"
        ).fetchone()
        if not period_row or period_row["total"] < 5:
            return None
        period_total = period_row["total"]
        period_wr = period_row["wins"] / period_total * 100.0

        # Trigger only when this week was materially worse than baseline
        if (baseline_wr - period_wr) < _BAD_WEEK_DROP_THRESHOLD:
            return None

        # Pull the losing predictions' features
        loss_rows = conn.execute(
            "SELECT features_json FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='loss' "
            "  AND datetime(resolved_at) >= datetime('now', '-7 days') "
            "  AND features_json IS NOT NULL"
        ).fetchall()
        losing_features = []
        for r in loss_rows:
            try:
                losing_features.append(json.loads(r["features_json"]))
            except Exception:
                continue

        losing_count = len(losing_features)
        if losing_count < _MIN_LOSSES_TO_ANALYZE:
            return None

        dominant = _detect_dominant_features(losing_features)
        pattern_text = _format_pattern_text(
            period_wr, baseline_wr, losing_count, dominant)

        # Mark prior patterns inactive — only the most recent post-mortem
        # is "active" so the AI prompt isn't drowned in stale lessons.
        conn.execute(
            "UPDATE learned_patterns SET still_active = 0 "
            "WHERE still_active = 1"
        )
        conn.execute(
            "INSERT INTO learned_patterns "
            "(period_start, period_end, pattern_text, losing_trade_count, "
            " dominant_features, period_wr, baseline_wr) "
            "VALUES ("
            " datetime('now', '-7 days'), datetime('now'), "
            " ?, ?, ?, ?, ?)",
            (pattern_text, losing_count, json.dumps(dominant),
             period_wr, baseline_wr),
        )
        conn.commit()

        return {
            "pattern_text": pattern_text,
            "losing_trade_count": losing_count,
            "dominant_features": dominant,
            "period_wr": period_wr,
            "baseline_wr": baseline_wr,
        }
    except Exception as exc:
        logger.warning("post-mortem failed for %s: %s", db_path, exc)
        return None
    finally:
        conn.close()


def get_active_patterns(db_path: str) -> List[str]:
    """Return the active learned-pattern texts for this profile. The
    trade pipeline calls this before building the batch prompt so the
    AI sees the most recent post-mortem learnings."""
    try:
        init_post_mortem_db(db_path)
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT pattern_text FROM learned_patterns "
            "WHERE still_active = 1 ORDER BY created_at DESC LIMIT 3"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []
