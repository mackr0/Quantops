"""Position-cap manifest contract + reset drift report (2026-06-11).

The regression: on 2026-06-06 the operator set max_total_positions
to 999 on all 13 profiles via a runtime DB change that never landed
in create_experiment_profiles.PROFILES. The 2026-06-09 fresh-start
rebuilt profiles from the manifest and silently reverted every cap
to 1/5/10/15; the experiment ran position-capped (blocking trades)
until 2026-06-11.

Contract pinned here (operator-stated 2026-06-11):
  * EXP-A1-BuyHoldSPY keeps max_total_positions=1 for the life of
    the experiment.
  * The two Random profiles keep max_total_positions=5.
  * Every AI-driven profile (strategy_type='ai') has
    max_total_positions=999 — effectively uncapped; the AI decides.

Class fix pinned here: clean_orphaned_profiles (the destroy step of
every fresh-start) prints a LOUD manifest-drift report BEFORE
destroying anything, so runtime settings that diverge from the
manifest are either folded in deliberately or consciously dropped —
never silently reverted again.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# (1) Manifest values
# ---------------------------------------------------------------------------

def test_manifest_caps_match_operator_contract():
    from create_experiment_profiles import PROFILES
    by_name = {p["name"]: p for p in PROFILES}
    assert by_name["EXP-A1-BuyHoldSPY"]["max_total_positions"] == 1
    assert by_name["EXP-A1-RandomA"]["max_total_positions"] == 5
    assert by_name["EXP-A1-RandomB"]["max_total_positions"] == 5
    ai_profiles = [p for p in PROFILES if p.get("strategy_type") == "ai"]
    assert len(ai_profiles) == 10, (
        f"Expected 10 AI-driven profiles in the manifest, found "
        f"{len(ai_profiles)} — update this test alongside any "
        "experiment redesign."
    )
    wrong = [(p["name"], p["max_total_positions"])
             for p in ai_profiles if p["max_total_positions"] != 999]
    assert not wrong, (
        f"AI-driven profiles must have max_total_positions=999 "
        f"(operator contract 2026-06-11: the AI decides position "
        f"count; capping blocks trades). Violations: {wrong}"
    )


# ---------------------------------------------------------------------------
# (2) Drift report behavior
# ---------------------------------------------------------------------------

def _db_with_profile(tmp_path, name, **overrides):
    from create_experiment_profiles import PROFILES
    manifest = next(p for p in PROFILES if p["name"] == name)
    db = str(tmp_path / "main.db")
    fields = {k: v for k, v in manifest.items()}
    fields.update(overrides)
    cols = ", ".join(f"{k}" for k in fields)
    with closing(sqlite3.connect(db)) as conn:
        col_defs = ", ".join(f"{k}" for k in fields)
        conn.execute(
            f"CREATE TABLE trading_profiles "
            f"(id INTEGER PRIMARY KEY, enabled INTEGER, {col_defs})"
        )
        conn.execute(
            f"INSERT INTO trading_profiles (enabled, {cols}) "
            f"VALUES (1, {', '.join('?' for _ in fields)})",
            list(fields.values()),
        )
        conn.commit()
    return db


def test_drift_report_flags_diverged_setting(tmp_path):
    from clean_orphaned_profiles import _report_manifest_drift
    db = _db_with_profile(
        tmp_path, "EXP-A1-FullSystemStandard", max_total_positions=10,
    )
    n = _report_manifest_drift(db)
    assert n >= 1, (
        "A live max_total_positions diverging from the manifest must "
        "be reported — silent reversion is the 999-regression class."
    )


def test_drift_report_quiet_when_matching(tmp_path):
    from clean_orphaned_profiles import _report_manifest_drift
    db = _db_with_profile(tmp_path, "EXP-A1-FullSystemStandard")
    assert _report_manifest_drift(db) == 0


# ---------------------------------------------------------------------------
# (3) Wiring: report runs in the destroy path, before destruction
# ---------------------------------------------------------------------------

def test_destroy_path_runs_drift_report_first():
    src = (REPO / "clean_orphaned_profiles.py").read_text()
    main_idx = src.index("def main()")
    report_idx = src.index("_report_manifest_drift(main_db)", main_idx)
    destroy_idx = src.index("_find_orphans(", main_idx)
    assert report_idx < destroy_idx, (
        "The manifest-drift report must run BEFORE orphan discovery/"
        "deletion — reporting after the destroy defeats the purpose."
    )
