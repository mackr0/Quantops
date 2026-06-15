"""AI-Brain BLOCKED badge shows the real reason, not the vague
catch-all (2026-06-15).

Operator saw end-of-day badges read "Not submitted — most likely
already-positioned dedup, pre-broker safety gate, or post-AI
meta-model suppression. No trades row was created." even though
every blocked trade had a SPECIFIC recorded drop (outside
market-hours / insufficient cash / dedup). Cause: the live endpoint
matched drops within a 2-HOUR window; after market close the last
cycle's drops aged out and the badge fell through to the guess.

Fixes pinned:
  1. cycle_data carries cycle_id.
  2. The live /api/cycle-data endpoint matches drops by cycle_id
     (exact, never ages out) — a drop timestamped 5 hours ago but
     sharing the cycle_id still badges the trade with its real
     reason.
  3. Meta-model suppression records a META_SUPPRESSED drop (was the
     one genuinely silent skip path).
  4. META_SUPPRESSED is cross-cutting so it always matches.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def logged_in(tmp_main_db, tmp_path, monkeypatch):
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
    import journal
    journal.init_db(str(tmp_path / f"quantopsai_profile_{pid}.db"))
    client = app.test_client()
    client.post("/login", data={"email": "t@t.com", "password": "password123"},
                follow_redirects=True)
    return client, pid, str(tmp_path)


def test_stale_drop_still_badges_via_cycle_id(logged_in):
    client, pid, cwd = logged_in
    import time
    # Live cycle_data JSON with cycle_id + a selected BUY that didn't fill.
    cycle_id = "cyc-eod-1"
    cycle = {
        "profile_id": pid, "timestamp": time.time(),
        "cycle_id": cycle_id,
        "ai_reasoning": "end of day", "shortlist": [],
        "trades_selected": [
            {"symbol": "ACHR", "action": "BUY", "size_pct": 5,
             "confidence": 60, "reasoning": "x"}],
    }
    with open(Path(cwd) / f"cycle_data_{pid}.json", "w") as f:
        json.dump(cycle, f)
    # Drop for that cycle, timestamped 5 HOURS ago (well outside the
    # old 2h window) — must still badge because cycle_id matches.
    with closing(sqlite3.connect(
            Path(cwd) / f"quantopsai_profile_{pid}.db")) as c:
        c.execute(
            "INSERT INTO trade_drops (timestamp, symbol, side, "
            " drop_code, drop_reason, cycle_id) VALUES "
            " (datetime('now','-5 hours'), 'ACHR', 'buy', 'SKIP', "
            " 'Order blocked: outside market_hours window', ?)",
            (cycle_id,))
        c.commit()
    resp = client.get(f"/api/cycle-data/{pid}").get_json()
    t = resp["trades_selected"][0]
    assert t.get("execution_outcome") == "gated", (
        f"stale-but-same-cycle drop did not badge; outcome="
        f"{t.get('execution_outcome')} (vague catch-all regression)"
    )
    assert "market_hours" in (t.get("gate_message") or ""), (
        "badge did not carry the specific drop reason"
    )


# ---------------------------------------------------------------------------
# Source / unit pins
# ---------------------------------------------------------------------------

def test_cycle_data_carries_cycle_id():
    src = (REPO / "trade_pipeline.py").read_text()
    block = src[src.index("cycle_data = {"):src.index("cycle_data = {") + 700]
    assert '"cycle_id": cycle_id' in block, (
        "cycle_data no longer carries cycle_id — the live endpoint "
        "loses exact drop matching and badges go vague after 2h."
    )


def test_meta_suppression_records_drop():
    src = (REPO / "trade_pipeline.py").read_text()
    idx = src.index("Meta-model SUPPRESS")
    block = src[idx:idx + 1200]
    assert "record_trade_drop" in block and "META_SUPPRESSED" in block, (
        "Meta-model suppression no longer records a drop — it would "
        "be a genuinely silent (vague) blocker."
    )


def test_meta_suppressed_is_cross_cutting():
    from views import _drop_action_class
    assert _drop_action_class("META_SUPPRESSED", "") == "any"


def test_legacy_json_without_cycle_id_recovers_via_timestamp(logged_in):
    """The end-of-day case the operator actually hit: cycle_data JSON
    written BEFORE cycle_id was carried (cycle_id absent) must still
    badge by recovering the cycle_id from the nearest ai_cycles row,
    even when the drop is hours old."""
    client, pid, cwd = logged_in
    import time
    now = time.time()
    cid = "cyc-legacy-eod"
    # JSON has NO cycle_id (legacy), timestamp = the cycle's time.
    cycle = {
        "profile_id": pid, "timestamp": now,
        "ai_reasoning": "eod", "shortlist": [],
        "trades_selected": [
            {"symbol": "BITO", "action": "BUY", "size_pct": 7.5,
             "confidence": 75, "reasoning": "x"}],
    }
    with open(Path(cwd) / f"cycle_data_{pid}.json", "w") as f:
        json.dump(cycle, f)
    with closing(sqlite3.connect(
            Path(cwd) / f"quantopsai_profile_{pid}.db")) as c:
        # ai_cycles row at ~the same timestamp carries the cycle_id.
        c.execute(
            "INSERT INTO ai_cycles (cycle_id, timestamp, profile_id, "
            " ai_reasoning, n_trades_selected) "
            "VALUES (?, datetime('now'), ?, 'eod', 1)", (cid, pid))
        # Drop is 4 hours old (outside any 2h window) but keyed to cid.
        c.execute(
            "INSERT INTO trade_drops (timestamp, symbol, side, "
            " drop_code, drop_reason, cycle_id) VALUES "
            " (datetime('now','-4 hours'), 'BITO', 'buy', 'SKIP', "
            " 'Insufficient cash remaining this cycle', ?)", (cid,))
        c.commit()
    t = client.get(f"/api/cycle-data/{pid}").get_json()["trades_selected"][0]
    assert t.get("execution_outcome") == "gated", (
        "legacy JSON (no cycle_id) did not recover the cycle_id — the "
        "operator's end-of-day vague-badge case is unfixed"
    )
    assert "cash" in (t.get("gate_message") or "")
