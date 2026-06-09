"""2026-06-09 — pre-broker gate visibility on the AI Brain panel.

Before this fix: the doomsday gates in trade_pipeline (Catastrophic
Single Trade, Book Concentration Cap, Kill Switch, Drawdown Pause,
Broker Disconnect, etc.) wrote a journalctl WARNING line and
returned a non-execution action — but nothing surfaced on the
dashboard. Operator saw a 'BLOCKED' badge with no context: which
rule fired? what was the threshold? was it a single bad trade or
the whole portfolio?

Concrete operator-reported incident (2026-06-09): pid 41 AI proposed
BUY LXEH/CLIK/GMHS at 75% conf. Catastrophic Single Trade Gate
caught them (proposed $24,791 vs recent avg $4,713 = 5.3×, cap 5×).
Reason was in journalctl. Dashboard just said BLOCKED.

Contracts pinned:

  1. `record_trade_drop` persists each pre-broker gate disposition
     to `trade_drops`. Best-effort (DB failure never blocks the
     live pipeline).

  2. `get_recent_trade_drops` reads the recent window for one
     profile. Used by `api_cycle_data` to enrich the AI Brain
     payload.

  3. `api_cycle_data` stamps each `trades_selected` entry with
     `execution_outcome='gated'` + `gate_code` + `gate_code_display`
     + `gate_message` when a matching drop exists.

  4. The single dispatch instrumentation point in
     `trade_pipeline.run_trade_cycle` calls record_trade_drop for
     every doomsday-gate disposition. Structural pin against the
     instrumentation being silently removed.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Layer 1 — persistence (record + read round-trip)
# ---------------------------------------------------------------------------

def _make_journal_db(tmp_path):
    """Minimal journal DB with the trade_drops table."""
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_drops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT,
            drop_code TEXT NOT NULL,
            drop_reason TEXT NOT NULL,
            cycle_id TEXT,
            ai_confidence INTEGER,
            ai_reasoning TEXT
        );
    """)
    conn.commit()
    conn.close()
    return str(db)


class TestRecordTradeDropPersists:

    def test_basic_round_trip(self, tmp_path):
        from journal import record_trade_drop, get_recent_trade_drops
        db = _make_journal_db(tmp_path)
        record_trade_drop(
            db_path=db, symbol="LXEH", side="buy",
            drop_code="CATASTROPHIC_SINGLE_TRADE",
            drop_reason=(
                "Catastrophic single-trade: $24,791 is 5.3x recent "
                "avg $4,713 (cap 5.0x)"
            ),
            cycle_id="cyc-001",
            ai_confidence=75,
            ai_reasoning="Strong ensemble score and high volume...",
        )
        out = get_recent_trade_drops(db, hours=2)
        assert len(out) == 1
        row = out[0]
        assert row["symbol"] == "LXEH"
        assert row["side"] == "buy"
        assert row["drop_code"] == "CATASTROPHIC_SINGLE_TRADE"
        assert "5.3x recent avg" in row["drop_reason"]
        assert row["cycle_id"] == "cyc-001"
        assert row["ai_confidence"] == 75
        assert "ensemble score" in row["ai_reasoning"]

    def test_missing_required_fields_no_op_no_raise(self, tmp_path):
        """Defensive: bad inputs must NOT crash the live pipeline.
        record_trade_drop is called from the hot path — a crash here
        would silently kill the cycle."""
        from journal import record_trade_drop
        # No symbol → no-op, no raise
        record_trade_drop(
            db_path=str(tmp_path / "p.db"), symbol="",
            side="buy", drop_code="X", drop_reason="y",
        )
        # No db_path → no-op
        record_trade_drop(
            db_path=None, symbol="LXEH", side="buy",
            drop_code="X", drop_reason="y",
        )
        # No drop_code → no-op
        record_trade_drop(
            db_path=str(tmp_path / "p.db"), symbol="LXEH",
            side="buy", drop_code="", drop_reason="y",
        )
        # No assertion — just must not raise

    def test_record_does_not_raise_on_db_failure(self, tmp_path):
        """Live-pipeline safety: a DB write failure here must not
        propagate. The journalctl WARNING is still there for audit."""
        from journal import record_trade_drop
        record_trade_drop(
            db_path=str(tmp_path / "nonexistent_dir" / "p.db"),
            symbol="LXEH", side="buy",
            drop_code="X", drop_reason="y",
        )
        # Just must not raise


