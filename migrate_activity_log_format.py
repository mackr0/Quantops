"""One-shot rewrite of historical activity_log rows that contain
raw snake_case parameter names + raw decimal values.

Bug history:
- Through 2026-04-27 the self-tuner's "PAST ADJUSTMENT REVIEWS"
  block emitted strings like:
    "Reviewed past adjustment: max_position_pct 0.08->0.092
     (win rate 48%->52%: IMPROVED)"
- Fixed in self_tuning.py:1330 at commit fb55c07.
- New activity rows produced after the fix render correctly:
    "Reviewed past adjustment: Max Position Size 8.0% → 9.2%
     (win rate 48% → 52%: IMPROVED)"
- But existing rows in activity_log are stored as-is and don't
  benefit from the code change.

This script walks every activity_log row whose `detail` matches the
old format and rewrites it in place using the same `_label()` +
`format_param_value()` helpers the live code now uses. Idempotent —
re-running is a no-op because the rewritten format no longer matches
the regex.

Usage:  python migrate_activity_log_format.py [--db PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys


# Match either the old format ("max_position_pct 0.08->0.092") OR the
# arrow-only format inside a longer detail string. Capture: param,
# old, new.
#
# Anchored on the literal "Reviewed past adjustment: " prefix so we
# don't accidentally rewrite unrelated detail text that happens to
# look similar.
_PATTERN_PAST_ADJ = re.compile(
    r"(Reviewed past adjustment:\s+)"      # 1: prefix (kept)
    r"([a-z][a-z0-9_]*)"                   # 2: snake_case param
    r"\s+"
    r"([0-9]+\.?[0-9]*)"                   # 3: old value
    r"\s*-+>\s*"                           # the -> separator
    r"([0-9]+\.?[0-9]*)"                   # 4: new value
)

# Match the "REVERSED" message variant.
_PATTERN_REVERSED = re.compile(
    r"(REVERSED:\s+)"
    r"([a-z][a-z0-9_]*)"
    r"\s+"
    r"back\s+from\s+"
    r"([0-9]+\.?[0-9]*)"
    r"\s+to\s+"
    r"([0-9]+\.?[0-9]*)"
)

# Match "- Adjusting <param>: ..."
_PATTERN_ADJUSTING = re.compile(
    r"(-\s+Adjusting\s+)([a-z][a-z0-9_]*)(:\s)"
)


def _is_known_param(name: str) -> bool:
    """Confirm the snake_case match is actually a tunable param —
    avoid rewriting random underscore-bearing English text."""
    try:
        from param_bounds import PARAM_BOUNDS
        return name in PARAM_BOUNDS
    except Exception:
        return False


def _format_value(param: str, raw: str) -> str:
    """Render `raw` (a numeric string from the regex) through
    `display_names.format_param_value`."""
    try:
        from display_names import format_param_value
        return format_param_value(param, raw)
    except Exception:
        return raw


def _label(param: str) -> str:
    try:
        from display_names import display_name
        return display_name(param)
    except Exception:
        return param


def rewrite_detail(detail: str) -> str:
    """Apply all known rewrites to one activity_log.detail string.
    Returns the new string (possibly unchanged)."""
    out = detail

    def _rewrite_past_adj(m):
        prefix, param, old, new = m.group(1), m.group(2), m.group(3), m.group(4)
        if not _is_known_param(param):
            return m.group(0)
        return (f"{prefix}{_label(param)} "
                f"{_format_value(param, old)} → {_format_value(param, new)}")

    def _rewrite_reversed(m):
        prefix, param, new, old = m.group(1), m.group(2), m.group(3), m.group(4)
        if not _is_known_param(param):
            return m.group(0)
        return (f"{prefix}{_label(param)} back from "
                f"{_format_value(param, new)} to {_format_value(param, old)}")

    def _rewrite_adjusting(m):
        prefix, param, suffix = m.group(1), m.group(2), m.group(3)
        if not _is_known_param(param):
            return m.group(0)
        return f"{prefix}{_label(param)}{suffix}"

    out = _PATTERN_PAST_ADJ.sub(_rewrite_past_adj, out)
    out = _PATTERN_REVERSED.sub(_rewrite_reversed, out)
    out = _PATTERN_ADJUSTING.sub(_rewrite_adjusting, out)
    # Cosmetic: also normalize "win rate 48%->52%" → "win rate 48% → 52%"
    out = re.sub(r"(win rate \d+%)\s*-+>\s*(\d+%)", r"\1 → \2", out)
    return out


def migrate(db_path: str, dry_run: bool = False) -> dict:
    """Walk activity_log, rewrite matching rows. Returns counts."""
    stats = {"scanned": 0, "rewritten": 0, "unchanged": 0}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Filter at SQL level to candidates that mention any
        # param-like substring we care about, to avoid scanning the
        # whole table.
        rows = conn.execute(
            "SELECT id, detail FROM activity_log "
            "WHERE detail LIKE '%Reviewed past adjustment:%' "
            "   OR detail LIKE '%REVERSED:%' "
            "   OR detail LIKE '%Adjusting %'"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"ERROR: cannot read activity_log: {exc}", file=sys.stderr)
        conn.close()
        return stats

    for row in rows:
        stats["scanned"] += 1
        new_detail = rewrite_detail(row["detail"])
        if new_detail == row["detail"]:
            stats["unchanged"] += 1
            continue
        stats["rewritten"] += 1
        if not dry_run:
            conn.execute(
                "UPDATE activity_log SET detail = ? WHERE id = ?",
                (new_detail, row["id"]),
            )
    if not dry_run:
        conn.commit()
    conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="quantopsai.db",
                    help="Path to the master DB holding activity_log")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing")
    args = ap.parse_args()

    stats = migrate(args.db, dry_run=args.dry_run)
    print(f"Activity log migration ({'DRY RUN' if args.dry_run else 'COMMITTED'}):")
    print(f"  scanned:   {stats['scanned']}")
    print(f"  rewritten: {stats['rewritten']}")
    print(f"  unchanged: {stats['unchanged']}")


if __name__ == "__main__":
    main()
