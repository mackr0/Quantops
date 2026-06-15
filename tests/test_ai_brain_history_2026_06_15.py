"""AI-Brain cycle history API (2026-06-15).

Operator can arrow back through the day's AI-Brain cycles (errors,
trades, reasoning) like the activity ticker. Each cycle is appended
to ai_cycles; /api/cycle-history pages them newest-first and stamps
each cycle's errors from trade_drops (joined exactly by cycle_id).

Pins:
  1. ai_cycles persists trades_selected_json (full decision list).
  2. /api/cycle-history pages newest-first, reports total, honors
     offset.
  3. A cycle's drops are stamped onto its selected trades (gated /
     rejected badges) by exact cycle_id.
  4. Pre-history cycles (no trades_selected_json) synthesize error
     entries from drops so the operator still sees them.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest


@pytest.fixture
def logged_in(tmp_main_db, tmp_path, monkeypatch):
    # Relative profile-DB path ("quantopsai_profile_<id>.db") must
    # resolve under a temp cwd so the test doesn't touch repo files.
    monkeypatch.chdir(tmp_path)
    import config
    config.DB_PATH = str(tmp_path / "main.db")
    from models import create_user, create_trading_profile
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    create_user("t@t.com", "password123", "T", is_admin=True)
    with closing(sqlite3.connect(config.DB_PATH)) as c:
        uid = c.execute("SELECT id FROM users WHERE email='t@t.com'").fetchone()[0]
    pid = create_trading_profile(uid, "Hist", "stocks")
    # Build the profile DB with ai_cycles + trade_drops.
    import journal
    pdb = str(tmp_path / f"quantopsai_profile_{pid}.db")
    journal.init_db(pdb)
    with closing(sqlite3.connect(pdb)) as c:
        # Two recorded cycles (newest c2), one with a drop.
        c.execute(
            "INSERT INTO ai_cycles (cycle_id, timestamp, profile_id, "
            " ai_reasoning, n_trades_selected, trades_selected_json) "
            "VALUES (?,?,?,?,?,?)",
            ("c1", "2026-06-15 14:00:00", pid, "older cycle", 1,
             json.dumps([{"symbol": "AAPL", "action": "BUY",
                          "confidence": 70, "reasoning": "x"}])))
        c.execute(
            "INSERT INTO ai_cycles (cycle_id, timestamp, profile_id, "
            " ai_reasoning, n_trades_selected, trades_selected_json) "
            "VALUES (?,?,?,?,?,?)",
            ("c2", "2026-06-15 14:15:00", pid, "newest cycle", 1,
             json.dumps([{"symbol": "BMNR", "action": "BUY",
                          "confidence": 80, "reasoning": "y"}])))
        # c2's BMNR errored — drop tagged with cycle_id=c2.
        c.execute(
            "INSERT INTO trade_drops (timestamp, symbol, side, "
            " drop_code, drop_reason, cycle_id) VALUES (?,?,?,?,?,?)",
            ("2026-06-15 14:15:01", "BMNR", "buy", "ERROR",
             "asset not found", "c2"))
        # A pre-history cycle: no trades_selected_json, only a drop.
        c.execute(
            "INSERT INTO ai_cycles (cycle_id, timestamp, profile_id, "
            " ai_reasoning, n_trades_selected) VALUES (?,?,?,?,?)",
            ("c0", "2026-06-15 13:45:00", pid, "ancient cycle", 0))
        c.execute(
            "INSERT INTO trade_drops (timestamp, symbol, side, "
            " drop_code, drop_reason, cycle_id) VALUES (?,?,?,?,?,?)",
            ("2026-06-15 13:45:01", "NVDA", "buy", "GATED",
             "book concentration cap", "c0"))
        c.commit()
    client = app.test_client()
    client.post("/login", data={"email": "t@t.com",
                                "password": "password123"},
                follow_redirects=True)
    return client, pid


def test_history_paginates_newest_first(logged_in):
    client, pid = logged_in
    r0 = client.get(f"/api/cycle-history/{pid}?offset=0").get_json()
    assert r0["total"] == 3
    e0 = r0["entries"][0]
    assert e0["cycle_id"] == "c2", "offset 0 must be the newest cycle"
    # c2's BMNR carries the ERROR drop as a 'rejected' badge.
    bmnr = [t for t in e0["trades_selected"] if t["symbol"] == "BMNR"][0]
    assert bmnr["execution_outcome"] == "rejected"
    assert "not found" in bmnr["rejection_message"]


def test_history_offset_walks_back(logged_in):
    client, pid = logged_in
    e1 = client.get(f"/api/cycle-history/{pid}?offset=1").get_json()["entries"][0]
    assert e1["cycle_id"] == "c1"
    e2 = client.get(f"/api/cycle-history/{pid}?offset=2").get_json()["entries"][0]
    assert e2["cycle_id"] == "c0"


def test_prehistory_cycle_synthesizes_errors(logged_in):
    client, pid = logged_in
    e2 = client.get(f"/api/cycle-history/{pid}?offset=2").get_json()["entries"][0]
    assert e2["decisions_recorded"] is False
    # The NVDA gate drop is surfaced as a synthesized gated entry.
    nvda = [t for t in e2["trades_selected"] if t["symbol"] == "NVDA"]
    assert nvda and nvda[0]["execution_outcome"] == "gated"
    assert "concentration" in nvda[0]["gate_message"]


def test_history_past_end_is_empty(logged_in):
    client, pid = logged_in
    r = client.get(f"/api/cycle-history/{pid}?offset=9").get_json()
    assert r["entries"] == [] and r["total"] == 3


# ---------------------------------------------------------------------------
# Static wiring pins (the JS can't be exercised by the test client)
# ---------------------------------------------------------------------------

def test_dashboard_wires_history_controls():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "templates/dashboard.html").read_text()
    assert "brain-older" in src and "brain-newer" in src, (
        "AI-Brain history arrows missing from the panel header."
    )
    assert "/api/cycle-history/" in src, (
        "Dashboard no longer calls the cycle-history endpoint."
    )
    assert "function renderBrain(pid, data)" in src, (
        "Shared renderBrain refactor gone — live and history would "
        "diverge."
    )
    # Auto-refresh must not yank the operator off a historical cycle.
    assert "if ((brainIndex[pid] || 0) === 0) fetchLive(pid)" in src
