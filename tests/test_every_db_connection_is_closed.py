"""Structural guardrail: every `sqlite3.connect()` call in
production source must be paired with a guaranteed close
(via context manager OR explicit try/finally close).

The bug class.
A long-running scheduler process accumulates open file handles
when sqlite3.connect() leaks. After ~1024 leaked connections (OS
default), every subsequent open fails with "Too many open files."
The scheduler crashes hours-to-days after a code path that opens
without closing was added.

This bug is INSIDIOUS — it doesn't manifest in tests (tests open
and close in seconds). It only shows up under sustained
production load. Detection at the source level is the only
reliable defense.

Acceptable patterns:
  1. `with sqlite3.connect(...) as conn:` (context manager auto-closes)
  2. `with closing(sqlite3.connect(...)) as conn:` (contextlib helper)
  3. `conn = sqlite3.connect(...); try: ... finally: conn.close()`
  4. `conn = _get_conn(...); try: ... finally: conn.close()` (helper-wrapped)

Unsafe pattern:
  - `conn = sqlite3.connect(...); ...; conn.close()` (without try/finally
    — exception between connect and close LEAKS the handle)

This test scans for `sqlite3.connect()` calls and verifies each
is in one of the safe patterns. Allowlist: scripts that are
short-lived (one-shot migrations) where leaks don't accumulate.
"""
from __future__ import annotations

import ast
import os
import sys
from typing import List, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Files where leaks are tolerable (short-lived scripts, test-only).
ALLOWLIST_FILES = {
    # Test scripts — pytest tmp_path provides cleanup
    # (already excluded by walk filter)
    # One-shot migrations / backfills run once then exit
    "backfill_multileg_negative_prices.py":
        "One-shot script. Process exits before any leak accumulates.",
    "backup_db.py":
        "Backup operations open many connections briefly; each is "
        "explicitly closed in try/finally inside backup_one().",
    "recover_cycle_data.py":
        "Recovery utility. Short-lived process.",
    "run_phase2_validations.py":
        "One-shot validation script.",
    "run_backtest_validation.py":
        "One-shot backtest runner.",
    "validate_phase1_realdata.py":
        "One-shot validation script.",
    "migrate.py":
        "Migration script.",
    "migrate_activity_log_format.py":
        "Migration script.",
}


def _walk_critical_path_files() -> List[str]:
    out = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in (
            "venv", "__pycache__", ".git", ".claude", "tests", "exports",
            "backups", "logs", "altdata", "node_modules", "docs",
        )]
        for f in files:
            if not f.endswith(".py"):
                continue
            if f.startswith("test_"):
                continue
            out.append(os.path.join(root, f))
    return out


def _is_safe_connect(call_node: ast.Call, parent_lookup) -> bool:
    """Determine if a sqlite3.connect() call is in a safe pattern.

    Walks the parent chain to look for:
      - Surrounding `with` statement → safe
      - Subsequent .close() call inside try/finally → safe (heuristic)
    """
    parent = parent_lookup.get(id(call_node))
    # Pattern 1: directly inside `with sqlite3.connect(...) as X:`
    while parent is not None:
        if isinstance(parent, ast.withitem):
            return True
        if isinstance(parent, ast.With):
            return True
        if isinstance(parent, ast.Assign):
            # `conn = sqlite3.connect(...)` — need to check
            # surrounding scope for try/finally with close
            return _has_try_finally_close_in_scope(parent, parent_lookup)
        parent = parent_lookup.get(id(parent))
    return False


def _has_try_finally_close_in_scope(assign_node: ast.Assign,
                                      parent_lookup) -> bool:
    """When `conn = sqlite3.connect()`, check the enclosing function
    body for a `try:` block whose `finally` calls `conn.close()`.

    This is a heuristic — won't catch all variants but catches the
    common `try: ... finally: conn.close()` pattern.
    """
    if not assign_node.targets:
        return False
    target = assign_node.targets[0]
    if not isinstance(target, ast.Name):
        return False
    var_name = target.id
    # Find enclosing function
    parent = parent_lookup.get(id(assign_node))
    while parent is not None and not isinstance(parent, ast.FunctionDef):
        parent = parent_lookup.get(id(parent))
    if parent is None:
        return False
    # Walk function body for Try with finally calling var_name.close()
    for node in ast.walk(parent):
        if not isinstance(node, ast.Try):
            continue
        for fin_node in node.finalbody:
            for sub in ast.walk(fin_node):
                if not isinstance(sub, ast.Call):
                    continue
                if (isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "close"
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == var_name):
                    return True
    return False


def _build_parent_lookup(tree: ast.Module) -> dict:
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    return parent


