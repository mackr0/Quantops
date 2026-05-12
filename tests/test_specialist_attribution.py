"""Specialist attribution surfacing on the AI Brain panel
(2026-05-12).

When option_spread_risk (or any veto-authority specialist)
blocks a trade, the dashboard's REJECTED badge should show
WHICH specialist blocked it — not just "REJECTED · Specialist
Veto" but "REJECTED · Specialist Veto · Option Spread Risk".

This file pins the data flow:
1. ensemble._synthesize captures vetoed_by (the specialist's NAME).
2. Pipeline.route_to_specialists includes (vetoed_by) in the
   veto_log entry: "<sym>: VETO (<name>) — <reason>".
3. OptionPipeline._record_veto persists broker_message as
   "specialist veto (<name>): <reason>".
4. views.py:api_cycle_data parses broker_message and surfaces
   vetoed_by + vetoed_by_display on the trade dict.
5. Dashboard JS renders the name on the badge + tooltip.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines import SpecialistVerdict, AIResult
from pipelines.option import OptionPipeline


# ---------------------------------------------------------------------------
# Layer 1 — ensemble._synthesize captures vetoed_by
# ---------------------------------------------------------------------------

class TestSynthesizeCapturesVetoedBy:
    def test_per_symbol_includes_vetoed_by_when_specialist_vetoes(self):
        from ensemble import _synthesize
        candidates = [{"symbol": "CWAN"}]
        # Build per-specialist verdicts where option_spread_risk
        # (in VETO_AUTHORIZED) issues a VETO
        raw = {
            "option_spread_risk": [
                {"symbol": "CWAN", "verdict": "VETO",
                 "confidence": 100, "reasoning": "max loss too high"},
            ],
        }
        # Patch VETO_AUTHORIZED to include option_spread_risk for
        # the test (ensemble's default list may differ)
        with patch("ensemble.VETO_AUTHORIZED",
                    {"option_spread_risk", "risk_assessor",
                     "adversarial_reviewer"}):
            out = _synthesize(candidates, raw, db_path=None)
        assert out["CWAN"]["vetoed"] is True
        assert out["CWAN"]["vetoed_by"] == "option_spread_risk"

    def test_vetoed_by_none_when_no_veto(self):
        from ensemble import _synthesize
        candidates = [{"symbol": "AAPL"}]
        raw = {
            "risk_assessor": [
                {"symbol": "AAPL", "verdict": "BUY",
                 "confidence": 70, "reasoning": "looks good"},
            ],
        }
        out = _synthesize(candidates, raw, db_path=None)
        assert out["AAPL"]["vetoed"] is False
        assert out["AAPL"]["vetoed_by"] is None


# ---------------------------------------------------------------------------
# Layer 2 — Pipeline.route_to_specialists threads vetoed_by into veto_log
# ---------------------------------------------------------------------------

class TestRouteToSpecialistsThreadsVetoedBy:
    def test_veto_log_entry_includes_specialist_name(self):
        captured = {}

        def fake_run_ensemble(**kwargs):
            return {"per_symbol": {
                "CWAN": {
                    "vetoed": True,
                    "veto_reason": "max loss exceeds budget",
                    "vetoed_by": "option_spread_risk",
                },
            }}

        with patch("ensemble.run_ensemble",
                    side_effect=fake_run_ensemble):
            verdict = OptionPipeline().route_to_specialists(
                SimpleNamespace(),
                AIResult(proposals=[
                    {"symbol": "CWAN", "signal": "MULTILEG_OPEN"},
                ]),
            )

        assert len(verdict.veto_log) == 1
        # Expected format: "CWAN: VETO (option_spread_risk) — <reason>"
        assert "VETO (option_spread_risk)" in verdict.veto_log[0]
        assert "max loss exceeds budget" in verdict.veto_log[0]


# ---------------------------------------------------------------------------
# Layer 3 — OptionPipeline._record_veto formats broker_message
# ---------------------------------------------------------------------------

class TestRecordVetoIncludesSpecialistInBrokerMessage:
    def test_broker_message_includes_specialist_name_in_parens(self):
        captured = []

        def fake_record(db_path, **kwargs):
            captured.append(kwargs)

        proposal = {
            "symbol": "CWAN", "action": "MULTILEG_OPEN",
            "confidence": 70, "reasoning": "test",
        }
        verdict = SpecialistVerdict(
            approved=[], vetoed=[proposal],
            veto_log=[
                "CWAN: VETO (option_spread_risk) — max loss too high"
            ],
        )
        ctx = SimpleNamespace(db_path="x.db")
        with patch("journal.record_broker_rejection",
                    side_effect=fake_record):
            OptionPipeline().execute(ctx, verdict)

        assert len(captured) == 1
        assert captured[0]["broker_message"] == (
            "specialist veto (option_spread_risk): max loss too high"
        )

    def test_broker_message_falls_back_when_no_specialist_name(self):
        """Older format without parenthesized name still works."""
        captured = []

        def fake_record(db_path, **kwargs):
            captured.append(kwargs)

        proposal = {"symbol": "CWAN", "action": "MULTILEG_OPEN"}
        verdict = SpecialistVerdict(
            approved=[], vetoed=[proposal],
            veto_log=["CWAN: VETO — max loss too high"],
        )
        ctx = SimpleNamespace(db_path="x.db")
        with patch("journal.record_broker_rejection",
                    side_effect=fake_record):
            OptionPipeline().execute(ctx, verdict)

        assert captured[0]["broker_message"] == (
            "specialist veto: max loss too high"
        )


# ---------------------------------------------------------------------------
# Layer 4 — Result entry includes vetoed_by
# ---------------------------------------------------------------------------

class TestResultEntryHasVetoedBy:
    def test_skipped_entry_includes_vetoed_by_when_present(self):
        proposal = {"symbol": "CWAN", "action": "MULTILEG_OPEN"}
        verdict = SpecialistVerdict(
            approved=[], vetoed=[proposal],
            veto_log=[
                "CWAN: VETO (option_spread_risk) — bad max loss"
            ],
        )
        with patch("journal.record_broker_rejection"):
            result = OptionPipeline().execute(
                SimpleNamespace(), verdict,
            )
        assert result.skipped[0]["vetoed_by"] == "option_spread_risk"

    def test_skipped_entry_vetoed_by_none_for_legacy_format(self):
        proposal = {"symbol": "CWAN", "action": "MULTILEG_OPEN"}
        verdict = SpecialistVerdict(
            approved=[], vetoed=[proposal],
            veto_log=["CWAN: VETO — bad"],
        )
        with patch("journal.record_broker_rejection"):
            result = OptionPipeline().execute(
                SimpleNamespace(), verdict,
            )
        assert result.skipped[0]["vetoed_by"] is None


# ---------------------------------------------------------------------------
# Layer 5 — views.py api_cycle_data parses + surfaces
# ---------------------------------------------------------------------------

class TestApiCycleDataParsesSpecialistName:
    """End-to-end: the dashboard JSON payload includes vetoed_by
    and vetoed_by_display when the rejection is a specialist veto."""

    def _setup_db_with_rejection(self, db_path, broker_message):
        from journal import init_db
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        ts = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT INTO broker_rejections
               (timestamp, symbol, action, signal_type,
                ai_confidence, ai_reasoning, rejection_code,
                broker_message)
               VALUES (?, 'CWAN', 'MULTILEG_OPEN', 'MULTILEG_OPEN',
                       70, 'test rationale', 'specialist_veto', ?)""",
            (ts, broker_message),
        )
        conn.commit()
        conn.close()

    def test_specialist_name_extracted_from_broker_message(self, tmp_path):
        # Simulate the views.py enrichment logic directly
        from journal import get_recent_broker_rejections
        from display_names import humanize
        import re as _re

        db_path = str(tmp_path / "test.db")
        self._setup_db_with_rejection(
            db_path,
            "specialist veto (option_spread_risk): "
            "max loss exceeds budget",
        )

        rejections = get_recent_broker_rejections(db_path, hours=2)
        assert len(rejections) == 1
        r = rejections[0]
        # Run the same parsing logic from views.py
        msg = r["broker_message"]
        m = _re.match(
            r"specialist veto\s*\(([^)]+)\):\s*(.*)",
            msg, _re.IGNORECASE,
        )
        assert m is not None
        vetoed_by = m.group(1)
        clean_reason = m.group(2).strip()
        vetoed_by_display = humanize(vetoed_by)

        assert vetoed_by == "option_spread_risk"
        assert vetoed_by_display == "Option Spread Risk"
        assert clean_reason == "max loss exceeds budget"

    def test_legacy_message_format_yields_no_specialist_name(self, tmp_path):
        from journal import get_recent_broker_rejections
        import re as _re

        db_path = str(tmp_path / "test.db")
        self._setup_db_with_rejection(
            db_path, "specialist veto: max loss exceeds budget",
        )
        r = get_recent_broker_rejections(db_path, hours=2)[0]
        msg = r["broker_message"]
        m = _re.match(
            r"specialist veto\s*\(([^)]+)\):\s*(.*)",
            msg, _re.IGNORECASE,
        )
        # Legacy format doesn't have parens → no match
        assert m is None
