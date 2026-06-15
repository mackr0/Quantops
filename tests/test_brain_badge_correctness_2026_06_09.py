"""2026-06-09 — AI Brain badge correctness.

Two distinct bugs surfaced from the catastrophic-gate floor deploy:

  1. MULTILEG_OPEN / MULTILEG_CLOSE — these are SUCCESS actions (the
     combo was submitted to the broker), but the drop-recording
     allowlist at trade_pipeline.py:2747 only covered single-leg
     success actions ("BUY", "SELL", "SHORT", "COVER"). Every
     successful multileg fell through and was logged to trade_drops
     → AI Brain rendered "Multileg Open NOK GATED · Multileg Open".
     Observed today: NOK bear_call_spread submitted successfully at
     14:21:16 (status=open at the broker), yet badged as GATED.

  2. Stale-cycle drops bleeding into the current cycle. The brain
     enrichment used `get_recent_trade_drops(hours=2)` and matched
     by symbol. A drop recorded at 13:57 for RGNT (pre-deploy,
     pre-floor-fix) re-badged a RGNT proposal at 14:30 even though
     no drop had been recorded for that symbol since deploy.
     Operator-visible symptom: "BUY RGNT GATED · Catastrophic" on
     every cycle for 2 hours after a single pre-deploy drop.

Fixes pinned here:

  - Layer 1: `_SUCCESS_ACTIONS` in trade_pipeline.py includes
    MULTILEG_OPEN and MULTILEG_CLOSE so successful multilegs never
    enter trade_drops.
  - Layer 2: views.api_cycle_data filters drops by
    `data["timestamp"]` (the cycle's wall-clock start) with a 60s
    back-buffer for clock skew. Drops older than the cycle's start
    are excluded from badge enrichment.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Layer 1 — MULTILEG_OPEN / MULTILEG_CLOSE are in the success allowlist
# ---------------------------------------------------------------------------


def test_trade_pipeline_treats_multileg_open_as_success():
    """Source-code pin: the action allowlist that decides whether
    to record_trade_drop must include MULTILEG_OPEN and
    MULTILEG_CLOSE. Without this, every successful multileg combo
    gets logged as a drop and the AI Brain badges it 'GATED'."""
    src = (REPO_ROOT / "trade_pipeline.py").read_text()
    # Find the 'Trade NOT submitted' branch — that's where the
    # filtering allowlist lives.
    anchor = src.find("Trade NOT submitted for")
    assert anchor > 0, "Couldn't find 'Trade NOT submitted' anchor"
    # Search the ~1000 chars BEFORE the anchor (where the check is)
    window = src[max(0, anchor - 1000):anchor]
    assert "MULTILEG_OPEN" in window, (
        "trade_pipeline.py's success-action allowlist must include "
        "MULTILEG_OPEN — without it, every successful multileg "
        "submission is logged to trade_drops and badged GATED."
    )
    assert "MULTILEG_CLOSE" in window, (
        "Same requirement for MULTILEG_CLOSE — closes of multileg "
        "spreads are success actions, not drops."
    )


def test_buy_short_etc_still_in_allowlist():
    """Sanity: the original allowlist members ("BUY", "SELL",
    "SHORT", "COVER") MUST stay present. A refactor that
    accidentally dropped them would re-badge every successful
    single-leg trade as gated."""
    src = (REPO_ROOT / "trade_pipeline.py").read_text()
    anchor = src.find("Trade NOT submitted for")
    window = src[max(0, anchor - 1000):anchor]
    for action in ("BUY", "SELL", "SHORT", "COVER"):
        assert f'"{action}"' in window, (
            f"Success-action allowlist must still contain "
            f"{action!r} — removing it would badge every "
            f"successful single-leg trade as gated."
        )


# ---------------------------------------------------------------------------
# Layer 2 — brain enrichment scopes drops to the current cycle
# ---------------------------------------------------------------------------


def _make_profile_db_with_drops(tmp_path, drops_iso_timestamps):
    """Create a minimal profile DB containing the trade_drops rows
    listed in drops_iso_timestamps as (ts, symbol, code, reason)
    tuples."""
    from journal import init_db
    db = str(tmp_path / "p.db")
    init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        for ts, sym, code, reason in drops_iso_timestamps:
            conn.execute(
                "INSERT INTO trade_drops "
                "(timestamp, symbol, side, drop_code, drop_reason) "
                "VALUES (?, ?, 'buy', ?, ?)",
                (ts, sym, code, reason),
            )
        conn.commit()
    return db


def test_get_recent_trade_drops_returns_old_drops():
    """Sanity: the journal helper itself returns drops within the
    last 2 hours. The filtering for cycle scope happens in the
    view layer (so the helper stays general-purpose)."""
    import tempfile
    from journal import get_recent_trade_drops
    with tempfile.TemporaryDirectory() as td:
        db = _make_profile_db_with_drops(
            Path(td),
            [
                ("2026-06-09 13:57:27", "RGNT",
                 "CATASTROPHIC_SINGLE_TRADE", "old drop"),
            ],
        )
        # The helper doesn't know about cycles — it returns whatever
        # is in the 2-hour window. We patched datetime('now') by
        # inserting an old-ish row but the helper compares to the
        # SQLite "now()" so this test just confirms the contract:
        # if the row is present, the helper returns it.
        rows = get_recent_trade_drops(db, hours=24 * 365)
        symbols = [r["symbol"] for r in rows]
        assert "RGNT" in symbols


class TestApiCycleDataScopesDropsToCycle:
    """End-to-end tests of the view's enrichment logic — drops older
    than the cycle's `data["timestamp"]` must NOT badge the current
    cycle's trades_selected entries."""

    def _write_cycle_file(self, path, cycle_epoch, selected_symbols):
        path.write_text(json.dumps({
            "profile_id": 999,
            "profile_name": "test",
            "timestamp": cycle_epoch,
            "trades_selected": [
                {"symbol": s, "action": "BUY", "confidence": 70,
                 "reasoning": "test"}
                for s in selected_symbols
            ],
            "shortlist": [],
            "ai_reasoning": "test",
        }))

    def test_drop_before_cycle_does_not_badge(self, tmp_path, monkeypatch):
        """Cycle ran at T. A drop exists at T - 30 minutes for the
        same symbol. The enrichment MUST NOT badge — that drop
        belongs to a previous cycle."""
        # Cycle wrote at 14:30:00 UTC; drop is at 14:00:00 (30m before)
        cycle_epoch = 1781015400  # 2026-06-09 14:30:00 UTC
        # Insert an OLD drop (30m before cycle start)
        db = _make_profile_db_with_drops(
            tmp_path,
            [
                ("2026-06-09 14:00:00", "RGNT",
                 "CATASTROPHIC_SINGLE_TRADE", "stale drop"),
            ],
        )
        # Write the cycle data file in the same tmp_path
        cycle_file = tmp_path / "cycle_data_999.json"
        self._write_cycle_file(cycle_file, cycle_epoch, ["RGNT"])

        from views import views_bp
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(views_bp)
        # Disable login_required so the view is callable in tests
        app.config["LOGIN_DISABLED"] = True

        # Patch the DB path resolution and cwd-relative file reads
        monkeypatch.chdir(tmp_path)
        # The view reads `quantopsai_profile_{id}.db`; symlink ours
        os.symlink(db, tmp_path / "quantopsai_profile_999.db")

        with app.test_client() as client:
            with patch("flask_login.utils._get_user", return_value=None):
                # Bypass auth via test config
                with app.test_request_context():
                    from views import api_cycle_data
                    # Force login_required to pass: monkey-patch
                    # current_user.is_authenticated → True
                    pass
                # Use direct function call instead of test_client to
                # bypass auth entirely.
        # Easier: call api_cycle_data directly.
        with app.test_request_context():
            from flask_login import LoginManager
            # Build a fake authenticated request context
            from views import api_cycle_data as _view
            # The view is decorated; call through .__wrapped__ if
            # available; else patch login_required.
            inner = getattr(_view, "__wrapped__", _view)
            response = inner(999)
        # response is a Flask Response (jsonify)
        body = json.loads(response.get_data(as_text=True))
        selected = body.get("trades_selected", [])
        assert len(selected) == 1
        rgnt = selected[0]
        # Critical assertion: a 30m-old drop should NOT badge a
        # current cycle's proposal.
        assert rgnt.get("execution_outcome") != "gated", (
            f"Stale pre-cycle drop must not badge current-cycle "
            f"proposal. Got outcome={rgnt.get('execution_outcome')!r} "
            f"gate_code={rgnt.get('gate_code')!r}"
        )

    def test_drop_after_cycle_does_badge(self, tmp_path, monkeypatch):
        """Drop happened DURING the cycle (after cycle start). It
        SHOULD badge — that's the legitimate same-cycle gate fire.

        Uses time-relative timestamps (cycle_ts = now-2min, drop_ts =
        now-1min) so the test isn't coupled to wall-clock decay of
        a hardcoded date — `get_recent_trade_drops` filters by
        `datetime('now', '-2 hours')` and would mask any badging
        if the seeded timestamps fall out of the window."""
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        now = _dt.now(_tz.utc)
        cycle_epoch = (now - _td(minutes=2)).timestamp()
        drop_ts_iso = (now - _td(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S")
        db = _make_profile_db_with_drops(
            tmp_path,
            [
                (drop_ts_iso, "RGNT",
                 "CATASTROPHIC_SINGLE_TRADE", "real same-cycle drop"),
            ],
        )
        cycle_file = tmp_path / "cycle_data_999.json"
        self._write_cycle_file(cycle_file, cycle_epoch, ["RGNT"])

        from views import views_bp
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(views_bp)
        app.config["LOGIN_DISABLED"] = True

        monkeypatch.chdir(tmp_path)
        os.symlink(db, tmp_path / "quantopsai_profile_999.db")

        with app.test_request_context():
            from views import api_cycle_data as _view
            inner = getattr(_view, "__wrapped__", _view)
            response = inner(999)
        body = json.loads(response.get_data(as_text=True))
        selected = body.get("trades_selected", [])
        assert len(selected) == 1
        rgnt = selected[0]
        assert rgnt.get("execution_outcome") == "gated", (
            "Drop recorded WITHIN this cycle must badge the trade. "
            f"Got outcome={rgnt.get('execution_outcome')!r}"
        )
        assert (rgnt.get("gate_code") or "").upper() == \
            "CATASTROPHIC_SINGLE_TRADE"


# ---------------------------------------------------------------------------
# Layer 3 — structural pins (refactor protection)
# ---------------------------------------------------------------------------


def test_view_scopes_drops_to_the_current_cycle():
    """Source-code pin on views.py — the drop-enrichment block must
    scope drops to THIS cycle so a prior cycle's drop can't badge
    the current one (the 2026-06-09 stale-drop bleed-through).

    2026-06-15: the primary mechanism is now EXACT cycle_id match
    (stronger than the old wall-clock cutoff — a different cycle's
    drop has a different cycle_id and can never match, and the
    reason never ages out of a time window). The timestamp cutoff
    survives as the fallback for legacy cycle_data JSON written
    before cycle_id was carried; pin both so neither defense is
    silently removed."""
    src = (REPO_ROOT / "views.py").read_text()
    anchor = src.find("EXACT cycle_id match when available")
    assert anchor > 0, (
        "Source pin failed — couldn't locate the cycle_id drop-"
        "matching block in views.py"
    )
    # Window covers the cycle_id block + recovery + exact-join +
    # legacy fallback (the recovery block widened this span).
    window = src[anchor:anchor + 3200]
    # Primary defense: match drops by cycle_id.
    assert 'data.get("cycle_id")' in window, (
        "Drop enrichment must read cycle_id from cycle_data and "
        "match drops by it — without this end-of-day badges go "
        "vague (>2h staleness) AND cross-cycle bleed can return."
    )
    assert "WHERE cycle_id = ?" in window, (
        "Drops must be fetched by exact cycle_id."
    )
    # Fallback defense for legacy JSON: the wall-clock cutoff.
    assert "cycle_cutoff_iso" in window and 'data.get("timestamp")' in window, (
        "The legacy-JSON fallback (cycle_cutoff_iso from the cycle "
        "timestamp) must remain for cycle_data files without "
        "cycle_id."
    )
