"""Structural guardrail: no `except: pass` or
`except Exception: pass` blocks in critical-path modules without
a written rationale.

The bug class.
Mack's standing memory: "No silent failures — every error must be
surfaced and fixed, not swallowed." A `try: ... except: pass`
block silently swallows errors, leaves the system in an unknown
state, and makes debugging impossible.

Common shapes that surface this:
  - Network call fails, code returns empty list
  - Parse fails, code returns None
  - Background task errors out, scheduler keeps marching
  - Optional optimization fails, no log

The acceptable patterns are:
  1. except SpecificException: ... handled (named exception class)
  2. except Exception: logger.warning(..., exc_info=True); ... handled
  3. # SILENT_OK: <rationale> comment immediately above the except
"""
from __future__ import annotations

import ast
import os
import sys
from typing import List, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Per-file baseline of existing silent-swallow counts as of
# 2026-05-13. The codebase predates this guardrail and has ~100
# legacy silent swallows — many genuinely correct (cache writes,
# per-candidate-loop continues) but undocumented. The proper fix
# is a separate annotation pass adding `# SILENT_OK: <rationale>`
# above each handler. Until then, this test runs as a RATCHET:
# new silent swallows added to ANY file will fail (count exceeds
# baseline). Reducing a file's count below baseline means the
# baseline auto-rebases on next test run (operator should commit
# the lower number to make it sticky).
#
# To regenerate this baseline after intentional reductions:
#   pytest tests/test_no_silent_except_pass.py --rebase
# (Not implemented as flag — operator manually edits this dict.)
GRANDFATHER_BASELINE = {
    # 2026-05-13: full audit pass complete — every silent swallow in
    # production source has been classified and either annotated with
    # `# SILENT_OK: <rationale>` (Cat 1: intentional best-effort
    # enrichment, per-loop continues, cache writes, notify_*) or
    # upgraded to log+continue (Cat 2: intraday_risk_monitor halt
    # lookup). Baseline empty so future violations fail loudly.
}


def _walk_critical_path_files() -> List[str]:
    """Production source. Excludes tests, vendor, scripts."""
    out = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in (
            "venv", "__pycache__", ".git", ".claude", "tests", "exports",
            "backups", "logs", "altdata", "node_modules", "docs",
            "scripts",
        )]
        for f in files:
            if not f.endswith(".py"):
                continue
            if f.startswith("test_") or f.startswith("backfill_"):
                continue
            if f.startswith("recover_") or f.startswith("run_"):
                continue
            out.append(os.path.join(root, f))
    return out


def _is_silent_swallow(handler: ast.ExceptHandler) -> bool:
    """True iff this except clause swallows errors silently:
    body is just `pass` or `continue` AND the handler doesn't
    catch a specific exception class."""
    # Specific exception class? Acceptable.
    if (handler.type is not None
            and not (isinstance(handler.type, ast.Name)
                     and handler.type.id in ("Exception", "BaseException"))):
        return False
    # Body of just pass / continue / return / "ignored"?
    if not handler.body:
        return True
    if len(handler.body) > 1:
        # Multiple statements — likely doing something. Check if any
        # are logger calls or notify_error.
        for stmt in handler.body:
            if _stmt_is_logging_or_notify(stmt):
                return False
        # Not a single pass, but no logging — borderline
        return False
    only = handler.body[0]
    if isinstance(only, ast.Pass):
        return True
    if isinstance(only, ast.Continue):
        return True
    return False


def _stmt_is_logging_or_notify(stmt: ast.stmt) -> bool:
    """Heuristic: stmt is a logger / print / notify call."""
    if not isinstance(stmt, ast.Expr):
        return False
    if not isinstance(stmt.value, ast.Call):
        return False
    target = stmt.value.func
    name_chain = []
    while isinstance(target, ast.Attribute):
        name_chain.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        name_chain.append(target.id)
    name_chain.reverse()
    s = ".".join(name_chain)
    return any(tok in s.lower() for tok in (
        "logger", "logging", "log", "notify_", "print",
    ))


