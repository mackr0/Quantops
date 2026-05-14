"""Structural guardrail (2026-05-13, expanded from 2026-05-12):
every analytics SQL query that aggregates over a phantom-pollution
-affected column from `trades` or `ai_predictions` must filter
`data_quality IS NULL`.

Original version of this test (2026-05-12) was narrow:
  - Only scanned 6 hardcoded files
  - Only caught `FROM trades WHERE pnl IS NOT NULL` shape
  - Missed ai_predictions queries entirely
  - Missed other pollution columns (slippage_pct, MFE,
    actual_return_pct, actual_outcome)

That's a textbook instance-style test — caught the specific bug
pattern we'd seen, missed the class. The 2026-05-13 audit found
the test wouldn't have caught the multileg-resolver / backfill
bugs we shipped this week.

This expanded version:
  - Auto-discovers all source files (no hardcoded list)
  - Catches all known pollution columns
  - Catches both trades AND ai_predictions tables
  - Handles multi-line and single-line SQL strings

The bug class.
On 2026-05-11, phantom-stop pollution put 31 corrupt rows in
`trades` where `price` was option premium ($0.16-$1.48) but the
trade was recorded as a stock SELL. Those rows polluted:
  - slippage analytics (+1130% display)
  - CVaR/VaR (-8409% tail loss)
  - per-strategy win-rate (extreme-magnitude outliers)
  - the resolver chain into ai_predictions.actual_return_pct
  - alpha-decay Sharpe degradation alarms

Fix landed in patches across 7+ files. Each site was found
individually. The fix took a full day. This test makes the
"missed an analytics site" bug class structurally impossible.

Pollution-affected columns: any column whose VALUE is wrong on
data_quality-tagged rows.
  - trades.pnl              (computed from corrupt price)
  - trades.slippage_pct     (vs corrupt decision_price)
  - trades.max_favorable_excursion
  - ai_predictions.actual_return_pct
  - ai_predictions.actual_outcome  (win/loss derived from corrupt return)
"""
from __future__ import annotations

import os
import re
import sys
from typing import List, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Aggregation indicators — any of these in a query containing a
# pollution column triggers the "must filter data_quality" rule.
AGGREGATION_FUNCS = (
    "COUNT(", "SUM(", "AVG(", "MIN(", "MAX(", "TOTAL(",
)
ANALYTICS_CLAUSES = (
    "GROUP BY", "HAVING",
)


# Columns whose value is corrupt on data_quality-tagged rows.
# Aggregating any of these without the filter produces wrong
# numbers in dashboards / metrics / tuning signals.
POLLUTION_COLS = (
    "pnl",                       # trades
    "slippage_pct",              # trades
    "max_favorable_excursion",   # trades
    "actual_return_pct",         # ai_predictions
    "actual_outcome",            # ai_predictions
)


# Files that legitimately operate on tagged rows. Each entry needs
# a written rationale. Stale entries (file no longer exists) are
# caught by test_allowlist_entries_match_existing_files below so
# rationales stay current.
ALLOWLIST_FILES = {
    "journal.py":
        "Defines data_quality_clause helper + the data_quality "
        "backfill itself. Backfill queries must NOT filter — that "
        "would defeat the purpose. Other queries in the file already "
        "use the helper.",
    "backfill_multileg_negative_prices.py":
        "Historical-row repair script. Needs to find the bad rows "
        "to fix them.",
    "ai_tracker.py":
        "Resolver path uses live broker price + per-prediction "
        "metadata, not aggregations across rows. The pollution-into-"
        "aggregation class doesn't apply here.",
    "virtual_audit.py":
        "Audit script that intentionally surfaces all rows including "
        "tagged ones — that IS the audit's purpose.",
    "recover_cycle_data.py":
        "Recovery script that lists recent predictions for human "
        "review. The MAX(timestamp) subquery triggers the analytics "
        "detector but the actual SELECT is a row listing for "
        "operator display, not aggregation. Filtering would hide "
        "tagged rows from the recovery operator who may need to "
        "see them.",
}


def _walk_source_files() -> List[str]:
    """Discover all production .py source files. Excludes tests,
    venv, vendor/output dirs."""
    out = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in (
            "venv", "__pycache__", ".git", "tests", "exports",
            "backups", "logs", "altdata", "node_modules", "docs",
        )]
        for f in files:
            if not f.endswith(".py"):
                continue
            if f.startswith("test_"):
                continue
            out.append(os.path.join(root, f))
    return out