# ---------------------------------------------------------------------------
# Layer 2 — trade_pipeline instrumentation site (structural pin)
# ---------------------------------------------------------------------------

class TestTradePipelineInstrumentation:

    def test_dispatch_log_point_calls_record_trade_drop(self):
        """The single catch-all log point in run_trade_cycle (where
        every non-execution disposition converges) MUST call
        record_trade_drop. Without it the doomsday gates fire,
        log to journalctl, and the dashboard still shows nothing —
        which is exactly the pre-fix behavior the operator hated."""
        src = (REPO_ROOT / "trade_pipeline.py").read_text()
        # Pin the catch-all log message exists
        assert "Trade NOT submitted for" in src, (
            "The catch-all dispatch log point has moved or been "
            "removed. Re-locate it and ensure record_trade_drop is "
            "still called there."
        )
        # Pin the record_trade_drop call is in trade_pipeline
        assert "record_trade_drop" in src, (
            "trade_pipeline.py must call record_trade_drop to "
            "persist pre-broker drops. Without persistence the AI "
            "Brain panel's BLOCKED badge has no source-of-truth and "
            "falls back to a contextless gray badge."
        )
        # Pin that the call lives near the dispatch log (within ~50
        # lines). Catches refactors that move the call somewhere
        # disconnected from the gate disposition.
        log_idx = src.index("Trade NOT submitted for")
        rec_idx = src.index("record_trade_drop(")
        # The function definition occurs ABOVE the call site (as an
        # import). The actual call site we care about is AFTER the
        # log line. Search forward from log_idx.
        forward_rec_idx = src.find("record_trade_drop(", log_idx)
        assert forward_rec_idx > 0, (
            "record_trade_drop is referenced in trade_pipeline.py "
            "but not at the catch-all dispatch log point. The drop "
            "won't be persisted from the place where every gate "
            "actually flows through."
        )
        # Within 50 lines of the log point.
        between = src[log_idx:forward_rec_idx]
        assert between.count("\n") < 50, (
            "record_trade_drop call drifted >50 lines from the catch-"
            "all log point. Keep them adjacent so any future early-"
            "return between them is impossible to introduce silently."
        )


# ---------------------------------------------------------------------------
# Layer 3 — API enrichment + dashboard badge
# ---------------------------------------------------------------------------

class TestApiEnrichmentReadsTradeDrops:

    def test_views_imports_get_recent_trade_drops(self):
        src = (REPO_ROOT / "views.py").read_text()
        assert "get_recent_trade_drops" in src, (
            "views.py must import + call get_recent_trade_drops so "
            "api_cycle_data can populate the GATED badge"
        )
        assert "drop_by_symbol" in src, (
            "views.py must index drops by symbol for O(1) lookup "
            "in the trades_selected enrichment loop"
        )

    def test_views_stamps_gated_outcome(self):
        src = (REPO_ROOT / "views.py").read_text()
        # The stamp must include: execution_outcome='gated', gate_code,
        # gate_code_display (humanized), gate_message
        assert "'gated'" in src or '"gated"' in src, (
            "execution_outcome must be set to 'gated' when a "
            "trade_drops match exists for the symbol"
        )
        assert "gate_code_display" in src, (
            "humanized gate_code MUST surface to the UI; without it "
            "the operator sees 'CATASTROPHIC_SINGLE_TRADE' instead "
            "of 'Catastrophic Single Trade' (snake_case in UI — "
            "violates the standing rule)"
        )
        assert "gate_message" in src, (
            "the gate's human reason must reach the badge tooltip"
        )


class TestDashboardBadgeRendersGated:

    def test_dashboard_renders_gated_badge(self):
        src = (REPO_ROOT / "templates" / "dashboard.html").read_text()
        assert "execution_outcome === 'gated'" in src, (
            "dashboard.html must branch on execution_outcome='gated' "
            "and render a distinct badge — separate from the "
            "no_fill / canceled / rejected variants"
        )
        assert "gate_code_display" in src, (
            "the badge label must use the humanized gate_code so "
            "the operator sees a plain-English rule name"
        )
        assert "gate_message" in src, (
            "the tooltip must include the gate's human reason — "
            "that's the whole point of the fix"
        )
        # 'GATED' is the badge text — pin it so a future rewording
        # is explicit, not silent.
        assert "'GATED" in src, (
            "the badge text 'GATED · <rule>' is the operator-visible "
            "marker; pinning prevents a silent rename"
        )
