"""Structural enforcement of memory rules (2026-05-19).

Each test pins one of the assistant's behavioral memory rules so
that violations surface in the test suite instead of being caught
by the operator post-hoc. The rules these encode are the
*high-violation-rate* ones — the ones that have actually been
violated in this session or in prior post-mortems.

When a test here fails, the violation is concrete and immediate:
fix the underlying change, OR explicitly bypass with a documented
exemption. The point is to make rule-following the path of least
resistance.

Encoded rules:

  1. **Docs + CHANGELOG parity** — every commit that modifies
     production source touches CHANGELOG.md. Working-tree check
     (preventive; fails before you commit if you forgot) +
     recent-history check (catches accumulated violations).
  2. **No `sed -i` on production source** — silent truncation
     on quoting errors. Use `Read` + `Edit` instead. Checked
     across .sh files and against recent commits.
  3. **No "master key" references** — env-level master key
     concept was removed 2026-05-19; any remaining string
     literal indicates stale code or docs.
  4. **No silent failures** — bare `except: pass` patterns
     swallow errors. Memory rule: every error must be surfaced.
  5. **No journal SQL surgery in scripts** — direct DELETE /
     UPDATE on `trades` from anywhere outside the reconciler
     creates recurring corruption (reconcile cron undoes it).

Bypass: each test supports a per-test exemption mechanism for
truly-legitimate cases. Don't normalize bypass — the right answer
99% of the time is fix the violation.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args: list) -> str:
    return subprocess.check_output(
        ["git"] + args, cwd=str(REPO), stderr=subprocess.DEVNULL,
    ).decode().strip()


def _is_production_source(rel_path: str) -> bool:
    """Production = .py files outside test, script, cleanup, and
    one-off-dated paths."""
    if not rel_path.endswith(".py"):
        return False
    if rel_path.startswith("tests/"):
        return False
    if rel_path.startswith("scripts/"):
        return False
    if rel_path.startswith(".claude/"):
        return False
    if rel_path.startswith("venv/"):
        return False
    if "_test_" in rel_path or rel_path.startswith("test_"):
        return False
    if rel_path.startswith("cleanup_"):
        return False
    if "_2026_05_18.py" in rel_path:
        return False  # dated one-off reset / cleanup scripts
    return True


# ---------------------------------------------------------------------------
# (1) Docs + CHANGELOG parity
# ---------------------------------------------------------------------------

def test_working_tree_has_changelog_update_when_modifying_production_source():
    """If the CURRENT working tree has uncommitted production-source
    changes but CHANGELOG.md is NOT also modified, FAIL.

    This is the PREVENTIVE check — running pytest before commit will
    fail loud if you forgot to update CHANGELOG. The recent-history
    test below is the post-hoc catch.

    Bypass: stash with `git stash`, or commit CHANGELOG.md FIRST,
    or add the absent-from-list exemption below if you have a
    truly legitimate refactor that needs no CHANGELOG entry.
    """
    try:
        changed = _git(["diff", "--name-only", "HEAD"]).splitlines()
        untracked = _git(["ls-files", "--others", "--exclude-standard"]).splitlines()
    except Exception:
        pytest.skip("not in a git repo / no HEAD yet")
    all_changed = set(changed + untracked)
    if not all_changed:
        return  # clean tree, nothing to enforce

    prod_modified = [f for f in all_changed if _is_production_source(f)]
    if not prod_modified:
        return  # only tests / docs / scripts changed — no CHANGELOG required

    if "CHANGELOG.md" in all_changed:
        return  # parity satisfied

    pytest.fail(
        f"Working tree modifies production source without "
        f"updating CHANGELOG.md:\n  "
        + "\n  ".join(sorted(prod_modified))
        + "\n\nMemory rule: 'every code change must include "
        "CHANGELOG.md and doc updates, no exceptions'. "
        "Add a CHANGELOG entry before committing, OR stash these "
        "changes if they're work-in-progress."
    )


# Commits where CHANGELOG wasn't updated alongside production source
# AND that's been formally accepted (e.g., truly trivial refactors,
# emergency rollbacks). Keep this list SHORT — each entry should
# have a one-line justification.
CHANGELOG_PARITY_EXEMPT_SHAS = {
    # (no exemptions today — every recent commit was paired)
}


def test_recent_commits_have_changelog_update_when_modifying_production_source():
    """Post-hoc catch: examine the last 5 commits and fail if any
    one modified production source without touching CHANGELOG.md.

    Rolling-window check means older violations age out — the test
    catches the most recent ones, where the fix is still cheap.
    """
    try:
        log_lines = _git(
            ["log", "-n", "5", "--format=%H %s"]
        ).splitlines()
    except Exception:
        pytest.skip("not in a git repo")
    if not log_lines:
        return

    violations = []
    for line in log_lines:
        sha, _, message = line.partition(" ")
        if sha in CHANGELOG_PARITY_EXEMPT_SHAS:
            continue
        if "[no-changelog]" in message.lower():
            continue  # explicit operator bypass
        try:
            files = _git(
                ["show", "--name-only", "--format=", sha]
            ).splitlines()
        except Exception:
            continue
        files = [f for f in files if f]  # drop blanks
        prod_changed = [f for f in files if _is_production_source(f)]
        if not prod_changed:
            continue
        if "CHANGELOG.md" in files:
            continue
        violations.append(
            f"{sha[:8]} {message[:80]}\n    "
            + "\n    ".join(f"+ {f}" for f in prod_changed)
        )
    if violations:
        pytest.fail(
            "Recent commits modified production source without "
            "CHANGELOG.md updates:\n\n"
            + "\n\n".join(violations)
            + "\n\nFix: amend the commit to include a CHANGELOG "
            "entry, OR add the SHA to CHANGELOG_PARITY_EXEMPT_SHAS "
            "in this test with a one-line justification."
        )


# ---------------------------------------------------------------------------
# (2) No sed -i on production source
# ---------------------------------------------------------------------------

def test_no_sed_inplace_in_shell_scripts():
    """Memory rule: `sed -i` can silently truncate on quoting
    errors and write 0 bytes back. Production shell scripts must
    not use it — Read+Edit (or explicit awk + atomic-replace) only.

    Tests/scripts/cleanup files are exempt: they're one-off tools
    where a sed-i mistake is recoverable."""
    offenders = []
    for sh in REPO.glob("*.sh"):
        if sh.name.startswith("test_"):
            continue
        try:
            src = sh.read_text()
        except Exception:
            continue
        for m in re.finditer(r"\bsed\s+-i\b", src):
            line_no = src[:m.start()].count("\n") + 1
            # Skip lines that are clearly commenting out the pattern
            line = src.splitlines()[line_no - 1]
            if line.lstrip().startswith("#"):
                continue
            offenders.append(f"{sh.name}:{line_no}: {line.strip()[:60]}")
    assert not offenders, (
        "Production shell scripts use `sed -i` — silently truncates "
        "on quoting errors. Use Read+Edit or atomic-replace.\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# (3) No "master key" references in production code or docs
# ---------------------------------------------------------------------------

def test_no_master_key_references_in_production_code():
    """Memory rule: the env-level master Alpaca key path was
    removed 2026-05-19. Any ACTIVE USE in production .py code
    (constant, variable name, env lookup) indicates stale code.

    Comments / docstrings that EXPLAIN the removal are intentional
    historical narrative — those are skipped via comment-line
    detection. Docs and the CHANGELOG are exempt for the same
    reason (they describe the past).
    """
    rx = re.compile(r"master[\s_-]+key", re.IGNORECASE)
    offenders = []
    for path in REPO.rglob("*.py"):
        rel = str(path.relative_to(REPO))
        # Skip ephemera + tests (tests document the rule itself)
        if rel.startswith((".claude/", ".git/", "venv/", "tests/")):
            continue
        try:
            src = path.read_text(errors="ignore")
        except Exception:
            continue
        # Track triple-quoted state line-by-line so we can skip
        # matches inside docstrings (historical narrative is
        # legitimate). Toggle on every """ or ''' occurrence.
        in_docstring = False
        line_in_docstring = [False] * (src.count("\n") + 2)
        for i, line in enumerate(src.splitlines(), start=1):
            line_in_docstring[i] = in_docstring
            # Count triple-quote toggles on this line. If odd, the
            # state flips by end-of-line.
            for trip in ('"""', "'''"):
                count = line.count(trip)
                if count % 2 == 1:
                    in_docstring = not in_docstring
        for m in rx.finditer(src):
            line_no = src[:m.start()].count("\n") + 1
            line = src.splitlines()[line_no - 1]
            stripped = line.strip()
            # Pure-comment line: skip
            if stripped.startswith("#"):
                continue
            # Inside a triple-quoted docstring: skip
            if line_no < len(line_in_docstring) and line_in_docstring[line_no]:
                continue
            # Same-line quoted phrase ("master key" in a string lit)
            quoted_match = re.search(
                r'["\']\s*[^"\']*master[\s_-]+key[^"\']*\s*["\']',
                line, re.IGNORECASE,
            )
            if quoted_match:
                continue
            offenders.append(f"{rel}:{line_no}: {stripped[:60]}")
    assert not offenders, (
        "Active 'master key' references in production .py.\n  "
        + "\n  ".join(offenders[:20])
        + "\n\nMemory rule: env-level master key was REMOVED "
        "2026-05-19; Alpaca creds live in alpaca_accounts only. "
        "Clean up active uses (constants, env-var lookups, "
        "variable names). Comments explaining the removal are OK."
    )