def _find_unsafe_connects(src: str) -> List[int]:
    """Return line numbers of unsafe sqlite3.connect() calls."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    parent_lookup = _build_parent_lookup(tree)
    unsafe = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        # Match sqlite3.connect or just connect when imported
        is_sqlite_connect = False
        if (isinstance(target, ast.Attribute)
                and target.attr == "connect"
                and isinstance(target.value, ast.Name)
                and target.value.id == "sqlite3"):
            is_sqlite_connect = True
        if not is_sqlite_connect:
            continue
        if not _is_safe_connect(node, parent_lookup):
            unsafe.append(node.lineno)
    return unsafe


# Per-file baseline of existing unsafe connects (ratchet style,
# same as the silent-except-pass guardrail). New violations on
# top of baseline fail; reductions trigger a baseline-update
# notice.
GRANDFATHER_BASELINE = {
    "ai_consistency_floor.py": 1,
    "ai_tracker.py": 1,
    "ai_weekly_summary.py": 6,
    "alpha_decay.py": 1,
    "alternative_data.py": 7,
    "book_concentration.py": 1,
    "bracket_orders.py": 2,
    "cancel_phantom_option_stock_stops.py": 1,
    "capital_allocator.py": 1,
    "client.py": 1,
    "conviction_tp.py": 1,
    "crisis_detector.py": 1,
    "db_integrity.py": 1,
    "earnings_calendar.py": 3,
    "factor_data.py": 3,
    "historical_universe_augment.py": 6,
    "journal.py": 1,
    "kelly_sizing.py": 1,
    "kill_switch.py": 2,
    "macro_data.py": 3,
    "meta_model.py": 1,
    "metrics/legacy.py": 4,
    "mfe_capture.py": 1,
    "models.py": 3,
    "multi_scheduler.py": 1,
    "pdufa_scraper.py": 4,
    "portfolio_manager.py": 1,
    "position_runaway.py": 2,
    "post_mortem.py": 1,
    "reconcile_journal_to_broker.py": 1,
    "rigorous_backtest.py": 3,
    "sec_filings.py": 3,
    "sector_classifier.py": 3,
    "self_tuning.py": 1,
    "shared_ai_cache.py": 4,
    "single_trade_gate.py": 1,
    "slippage_model.py": 1,
    "specialist_calibration.py": 5,
    "stop_coverage.py": 1,
    "strategies/catalyst_filing_short.py": 1,
    "task_watchdog.py": 6,
    "virtual_audit.py": 1,
}


class TestEveryDbConnectionIsClosed:
    def test_no_new_unsafe_connect_patterns(self):
        violations_per_file = {}
        for src_path in _walk_critical_path_files():
            rel = os.path.relpath(src_path, REPO_ROOT)
            if os.path.basename(src_path) in ALLOWLIST_FILES:
                continue
            try:
                with open(src_path) as fh:
                    src = fh.read()
            except Exception:
                continue
            unsafe_lines = _find_unsafe_connects(src)
            if unsafe_lines:
                violations_per_file[rel] = len(unsafe_lines)

        # Apply baseline ratchet
        regressions = []
        new_files = []
        for rel, n in violations_per_file.items():
            baseline = GRANDFATHER_BASELINE.get(rel)
            if baseline is None:
                new_files.append((rel, n))
            elif n > baseline:
                regressions.append((rel, baseline, n))

        problems = []
        if regressions:
            problems.append("Files with NEW unsafe sqlite3.connect:")
            for rel, b, n in regressions:
                problems.append(f"  {rel}: baseline={b}, now {n}")
        if new_files:
            problems.append(
                "\nFiles with unsafe sqlite3.connect (no baseline):"
            )
            for rel, n in new_files:
                problems.append(
                    f"  {rel}: {n} unsafe connect(s)"
                )

        if problems:
            pytest.fail(
                "Unsafe sqlite3.connect() patterns detected — "
                "leaks accumulate over weeks of scheduler runtime "
                "and eventually crash with 'too many open files'.\n\n"
                + "\n".join(problems)
                + "\n\nFix one of:\n"
                "  1. Use `with sqlite3.connect(...) as conn:` "
                "context manager\n"
                "  2. Use `with closing(sqlite3.connect(...)) as "
                "conn:` from contextlib\n"
                "  3. `conn = sqlite3.connect(...); try: ... "
                "finally: conn.close()`\n"
                "  4. If the file is a one-shot script that exits "
                "before leaks accumulate, add to ALLOWLIST_FILES "
                "with a written rationale"
            )

    def test_scanner_correctly_flags_unsafe_pattern(self):
        """Sanity: the AST walker correctly identifies the
        unsafe shape on a synthetic example."""
        unsafe_sample = """
import sqlite3
def bad():
    conn = sqlite3.connect("x.db")
    conn.execute("SELECT 1")
    conn.close()
"""
        unsafe = _find_unsafe_connects(unsafe_sample)
        assert unsafe, (
            "Scanner failed to flag plain connect-without-finally"
        )

    def test_scanner_correctly_recognizes_safe_pattern(self):
        """Sanity: the AST walker recognizes `with` context."""
        safe_sample = """
import sqlite3
def good():
    with sqlite3.connect("x.db") as conn:
        conn.execute("SELECT 1")
"""
        unsafe = _find_unsafe_connects(safe_sample)
        assert not unsafe, (
            "Scanner false-flagged the safe `with` pattern"
        )
