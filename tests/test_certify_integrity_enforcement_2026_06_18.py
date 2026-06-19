"""2026-06-18 — THE book-integrity gate. certify detects phantom equity /
broker drift perfectly, but the old `audit_runner` ran it AFTER trading,
on its own 10-min interval, only emailed (never halted), and audited the
wrong profile range (1-11, not the experiment's 145-154). So the UWMC
oversell phantom (10 profiles, ~$187K) accumulated for ~a day before a
human caught it by eye.

`_run_integrity_gate()` replaces it: runs BEFORE entries EVERY trading
cycle, and AUTO-ENGAGES the kill switch on any finding so the entries
that cycle are blocked (exits still run). One gate, in-line with trading
— a problem halts on the same cycle it appears.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def patched(monkeypatch):
    """Patch the global checks + kill-switch + email the gate imports.
    Returns (multi_scheduler, state) where state records kill-switch
    calls and lets a test set the check results / current halt state."""
    import multi_scheduler
    import certify_books
    import kill_switch
    import notifications

    # Reset the module-level first-detection email dedup so each test
    # starts from a clean "no findings seen yet" state.
    multi_scheduler._integrity_findings_active = False
    state = {"drift": [], "decomp": [], "is_active": (False, ""),
             "calls": {}, "emails": 0}

    monkeypatch.setattr(certify_books, "check_broker_drift",
                        lambda: list(state["drift"]))
    monkeypatch.setattr(certify_books, "check_decomposition",
                        lambda tolerance=100.0: list(state["decomp"]))
    monkeypatch.setattr(kill_switch, "is_active",
                        lambda db_path=None: state["is_active"])
    monkeypatch.setattr(
        kill_switch, "activate",
        lambda reason, set_by="manual", db_path=None:
        state["calls"].update(activate=(reason, set_by)) or True)
    monkeypatch.setattr(
        kill_switch, "deactivate",
        lambda set_by="manual", db_path=None:
        state["calls"].update(deactivate=set_by) or True)
    monkeypatch.setattr(
        notifications, "send_email",
        lambda *a, **k: state.update(emails=state["emails"] + 1) or True)
    return multi_scheduler, state


def test_halts_and_returns_false_on_broker_drift(patched):
    sched, st = patched
    st["drift"] = ["account 42 UWMC: broker=-41,907 virtual=0 drift=-41,907"]
    ok = sched._run_integrity_gate()
    assert ok is False
    reason, set_by = st["calls"]["activate"]
    assert set_by == "integrity_auto"
    assert "UWMC" in reason and "FAILED" in reason
    assert st["emails"] == 1, "operator emailed on the first halt transition"


def test_halts_on_decomposition_gap(patched):
    sched, st = patched
    st["decomp"] = ["p154: equity=734,096 P&L=+34,096 decomposition gap=+45,014"]
    assert sched._run_integrity_gate() is False
    assert st["calls"].get("activate")


def test_clean_books_pass_no_halt(patched):
    sched, st = patched
    assert sched._run_integrity_gate() is True
    assert "activate" not in st["calls"]
    assert "deactivate" not in st["calls"]
    assert st["emails"] == 0


def test_emails_once_across_consecutive_failing_cycles(patched):
    sched, st = patched
    st["drift"] = ["some drift"]
    sched._run_integrity_gate()
    sched._run_integrity_gate()   # same failure, next cycle
    sched._run_integrity_gate()
    assert st["emails"] == 1, "operator emailed once per failure, not per cycle"


def test_surfaces_integrity_failure_even_under_a_manual_halt(patched):
    # #7 — a finding stacked on a pre-existing manual halt must still be
    # surfaced (the manual reason masks ours on the dashboard).
    sched, st = patched
    st["drift"] = ["account 42 UWMC drift"]
    st["is_active"] = (True, "manual halt: operator paused trading")
    assert sched._run_integrity_gate() is False
    assert st["emails"] == 1, "integrity failure surfaced despite prior halt"
    # ...and it must NOT overwrite the manual halt's provenance.
    assert "activate" not in st["calls"], (
        "must not clobber a manual halt's reason/set_by")


def test_auto_releases_only_its_own_halt(patched):
    sched, st = patched
    st["is_active"] = (True, "Book integrity FAILED — 1 finding(s): ...")
    assert sched._run_integrity_gate() is True  # books now clean
    assert st["calls"].get("deactivate") == "integrity_auto"


def test_never_releases_a_manual_halt(patched):
    sched, st = patched
    st["is_active"] = (True, "manual halt: operator paused trading")
    sched._run_integrity_gate()  # books clean
    assert "deactivate" not in st["calls"]


def test_manual_halt_survives_a_finding_then_clean_cycle(patched):
    # #6 regression — the gate must never release a human's kill switch.
    # A finding while a manual halt is up must NOT re-stamp the reason as
    # an integrity halt (which the next clean cycle would then release).
    sched, st = patched
    st["is_active"] = (True, "manual halt: operator paused trading")
    st["drift"] = ["account 42 UWMC drift"]
    sched._run_integrity_gate()                 # finding cycle
    assert "activate" not in st["calls"]        # manual reason untouched
    st["drift"] = []                            # books clean next cycle
    sched._run_integrity_gate()
    assert "deactivate" not in st["calls"], (
        "a manual halt must survive an integrity finding + clean cycle")


# ── structural pins ────────────────────────────────────────────────────

def test_gate_runs_before_entries_every_cycle():
    sched = (REPO / "multi_scheduler.py").read_text()
    assert "def _run_integrity_gate" in sched
    # must be CALLED inside the due-profiles branch, before the trading
    # ThreadPoolExecutor — not on a side schedule.
    idx_call = sched.find("_run_integrity_gate()")
    idx_pool = sched.find("ThreadPoolExecutor(max_workers")
    assert 0 < idx_call < idx_pool, (
        "_run_integrity_gate() must run BEFORE the per-profile trading "
        "pool so a finding halts entries on the same cycle.")
    # the slower email-only side audit must be gone.
    assert "detect_and_alert_new_drift()" not in sched, (
        "the separate audit_runner cadence must be removed — one gate.")


def test_entries_gate_on_kill_switch():
    tp = (REPO / "trade_pipeline.py").read_text()
    assert "from kill_switch import is_active" in tp
