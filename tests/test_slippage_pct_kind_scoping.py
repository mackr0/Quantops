"""Class-invariant guardrail (2026-05-12): every slippage_pct
aggregate query must scope to a SINGLE instrument class
(stocks OR options), never both.

The 2026-05-11 +1130% Avg-Slippage incident was caused by mixing
stock and option rows in one slippage_pct AVG. Option premium
%-moves (10-100% per cycle on small underlying moves) dominated
the average. Phase 1 added the per-pipeline metrics module but
the legacy display in views.py was still aggregating
unscoped — Mack saw the bug again on 2026-05-12.

This test scans:
- Every `AVG(slippage_pct)` SQL fragment must be paired with
  either `occ_symbol IS NULL` (stocks) OR `occ_symbol IS NOT NULL`
  (options) in the same WHERE clause.
- Every `get_slippage_stats(...)` call must pass `kind=` (stocks
  or options or a variable) — the unscoped default is dangerous.

Allowlist exists for the journal helper itself (which DOES
expose an unscoped option) plus the per-pipeline metrics modules
that compose it correctly.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

REPO_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)


# Files allowed to compute unscoped slippage aggregates (the
# helper itself + tests).
SLIPPAGE_HELPER_ALLOWLIST = {
    "journal.py":
        "Defines get_slippage_stats — kind=None branch is the helper's "
        "implementation, not a consumer.",
    # 2026-05-12 — metrics/legacy.py was previously here as
    # "operates on the result" but it's actually a consumer that
    # CALLS get_slippage_stats unscoped. That was the source of the
    # SECOND +1130% incident on the performance.html page. Removed
    # from the allowlist; metrics/legacy.py now passes kind="stocks"
    # explicitly.
}


# Files scanned for slippage usage. Add new analytics paths here.
SCANNED_FILES = [
    "views.py", "models.py",
    "ai_tracker.py", "self_tuning.py",
    "metrics/stock.py", "metrics/option.py", "metrics/portfolio.py",
]


def _read(relpath: str) -> str:
    full = os.path.join(REPO_ROOT, relpath)
    if not os.path.exists(full):
        return ""
    with open(full) as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Pattern 1 — AVG(slippage_pct) must be paired with occ_symbol filter
# ---------------------------------------------------------------------------

class TestAvgSlippagePctScoping:
    """Every SQL block computing AVG(slippage_pct) or
    SUM(slippage_pct) MUST also include `occ_symbol IS NULL`
    (stocks) OR `occ_symbol IS NOT NULL` (options) in the same
    SQL string. Without it, mixed-kind aggregation produces the
    +1130% noise."""

    AGG_PATTERN = re.compile(
        r"(?:AVG|SUM)\s*\(\s*slippage_pct\s*\)",
        re.IGNORECASE,
    )

    def _aggregate_sql_blocks(self, source: str):
        """Yield ((line_number, sql_block_text)) for every multi-line
        SQL string that contains AVG(slippage_pct) or SUM(slippage_pct).
        Coarse: matches the surrounding triple-quoted block OR the
        line."""
        # Find each match's line number via prefix counting.
        for m in self.AGG_PATTERN.finditer(source):
            start = m.start()
            line_no = source[:start].count("\n") + 1
            # Pull the surrounding ~1000 chars (covers most multi-
            # line SQL blocks while keeping the search local —
            # 300 was cutting off mid-word on long WHERE clauses).
            chunk_start = max(0, start - 500)
            chunk_end = min(len(source), start + 500)
            chunk = source[chunk_start:chunk_end]
            yield line_no, chunk

    def test_every_slippage_avg_includes_occ_symbol_filter(self):
        offenses = []
        for relpath in SCANNED_FILES:
            if relpath in SLIPPAGE_HELPER_ALLOWLIST:
                continue
            src = _read(relpath)
            for ln, chunk in self._aggregate_sql_blocks(src):
                # Look for occ_symbol filter in the surrounding chunk
                if "occ_symbol IS NULL" in chunk or \
                   "occ_symbol IS NOT NULL" in chunk:
                    continue
                offenses.append(
                    f"{relpath}:{ln}  AVG(slippage_pct) without "
                    f"occ_symbol filter"
                )
        if offenses:
            pytest.fail(
                "Slippage_pct aggregate without instrument-class "
                "scoping. Mixing stock and option rows produces "
                "noise (+1130% incident on 2026-05-11). Add "
                "`AND occ_symbol IS NULL` (stocks) OR "
                "`AND occ_symbol IS NOT NULL` (options) to the "
                "same SQL block.\n\n"
                + "\n".join(f"  {o}" for o in offenses)
            )


# ---------------------------------------------------------------------------
# Pattern 2 — get_slippage_stats(...) must be called with kind= arg
# ---------------------------------------------------------------------------

class TestGetSlippageStatsHasKind:
    """Every consumer of `get_slippage_stats(...)` should pass an
    explicit `kind=` argument. The unscoped default (kind=None)
    is dangerous because it returns the same broken mixed-kind
    aggregate that produced the +1130% display."""

    CALL_PATTERN = re.compile(
        r"get_slippage_stats\s*\(([^)]*)\)"
    )

    def test_consumers_pass_explicit_kind(self):
        offenses = []
        for relpath in SCANNED_FILES:
            if relpath in SLIPPAGE_HELPER_ALLOWLIST:
                continue
            src = _read(relpath)
            for m in self.CALL_PATTERN.finditer(src):
                args = m.group(1)
                # Allow if `kind=` appears in the args (positional
                # or keyword).
                if "kind=" in args or "kind =" in args:
                    continue
                line_no = src[:m.start()].count("\n") + 1
                offenses.append(
                    f"{relpath}:{line_no}  get_slippage_stats({args!r}) "
                    f"missing explicit kind="
                )
        if offenses:
            pytest.fail(
                "get_slippage_stats called without explicit kind=. "
                "Without scoping, the call returns mixed-kind "
                "aggregates that produce nonsense %-values "
                "(+1130% incident). Pass kind='stocks' OR "
                "kind='options' OR kind=variable.\n\n"
                + "\n".join(f"  {o}" for o in offenses)
            )


# ---------------------------------------------------------------------------
# Allowlist hygiene
# ---------------------------------------------------------------------------

class TestSlippageAllowlistHygiene:
    def test_allowlist_files_still_exist(self):
        for relpath, rationale in SLIPPAGE_HELPER_ALLOWLIST.items():
            assert rationale, (
                f"Empty rationale for slippage allowlist entry: "
                f"{relpath}"
            )
            assert os.path.exists(
                os.path.join(REPO_ROOT, relpath)
            ), (
                f"Allowlisted file no longer exists: {relpath}. "
                f"Remove from SLIPPAGE_HELPER_ALLOWLIST."
            )