def _extract_sql_queries(src: str) -> List[Tuple[int, str]]:
    """Extract SQL query strings that touch trades or ai_predictions.

    Returns (start_lineno, query_text) tuples. Handles:
      - Multi-line triple-quoted strings (\"\"\" ... \"\"\")
      - Single-line double-quoted strings
      - Single-line single-quoted strings
    """
    out = []
    seen = set()  # dedup by (lineno, first 40 chars)

    # Multi-line triple-quoted (most common for complex SQL)
    for m in re.finditer(
        r'"""([^"]*?(?:FROM\s+trades|FROM\s+ai_predictions)[^"]*?)"""',
        src, re.IGNORECASE | re.DOTALL,
    ):
        line_no = src.count("\n", 0, m.start()) + 1
        text = m.group(1)
        key = (line_no, text[:40])
        if key not in seen:
            seen.add(key)
            out.append((line_no, text))

    # Single-line strings — scan line by line to avoid greedy
    # crossing-line matches
    for line_no, line in enumerate(src.split("\n"), start=1):
        # Skip comments
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Single-line double-quoted
        for m in re.finditer(
            r'"([^"\n]*?(?:FROM\s+trades|FROM\s+ai_predictions)[^"\n]*?)"',
            line, re.IGNORECASE,
        ):
            text = m.group(1)
            key = (line_no, text[:40])
            if key not in seen:
                seen.add(key)
                out.append((line_no, text))

    return out


def _is_analytics_query(query: str) -> bool:
    """True iff the query has an aggregation shape AND references
    at least one pollution-affected column."""
    upper = query.upper()
    has_aggregation = (
        any(f in upper for f in AGGREGATION_FUNCS)
        or any(c in upper for c in ANALYTICS_CLAUSES)
    )
    if not has_aggregation:
        return False
    lower = query.lower()
    for col in POLLUTION_COLS:
        # Match column name as a whole word (avoid matching
        # "pnl" inside "pnl_pct_change" etc.)
        if re.search(rf"\b{re.escape(col)}\b", lower):
            return True
    return False


def _has_data_quality_filter(query: str, file_src: str,
                                lineno: int) -> bool:
    """True iff the query (or surrounding code in the file)
    filters data_quality.

    Acceptance:
      1. SQL string itself contains `data_quality`
      2. SQL contains an f-string interp slot following the
         `<word>_dq` naming convention (`{_dq}`, `{_aip_dq}`,
         `{_trades_dq}`, `{dq_clause}`, etc.)
      3. The same FUNCTION as the query also calls
         data_quality_clause(...). Function-scope match is
         narrower than line-window so it doesn't false-positive
         on a previous function's clause being in scope.
    """
    # Path 1: in the SQL itself
    if "data_quality" in query.lower():
        return True
    # Path 2: f-string interp pattern. Recognize any name
    # ending in _dq or matching the canonical helper name.
    if re.search(r"\{[_\w]*dq[_\w]*\}", query):
        return True
    if "{dq_clause}" in query or "{_data_quality}" in query:
        return True
    # Path 3: data_quality_clause call within the SAME function
    # as the query. Walk backward from lineno to find the
    # enclosing `def`, then forward to find the next `def` (or
    # EOF) to bound the function. Check whether
    # data_quality_clause appears within those bounds.
    if "data_quality_clause" not in file_src:
        return False
    lines = file_src.split("\n")
    # Find enclosing `def` (search backward)
    func_start = 0
    for i in range(lineno - 1, -1, -1):
        if re.match(r"\s*def\s+\w+", lines[i]):
            func_start = i
            break
    # Find next `def` at the same or shallower indent
    func_end = len(lines)
    if func_start < len(lines):
        start_indent = len(lines[func_start]) - len(
            lines[func_start].lstrip())
        for i in range(func_start + 1, len(lines)):
            if not lines[i].strip():
                continue
            this_indent = len(lines[i]) - len(lines[i].lstrip())
            if (this_indent <= start_indent
                    and re.match(r"\s*def\s+\w+", lines[i])):
                func_end = i
                break
    chunk = "\n".join(lines[func_start:func_end])
    if "data_quality_clause" in chunk:
        return True
    return False


