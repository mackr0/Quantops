"""Kill-switch flap latch (2026-07-07): repeated integrity trips within
an hour LATCH the switch — auto-release stops, operator clear required.

THE FLAP (2026-07-06, the Alpaca wipe's first day): the integrity gate
kill-switched at 09:38 ET, ONE clean pass auto-released it at ~09:43,
entries resumed against corrupting books, drift re-tripped the switch —
repeatedly. Each release window bought positions the overnight wipe
turned into real broker shorts. One clean pass is not evidence that a
progressive broker-side event is over.

Mechanics pinned here:
  • kill_switch.integrity_release_latched — >= 2 integrity_auto
    DEACTIVATIONS inside the trailing 60 min → latched (releases are
    counted, not activations: activation history rows are also written
    on reason changes during ONE sustained halt, which would over-
    count; a deactivate row only exists for a true ON→OFF transition).
    Fail-CLOSED: any error reads as latched.
  • The gate's release branch converts the halt to a
    "Book integrity LATCHED" reason (set_by=integrity_latch) instead of
    releasing. That reason deliberately lacks the "Book integrity
    FAILED" prefix, so the auto-release branch can never touch it again
    — durable until a manual clear from the dashboard kill-switch
    banner, and the
    operator email fires exactly once per episode by construction.
  • New findings while latched leave the latch in place (the findings
    branch treats a non-prefix reason as not-ours and never overwrites).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import kill_switch
from kill_switch import integrity_release_latched


@pytest.fixture
def ks_db(tmp_path):
    path = str(tmp_path / "master.db")
    kill_switch._ensure_table(path)
    return path


def _add_history(db_path, action, set_by, minutes_ago):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO kill_switch_history (action, reason, set_by, set_at) "
        "VALUES (?, '', ?, datetime('now', ?))",
        (action, set_by, f"-{minutes_ago} minutes"))
    conn.commit()
    conn.close()


# ------------------------------------------------ latch predicate

def test_zero_or_one_release_does_not_latch(ks_db):
    assert integrity_release_latched(ks_db) is False
    _add_history(ks_db, "deactivate", "integrity_auto", 5)
    assert integrity_release_latched(ks_db) is False


def test_two_releases_in_window_latch(ks_db):
    # the Monday shape: release ~09:43, re-trip, release again, re-trip
    # — the THIRD release decision must latch instead
    _add_history(ks_db, "deactivate", "integrity_auto", 20)
    _add_history(ks_db, "deactivate", "integrity_auto", 5)
    assert integrity_release_latched(ks_db) is True


def test_releases_outside_window_ignored(ks_db):
    _add_history(ks_db, "deactivate", "integrity_auto", 120)
    _add_history(ks_db, "deactivate", "integrity_auto", 90)
    _add_history(ks_db, "deactivate", "integrity_auto", 5)
    assert integrity_release_latched(ks_db) is False


def test_manual_and_settings_releases_do_not_count(ks_db):
    # only the AUTO-release/re-trip cycle is a flap; an operator
    # clearing and re-arming is deliberate action
    _add_history(ks_db, "deactivate", "manual", 10)
    _add_history(ks_db, "deactivate", "settings_ui", 8)
    _add_history(ks_db, "deactivate", "integrity_auto", 5)
    assert integrity_release_latched(ks_db) is False


def test_manual_clear_fences_the_flap_window(ks_db):
    # Round-1 review M1: after an operator clears a latched flap by
    # hand, the residue of the adjudicated episode must not insta-
    # latch the NEXT episode's first release. Only auto-releases
    # AFTER the most recent manual deactivation count.
    _add_history(ks_db, "deactivate", "integrity_auto", 40)
    _add_history(ks_db, "deactivate", "integrity_auto", 30)
    assert integrity_release_latched(ks_db) is True  # the flap
    _add_history(ks_db, "deactivate", "user:operator", 20)  # manual clear
    assert integrity_release_latched(ks_db) is False  # fresh episode
    # one auto-release into the new episode: still below threshold
    _add_history(ks_db, "deactivate", "integrity_auto", 10)
    assert integrity_release_latched(ks_db) is False
    # two: the new episode is itself flapping — latch again
    _add_history(ks_db, "deactivate", "integrity_auto", 5)
    assert integrity_release_latched(ks_db) is True


def test_activations_are_not_counted(ks_db):
    # re-stamps during ONE sustained halt write activate rows on every
    # reason change — they must never count toward the flap
    for m in (30, 25, 20, 15, 10):
        _add_history(ks_db, "activate", "integrity_auto", m)
    assert integrity_release_latched(ks_db) is False


def test_db_error_fails_closed_latched(ks_db, monkeypatch):
    def _boom(_db=None):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(kill_switch, "_conn", _boom)
    assert integrity_release_latched(ks_db) is True


# ------------------------------------------------ gate release branch

class _KS:
    """Records the gate's kill_switch calls."""

    def __init__(self, active_reason, latched):
        self.active_reason = active_reason
        self.latched = latched
        self.activated = []
        self.deactivated = []

    def is_active(self, db_path=None):
        return (bool(self.active_reason), self.active_reason or "")

    def activate(self, reason, set_by="manual", db_path=None):
        self.activated.append((reason, set_by))
        self.active_reason = reason
        return True

    def deactivate(self, set_by="manual", db_path=None):
        self.deactivated.append(set_by)
        self.active_reason = ""
        return True

    def integrity_release_latched(self, db_path=None):
        return self.latched