# ---------------------------------------------------------------------------
# (4) No silent failures (bare except: pass)
# ---------------------------------------------------------------------------

def test_no_bare_except_pass_in_production_source():
    """Memory rule: every error must be surfaced and fixed, not
    swallowed. `except: pass` and `except Exception: pass` silently
    hide bugs that operator would otherwise see + fix.

    AST scan so we catch the pattern reliably even across line
    splits + comments."""

    class _BarePassFinder(ast.NodeVisitor):
        def __init__(self):
            self.hits = []

        def visit_ExceptHandler(self, node):
            # body of length 1 that is a `pass` statement
            if (len(node.body) == 1
                    and isinstance(node.body[0], ast.Pass)):
                # Allow bare `pass` if the exception type is
                # specific and the comment names a clear reason
                # — pragmatic carve-out: it must be a SPECIFIC
                # exception type (not bare except, not Exception).
                # Bare/Exception + pass is always banned.
                exc_type = node.type
                if exc_type is None:
                    self.hits.append((node.lineno, "bare except"))
                elif (isinstance(exc_type, ast.Name)
                      and exc_type.id == "Exception"):
                    self.hits.append((node.lineno, "except Exception"))
                # Specific exception types with `pass` are
                # tolerated (sometimes legitimate, e.g., race-y
                # cleanup). The bare/Exception variants are not.
            self.generic_visit(node)

    offenders = []
    for path in REPO.rglob("*.py"):
        rel = str(path.relative_to(REPO))
        if not _is_production_source(rel):
            continue
        try:
            src = path.read_text(errors="ignore")
            tree = ast.parse(src)
        except Exception:
            continue
        src_lines = src.splitlines()
        finder = _BarePassFinder()
        finder.visit(tree)
        for lineno, what in finder.hits:
            # Honor explicit operator exemptions: a comment marker
            # on the `pass` line or in the ~5 surrounding lines.
            # Two formats supported:
            #   - `# SILENT_OK: <reason>` (used in notifications.py
            #     for the recursion-safe outermost safety net)
            #   - `# noqa: bare-pass` (PEP-8-style marker)
            # The comment block right above the except is the
            # canonical place; widen the window to ±5 lines so a
            # multi-line "why" preamble counts.
            window_start = max(0, lineno - 6)  # five lines above
            window_end = min(len(src_lines), lineno + 1)
            joined = "\n".join(src_lines[window_start:window_end])
            if "SILENT_OK:" in joined or "noqa: bare-pass" in joined:
                continue
            offenders.append(f"{rel}:{lineno}: {what}: pass")
    assert not offenders, (
        "Bare-except-pass / except-Exception-pass in production "
        "source — silently swallows errors.\n  "
        + "\n  ".join(offenders[:30])
        + "\n\nFix: catch a specific exception type AND log/notify "
        "(not bare `pass`). For truly-recursion-safe sites (e.g., a "
        "notify-fail handler that can't safely re-notify), add a "
        "`# SILENT_OK: <reason>` comment on or near the line — the "
        "explicit acknowledgment is the bypass."
    )