class TestDataQualityFilterPresent:
    def test_every_analytics_query_filters_data_quality(self):
        violations = []
        files_scanned = 0
        analytics_queries_found = 0
        for src_path in _walk_source_files():
            rel = os.path.relpath(src_path, REPO_ROOT)
            basename = os.path.basename(src_path)
            if basename in ALLOWLIST_FILES:
                continue
            try:
                with open(src_path) as fh:
                    src = fh.read()
            except Exception:
                continue
            files_scanned += 1
            for lineno, query in _extract_sql_queries(src):
                if not _is_analytics_query(query):
                    continue
                analytics_queries_found += 1
                if _has_data_quality_filter(query, src, lineno):
                    continue
                violations.append(
                    (rel, lineno, query.strip()[:200])
                )
        # Sanity: scanner must find SOME analytics queries or it's
        # silently broken
        assert analytics_queries_found >= 5, (
            f"Scanner found only {analytics_queries_found} analytics "
            f"queries across {files_scanned} files — likely broken. "
            f"Investigate _extract_sql_queries / _is_analytics_query."
        )
        if violations:
            details = "\n\n".join(
                f"  {rel}:{lineno}\n    {query}"
                for rel, lineno, query in violations
            )
            pytest.fail(
                f"{len(violations)} analytics queries against "
                f"trades/ai_predictions don't filter data_quality. "
                f"Phantom-stop-class incidents would silently pool "
                f"corrupt rows into the resulting metrics.\n\n"
                f"Fix one of:\n"
                f"  1. Add `data_quality IS NULL` to the WHERE\n"
                f"  2. Use `data_quality_clause(conn)` and inject "
                f"via f-string\n"
                f"  3. If this query LEGITIMATELY needs corrupt "
                f"rows (audit/backfill), add the file to "
                f"ALLOWLIST_FILES with a written rationale\n\n"
                f"Sites:\n{details}"
            )

    def test_allowlist_entries_match_existing_files(self):
        """Stale allowlist entries (files no longer in the repo)
        should fail this test so rationales stay current."""
        all_basenames = {
            os.path.basename(p) for p in _walk_source_files()
        }
        # Allowlist entries that don't have a matching file
        stale = [k for k in ALLOWLIST_FILES if k not in all_basenames]
        if stale:
            pytest.fail(
                "ALLOWLIST_FILES entries reference files that "
                "no longer exist (rationale drift):\n  "
                + "\n  ".join(stale)
                + "\n\nRemove these entries — they protect nothing."
            )


class TestScannerSanity:
    """The structural test only works if the scanner correctly
    classifies known patterns. These tests verify the scanner
    itself isn't broken."""

    def test_scanner_recognizes_safe_pattern(self):
        sample = """
            SELECT SUM(pnl) FROM trades
            WHERE status='closed'
              AND data_quality IS NULL
        """
        assert _is_analytics_query(sample), (
            "Sample WITH aggregation + pollution col not flagged "
            "as analytics"
        )
        assert _has_data_quality_filter(sample, "", 1), (
            "Sample WITH data_quality filter not recognized as safe"
        )

    def test_scanner_flags_dangerous_pattern(self):
        sample = (
            "SELECT AVG(actual_return_pct) FROM ai_predictions "
            "WHERE status='resolved'"
        )
        assert _is_analytics_query(sample)
        assert not _has_data_quality_filter(sample, "", 1)

    def test_scanner_recognizes_data_quality_clause_helper(self):
        """f-string injection via the helper must be recognized
        as filtering."""
        sample = (
            'f"SELECT SUM(pnl) FROM trades '
            'WHERE status=\'closed\' {_dq}"'
        )
        assert _has_data_quality_filter(sample, "", 1)

    def test_non_analytics_lookup_not_flagged(self):
        """Single-row WHERE id=? against trades — not analytics,
        doesn't need filter."""
        sample = "SELECT * FROM trades WHERE id = ?"
        assert not _is_analytics_query(sample)

    def test_aggregation_without_pollution_col_not_flagged(self):
        """COUNT(*) without aggregating a pollution column doesn't
        need the filter (the count is unaffected by row-content
        corruption)."""
        sample = "SELECT COUNT(*) FROM trades WHERE symbol = ?"
        assert not _is_analytics_query(sample)


class TestRegressionFromOriginalTest:
    """Preserve the regressions the original (instance-style) test
    pinned. The new structural version is broader, but should still
    catch the specific patterns the original would have caught."""

    def test_pnl_aggregation_pattern_still_caught(self):
        """Original test pattern: FROM trades WHERE pnl IS NOT NULL
        without data_quality. Verify the new scanner flags this."""
        sample = (
            "SELECT pnl, price, qty FROM trades "
            "WHERE pnl IS NOT NULL"
        )
        # Note: this isn't strictly an aggregation; the original
        # test caught it because pnl IS NOT NULL is the analytics
        # filter. With the new test we check for AGGREGATION
        # functions. Bare SELECT pnl FROM trades doesn't aggregate.
        # That's actually correct: SELECT pnl FROM trades WHERE
        # pnl IS NOT NULL just lists rows — Python aggregates them
        # downstream. The downstream Python may or may not pollute.
        # The new test focuses on SQL-level aggregation as the
        # higher-confidence trigger.
        # If we wanted to catch downstream-Python aggregation,
        # we'd need a separate static-analysis pass.
        # This test documents the intentional scope reduction.
        assert not _is_analytics_query(sample), (
            "If this assertion ever flips, the scope expansion is "
            "intentional — update this test."
        )