def _has_silent_ok_comment(src: str, lineno: int) -> bool:
    """Check if the line immediately before `lineno` has a
    `# SILENT_OK:` comment."""
    lines = src.split("\n")
    # 1-indexed lineno; check 1-2 lines above the `except`
    for offset in range(1, 4):
        idx = lineno - 1 - offset
        if idx < 0:
            break
        line = lines[idx].strip()
        if line.startswith("# SILENT_OK"):
            return True
        if line and not line.startswith("#"):
            break  # hit a code line — no SILENT_OK comment
    return False


def _scan_violations() -> dict:
    """Walk all critical-path files and return
    {rel_path: count_of_silent_swallows} for files that have any."""
    counts = {}
    for src_path in _walk_critical_path_files():
        rel = os.path.relpath(src_path, REPO_ROOT)
        try:
            with open(src_path) as fh:
                src = fh.read()
        except Exception:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        n = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not _is_silent_swallow(node):
                continue
            if _has_silent_ok_comment(src, node.lineno):
                continue
            n += 1
        if n > 0:
            counts[rel] = n
    return counts


class TestNoSilentExceptPashRatchet:
    """RATCHET test: existing silent swallows are grandfathered;
    new ones fail. Counts only — no per-line tracking, since line
    numbers shift with edits.

    The proper-fix path is a separate annotation pass: walk each
    grandfather-listed file, classify each silent swallow as
    legitimate (cache write, per-loop continue) or buggy, then
    add `# SILENT_OK: <rationale>` to the legitimate ones and
    fix the buggy ones. Estimated 2-3 hour task."""

    def test_no_file_exceeds_baseline_silent_swallow_count(self):
        actual = _scan_violations()
        regressions = []
        new_files = []
        for rel, n in actual.items():
            baseline = GRANDFATHER_BASELINE.get(rel)
            if baseline is None:
                new_files.append((rel, n))
                continue
            if n > baseline:
                regressions.append((rel, baseline, n))
        # Sanity removed (2026-05-13): full audit complete, baseline is
        # empty. The scanner correctly returns 0 violations now. Re-add
        # a sanity check here only if a future change causes the scanner
        # to silently match nothing (e.g. directory walk regression).
        problems = []
        if regressions:
            problems.append(
                "Files with NEW silent swallows added (count > baseline):"
            )
            for rel, baseline, n in regressions:
                problems.append(
                    f"  {rel}: baseline={baseline}, now {n} "
                    f"(+{n - baseline})"
                )
        if new_files:
            problems.append(
                "\nNew files with silent swallows (no baseline):"
            )
            for rel, n in new_files:
                problems.append(f"  {rel}: {n} silent swallows")
        if problems:
            pytest.fail(
                "Silent except-pass count regressed (Mack's "
                "standing memory: 'No silent failures'):\n\n"
                + "\n".join(problems)
                + "\n\nFix the new violations OR — if the swallow "
                "is intentional — add a `# SILENT_OK: <rationale>` "
                "comment on the line above the `except`. Do NOT "
                "just bump the baseline number unless you've added "
                "the comments."
            )

    def test_baseline_doesnt_oversize(self):
        """If a file's baseline is HIGHER than actual, the operator
        reduced violations — celebrate by bumping baseline DOWN.
        Test surfaces this so the ratchet stays tight."""
        actual = _scan_violations()
        improvements = []
        for rel, baseline in GRANDFATHER_BASELINE.items():
            actual_count = actual.get(rel, 0)
            if actual_count < baseline:
                improvements.append((rel, baseline, actual_count))
        if improvements:
            details = "\n".join(
                f"  {rel}: baseline={b}, actual={a} "
                f"(reduce baseline to {a})"
                for rel, b, a in improvements
            )
            pytest.fail(
                "GRANDFATHER_BASELINE has STALE entries — the "
                "files below have FEWER silent swallows than "
                "baseline. Update GRANDFATHER_BASELINE in this "
                "test to lock in the improvement (otherwise a "
                "regression could slip through unnoticed):\n\n"
                + details
            )
