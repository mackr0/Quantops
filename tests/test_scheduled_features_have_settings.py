"""Guardrail: every per-profile scheduled feature must have a
settings-page toggle.

Pattern this catches: shipping a `_task_X(ctx)` registered via
`run_task("[seg_label] X", lambda: _task_X(ctx), db_path=ctx.db_path)`
without a per-profile enable/disable. The job runs unconditionally for
every profile, the user has no way to see it exists or turn it off,
and module constants stay buried.

How the guardrail works:

  1. Parse `multi_scheduler.py` and find every `_task_*` registered
     via `run_task(...)`.
  2. For each one, classify:
       - INFRASTRUCTURE: kept on the explicit allowlist below; runs
         for every profile because it's load-bearing (e.g.
         `_task_resolve_predictions`, `_task_daily_snapshot`).
       - GATED: the `run_task` call is wrapped in an `if
         getattr(ctx, "enable_X", ...)` check. Confirm `enable_X`
         exists as a column in `trading_profiles` AND has a control
         in `templates/settings.html`.
  3. Fail if any task is neither INFRASTRUCTURE nor GATED.

This would have caught the Item 2a/2b/1b ship where the new tasks
ran with no toggle, no settings control, and no test to flag it.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# Tasks that legitimately run for every profile every cycle. Each
# entry is a `_task_*` name. Anything else MUST be gated by an
# `enable_*` toggle that's visible on the settings page.
INFRASTRUCTURE_TASKS = {
    # ── Core trade pipeline / accounting — never optional ────────────
    "_task_resolve_predictions",       # mark resolved → close the loop
    "_task_daily_snapshot",            # equity / pnl tracking
    "_task_cost_check",                # AI spend guard
    "_task_cross_account_reconcile",   # virtual account ledger sync
    "_task_self_tune",                 # autonomous parameter tuning
    "_task_retrain_meta_model",        # daily GBM + SGD bootstrap
    "_task_calibrate_specialists",     # Platt-scaling per specialist
    "_task_specialist_health_check",   # auto-disable bad specialists
    "_task_universe_audit",            # survivorship-bias correction
    "_task_alpha_decay",               # per-strategy decay monitoring
    "_task_post_mortem",               # weekly losing-week analysis
    "_task_scan_and_trade",            # THE trade loop — never optional
    "_task_check_exits",               # exit polling fallback
    "_task_cancel_stale_orders",       # order hygiene
    "_task_update_fills",              # broker fill sync
    "_task_reconcile_trade_statuses",  # DB consistency sweep

    # ── Options lifecycle — no-op when profile holds no options ─────
    "_task_options_roll_manager",      # auto-roll credit at 80% max profit
    "_task_options_delta_hedger",      # delta-rebalance long calls/puts

    # ── Safety / event layers — load-bearing ────────────────────────
    "_task_crisis_monitor",            # cross-asset capital preservation
    "_task_event_tick",                # event bus dispatcher
    "_task_run_watchdog",              # self-healing for stuck tasks

    # ── Strategy + capital lifecycle — system invariants ────────────
    "_task_auto_strategy_lifecycle",   # auto-disable bad strategies
    "_task_auto_strategy_generation",  # commission new strategies
    "_task_capital_rebalance",         # auto-allocator scale updates

    # ── Operational ───────────────────────────────────────────────
    "_task_db_backup",                 # nightly backup
    "_task_daily_summary_email",       # email digest (silent if no SMTP)
    "_task_options_lifecycle",         # exercise/assignment detection on
                                         # options positions; no-op when none
    "_task_virtual_audit",             # virtual-profile reconciliation
    "_task_sec_filings",               # daily SEC EDGAR scan; once per
                                         # market_type, not per profile
    "_task_weekly_digest",             # Sunday-only weekly summary
    "_task_app_store_snapshot",        # daily idempotent — once per UTC
                                         # day across all profiles
}


def _multi_scheduler_src() -> str:
    path = os.path.join(
        os.path.dirname(__file__), "..", "multi_scheduler.py"
    )
    with open(path) as f:
        return f.read()


def _settings_html() -> str:
    path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "settings.html"
    )
    with open(path) as f:
        return f.read()


def _profile_columns():
    """Schema columns on trading_profiles (CREATE TABLE + ALTER ADDs
    in models.py)."""
    path = os.path.join(os.path.dirname(__file__), "..", "models.py")
    with open(path) as f:
        src = f.read()
    cols = set()
    # CREATE TABLE block columns
    create_start = src.find(
        "CREATE TABLE IF NOT EXISTS trading_profiles"
    )
    create_end = src.find(");", create_start)
    create_block = src[create_start:create_end]
    cols.update(re.findall(
        r"^\s+([a-z_][a-z_0-9]*)\s+(?:INTEGER|REAL|TEXT|BLOB)",
        create_block, re.M,
    ))
    # ALTER TABLE migrations registered in the _migrations list.
    mig_iter = re.finditer(
        r'\(\s*"trading_profiles"\s*,\s*"([a-z_][a-z_0-9]*)"\s*,',
        src,
    )
    for m in mig_iter:
        cols.add(m.group(1))
    return cols


def _task_call_blocks(src: str):
    """Yield (gate_expr, task_name, line_no) for each `lambda: _task_X(ctx)`
    registered in the scheduler.

    For each lambda we first walk up to find the `run_task(` call line
    (the lambda is one of its kwargs). Then we look for an enclosing
    `if getattr(ctx, "enable_*", ...)` whose indent is strictly less
    than the run_task line's indent — that's the gate that wraps it.
    """
    lines = src.splitlines()
    out = []
    task_re = re.compile(r"lambda:\s*(_task_[a-zA-Z_0-9]+)\s*\(")
    runtask_re = re.compile(r"^(\s*)run_task\s*\(")
    gate_re = re.compile(
        r'^(\s*)if\s+getattr\(\s*ctx\s*,\s*["\'](enable_[a-z_0-9]+)["\']'
    )
    indent_of = lambda s: len(s) - len(s.lstrip(" "))
    for i, line in enumerate(lines):
        m = task_re.search(line)
        if not m:
            continue
        task_name = m.group(1)
        # Walk up to find the run_task() line
        runtask_line = None
        runtask_indent = None
        for j in range(i, max(i - 6, -1), -1):
            rm = runtask_re.match(lines[j])
            if rm:
                runtask_line = j
                runtask_indent = len(rm.group(1))
                break
        if runtask_line is None:
            out.append((None, task_name, i + 1))
            continue
        # Now search above the run_task line for a wrapping if-gate.
        # 60 lines covers gates that wrap 2-3 sibling run_task calls.
        gate = None
        for j in range(runtask_line - 1, max(runtask_line - 60, -1), -1):
            gm = gate_re.match(lines[j])
            if gm:
                gate_indent = len(gm.group(1))
                if gate_indent < runtask_indent:
                    gate = gm.group(2)
                break
            stripped = lines[j].strip()
            if not stripped or stripped.startswith("#"):
                continue
            if indent_of(lines[j]) < runtask_indent:
                # Hit a sibling statement at less indent — left the
                # potential gating block.
                break
        out.append((gate, task_name, i + 1))
    return out


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

class TestEveryScheduledFeatureHasSettingsToggle:
    def test_every_per_profile_task_is_infrastructure_or_gated(self):
        src = _multi_scheduler_src()
        cols = _profile_columns()
        html = _settings_html()
        violations = []
        gate_only_in_schema_not_html = []
        gate_missing_from_schema = []

        for gate, task_name, line_no in _task_call_blocks(src):
            if task_name in INFRASTRUCTURE_TASKS:
                continue
            if gate is None:
                violations.append(
                    f"  {task_name}  (multi_scheduler.py:{line_no}) "
                    f"runs unconditionally — no `if getattr(ctx, "
                    f"'enable_*', ...)` gate found in surrounding "
                    f"lines. Either add the task to "
                    f"INFRASTRUCTURE_TASKS in this test (with a "
                    f"rationale) or add an `enable_X` toggle."
                )
                continue
            # We have a gate — confirm the column exists + is on
            # settings.html with a form input.
            if gate not in cols:
                gate_missing_from_schema.append(
                    f"  {task_name} gated on `{gate}` but the column "
                    f"isn't on trading_profiles schema."
                )
            if f'name="{gate}"' not in html:
                gate_only_in_schema_not_html.append(
                    f"  {task_name} gated on `{gate}` but settings.html "
                    f"has no `<input name='{gate}'>` control — users "
                    f"can't toggle it through the UI."
                )

        msgs = []
        if violations:
            msgs.append(
                "Per-profile scheduled tasks running with NO gate:\n"
                + "\n".join(violations)
            )
        if gate_missing_from_schema:
            msgs.append(
                "Toggles referenced by scheduler but missing from "
                "schema:\n" + "\n".join(gate_missing_from_schema)
            )
        if gate_only_in_schema_not_html:
            msgs.append(
                "Toggles in schema but missing from settings.html:\n"
                + "\n".join(gate_only_in_schema_not_html)
            )

        if msgs:
            pytest.fail("\n\n".join(msgs))
