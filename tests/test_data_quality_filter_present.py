"""Class-invariant guardrail (2026-05-12): every analytics query
on the trades table that reads `pnl` or `price` must filter
data_quality-tagged rows.

The phantom_stop_2026_05_11 incident left 31 rows in the trades
table where the `price` field has the option premium ($0.16-$1.48)
but the trade actually executed against a stock ticker. Including
those rows in:
- per-trade % return calcs → CVaR -8409%, VaR garbage
- price-band analytics → self-tuning RAISES min_price every cycle
- slippage_pct aggregates → +1131% display
- cluster detection → false losing-week clusters
- short-side win-rate → falsely depresses short-side stats

This test scans production source files for `FROM trades` queries
that include `pnl IS NOT NULL` (analytics queries) and verifies
each one either:
  a) Filters `data_quality IS NULL` directly in the SQL, OR
  b) Calls `data_quality_clause(conn)` to inject the filter, OR
  c) Appears in DATA_QUALITY_FILTER_ALLOWLIST with rationale
     (e.g., one-shot migrations that intentionally include all
     rows for the migration's scope).
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

REPO_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)


# Allowlist: file:line marker → rationale.
DATA_QUALITY_FILTER_ALLOWLIST = {
    # journal.py one-shot migrations and helper itself —
    # intentionally operate on all rows.
    "data_quality_clause":
        "Helper function — defines the filter, doesn't consume it",
    "data_quality = 'phantom_stop_2026_05_11'":
        "One-shot backfill targeting these rows specifically",
    # Trade-level lookups that aren't aggregates (return one
    # specific row by id / order_id / status='open' etc.) don't
    # need the filter. The pattern below catches only the
    # `pnl IS NOT NULL` analytics queries.
}


SCANNED_FILES = [
    "metrics/legacy.py",
    "views.py",
    "self_tuning.py",
    "journal.py",
    "post_mortem.py",
    "kelly_sizing.py",
]


def _read(relpath: str) -> str:
    full = os.path.join(REPO_ROOT, relpath)
    if not os.path.exists(full):
        return ""
    with open(full) as fh:
        return fh.read()


class TestEveryAnalyticsTradeQueryFiltersDataQuality:
    """Pin: every `FROM trades WHERE pnl IS NOT NULL` query in
    production source either passes the data_quality filter or
    is allowlisted. Catches the next analytics query that ships
    without the filter."""

    QUERY_PATTERN = re.compile(
        r"FROM\s+trades\s+WHERE\s+pnl\s+IS\s+NOT\s+NULL",
        re.IGNORECASE,
    )

    def _surrounding_chunk(self, source: str, match_start: int) -> str:
        """Return ~600 chars around the match — enough to capture
        a `data_quality_clause(...)` call or a `data_quality`
        literal a few lines earlier or later."""
        return source[
            max(0, match_start - 600):
            min(len(source), match_start + 600)
        ]

    def test_every_analytics_query_includes_filter(self):
        offenses = []
        for relpath in SCANNED_FILES:
            src = _read(relpath)
            for m in self.QUERY_PATTERN.finditer(src):
                line_no = src[:m.start()].count("\n") + 1
                chunk = self._surrounding_chunk(src, m.start())
                # Acceptance criteria — filter is present in chunk.
                # Recognized patterns:
                #   - data_quality_clause(...)  helper call
                #   - data_quality IS NULL       direct SQL
                #   - {_dq}                       f-string interpolation
                #   - {dq_clause}                 f-string interpolation
                if "data_quality_clause" in chunk:
                    continue
                if "data_quality IS NULL" in chunk:
                    continue
                if "{_dq}" in chunk or "{dq_clause}" in chunk:
                    continue
                # Allowlist match
                if any(allow in chunk
                        for allow in DATA_QUALITY_FILTER_ALLOWLIST):
                    continue
                offenses.append(
                    f"{relpath}:{line_no}  FROM trades WHERE "
                    f"pnl IS NOT NULL — missing data_quality filter"
                )
        if offenses:
            pytest.fail(
                "Analytics queries without data_quality filter — "
                "phantom-stop incident rows would pollute the "
                "computed metrics.\n\nFix one of:\n"
                "  1. Add `data_quality IS NULL` to the WHERE clause.\n"
                "  2. Use `data_quality_clause(conn)` helper from "
                "journal.py.\n"
                "  3. Add an entry to DATA_QUALITY_FILTER_ALLOWLIST "
                "with a rationale.\n\n"
                + "\n".join(f"  {o}" for o in offenses)
            )


class TestAllowlistHygiene:
    def test_allowlist_entries_have_rationale(self):
        for marker, rationale in DATA_QUALITY_FILTER_ALLOWLIST.items():
            assert rationale, (
                f"Empty rationale for allowlist entry: {marker}"
            )