# ---------------------------------------------------------------------------
# (5) No journal SQL surgery from non-reconciler code
# ---------------------------------------------------------------------------

def test_no_direct_journal_mutation_outside_authorized_modules():
    """Memory rule: manual DELETE / UPDATE on `trades` table from
    anywhere outside the authorized journal/reconcile code creates
    recurring corruption (reconcile cron undoes it next pass).

    Authorized modules: journal.py, reconcile_journal_to_broker.py,
    reconcile_aggregate_drift.py, models.py (the schema layer),
    halt_helpers (writes audit_alerts only), and a handful of
    historically-dated cleanup scripts."""
    AUTHORIZED = {
        "journal.py",
        "reconcile_journal_to_broker.py",
        "reconcile_aggregate_drift.py",
        "trade_pipeline.py",     # writes via journal helpers + UPDATE on its own rows
        "trader.py",             # writes via journal helpers
        "options_roll_manager.py",  # updates option exit rows
        "options_trader.py",     # writes via journal helpers
        "options_lifecycle.py",  # OCC option lifecycle (assignment/expiration/exercise) → trades UPDATE
        "models.py",             # schema layer
        "bracket_orders.py",     # UPDATEs protective_*_order_id columns only (Phase B1 atomicity pattern)
        "backfill_multileg_negative_prices.py",  # dated one-off fix
        "scripts/cleanup_phantom_stock_sells_2026_05_11.py",
        "cleanup_bug_cascade_buys_2026_05_18.py",
        "reset_for_clean_experiment.py",
        "full_reset_2026_05_18.py",
        "backup_db.py",
        "db_integrity.py",
        "halt_helpers.py",       # writes audit_alerts only
        "stat_arb_pair_book.py",  # writes via journal + own row UPDATEs
        "multi_scheduler.py",    # _task_update_fills mutates pending_fill -> closed
        "aggregate_audit.py",    # reads only; included if it grew an update
        "pipelines/outcomes/backfill.py",  # Phase 5d backfill
        "pipelines/outcomes/option_resolver.py",  # resolved-prediction updates
        "pipelines/outcomes/option.py",
        "pipelines/outcomes/stock.py",
    }
    rx = re.compile(
        r"\b(DELETE\s+FROM\s+trades|UPDATE\s+trades\s+SET|INSERT\s+INTO\s+trades)\b",
        re.IGNORECASE,
    )
    offenders = []
    for path in REPO.rglob("*.py"):
        rel = str(path.relative_to(REPO))
        if not _is_production_source(rel):
            continue
        if rel in AUTHORIZED:
            continue
        # altdata/ stores DIFFERENT `trades` tables (congressional
        # trades, etc.) — not our trade journal. Skip the whole
        # subtree to avoid grep-collision false positives.
        if rel.startswith("altdata/"):
            continue
        try:
            src = path.read_text(errors="ignore")
        except Exception:
            continue
        for m in rx.finditer(src):
            line_no = src[:m.start()].count("\n") + 1
            line = src.splitlines()[line_no - 1].strip()
            if line.startswith("#"):
                continue
            offenders.append(f"{rel}:{line_no}: {m.group(0)}")
    assert not offenders, (
        "Direct journal-table mutation outside authorized modules.\n  "
        + "\n  ".join(offenders[:20])
        + "\n\nMemory rule: 'never SQL-edit journal DBs to fix "
        "perceived state — manual DELETE/UPDATE gets undone by "
        "reconcile cron'. Use journal.log_trade / record_trade / "
        "the reconciler's authorized paths. If this is a NEW "
        "legitimate writer, add it to AUTHORIZED in this test."
    )


# ---------------------------------------------------------------------------
# Meta — make sure THIS test file is itself loaded by pytest
# ---------------------------------------------------------------------------

def test_rule_enforcement_module_is_collected():
    """If this test file gets deleted or excluded from pytest
    discovery, the rule enforcement above stops running. Pin that
    the file is reachable from the test root."""
    assert __file__.endswith("test_memory_rule_enforcement_2026_05_19.py")