@pytest.fixture
def gate(monkeypatch):
    """Run _run_integrity_gate with clean books and a scripted
    kill-switch state; returns (run, ks_holder, emails)."""
    import multi_scheduler as ms
    emails = []

    def _run(active_reason, latched, findings=None):
        ks = _KS(active_reason, latched)
        monkeypatch.setattr(kill_switch, "is_active", ks.is_active)
        monkeypatch.setattr(kill_switch, "activate", ks.activate)
        monkeypatch.setattr(kill_switch, "deactivate", ks.deactivate)
        monkeypatch.setattr(kill_switch, "integrity_release_latched",
                            ks.integrity_release_latched)
        monkeypatch.setattr(ms, "_collect_integrity_findings",
                            lambda: list(findings or []))
        monkeypatch.setattr(ms, "_heal_pending_fills_best_effort",
                            lambda: None)
        import notifications
        monkeypatch.setattr(notifications, "send_email",
                            lambda subj, body: emails.append(subj))
        ms._integrity_findings_active = False
        result = ms._run_integrity_gate()
        return result, ks

    return _run, emails


def test_unlatched_clean_pass_still_releases(gate):
    run, emails = gate
    result, ks = run("Book integrity FAILED — 1 finding(s): x",
                     latched=False)
    assert result is True
    assert ks.deactivated == ["integrity_auto"]
    assert ks.activated == []


def test_flap_converts_to_latched_instead_of_releasing(gate):
    run, emails = gate
    result, ks = run("Book integrity FAILED — 2 finding(s): y",
                     latched=True)
    assert result is True          # books ARE clean — reported honestly
    assert ks.deactivated == []    # but the switch was NOT released
    assert len(ks.activated) == 1
    reason, set_by = ks.activated[0]
    assert reason.startswith("Book integrity LATCHED")
    assert set_by == "integrity_latch"
    # the latched reason must NOT carry the FAILED prefix, or the
    # release branch would auto-lift it on the next clean pass
    assert not reason.startswith("Book integrity FAILED")
    assert emails and "LATCHED" in emails[0]


def test_latched_reason_is_never_auto_released(gate):
    # next clean pass with the LATCHED reason active: the release
    # branch doesn't even recognize it as ours — durable by design
    run, emails = gate
    result, ks = run("Book integrity LATCHED — repeated drift trips",
                     latched=True)
    assert result is True
    assert ks.deactivated == [] and ks.activated == []
    assert emails == []  # email fired at conversion only, never again


def test_new_findings_while_latched_leave_latch_in_place(gate):
    run, emails = gate
    result, ks = run("Book integrity LATCHED — repeated drift trips",
                     latched=True,
                     findings=["account 52 XYZ: broker=0 virtual=10"])
    assert result is False          # entries stay blocked
    assert ks.activated == []       # never overwrite a non-FAILED reason
    assert ks.deactivated == []


def test_manual_clear_works_while_latched(ks_db):
    # the latch governs only the AUTO-release path — a manual clear
    # (the dashboard kill-switch banner / admin API) must always work
    kill_switch.activate("Book integrity LATCHED — flap", "integrity_latch",
                         db_path=ks_db)
    assert kill_switch.is_active(ks_db)[0] is True
    kill_switch.deactivate(set_by="settings_ui", db_path=ks_db)
    assert kill_switch.is_active(ks_db)[0] is False


# ------------------------------------------------ structural pin

def test_release_branch_consults_latch_before_deactivate():
    src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                            "multi_scheduler.py")).read()
    gate_src = src.split("def _run_integrity_gate(")[1].split("\ndef ")[0]
    latch = gate_src.find("integrity_release_latched")
    release = gate_src.find('deactivate(set_by="integrity_auto")')
    assert 0 < latch < release, (
        "the auto-release must consult the flap latch first — one "
        "clean pass is not evidence a progressive broker-side event "
        "is over (2026-07-06: each auto-release window bought "
        "positions the wipe turned into real shorts)")
