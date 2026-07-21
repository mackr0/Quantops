"""2026-07-21 — AI-Brain truthfulness pins.

The dashboard showed action-claiming Decision narratives ("initiating
long positions in Energy") beside "No candidates met threshold this
cycle". Causes: (1) the AI was called with an EMPTY candidate list
after gates wiped the shortlist and fabricated a narrative; (2) cycles
that never reached the pipeline (halt / empty screen / funding guard)
wrote no snapshot, so the panel showed the PREVIOUS cycle's narrative
as current; (3) one catch-all string asserted a "threshold" cause for
every kind of emptiness.

Pins:
  1. ai_select_trades with zero candidates NEVER calls the model and
     returns an explicit pass.
  2. run_trade_cycle's gates-emptied guard sits between the ensemble
     stage and the AI call (structural).
  3. _save_cycle_data records no_candidates_reason and renders the
     Decision line as "PASS — <reason>", never a fabricated narrative.
  4. The scheduler skip paths write truthful snapshots
     (_write_skip_snapshot) for funding-guard, empty-screen, and halt.
  5. The batch prompt carries the PORTFOLIO_REASONING CONTRACT
     (narrative may only describe presented candidates).
  6. The dashboard renders data.no_candidates_reason when present.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestNoModelCallOnEmptyCandidates:
    def test_ai_select_trades_empty_list_never_calls_model(self):
        from ai_analyst import ai_select_trades
        with patch("ai_analyst.call_ai",
                   side_effect=AssertionError(
                       "model must NOT be called with zero candidates")):
            resp = ai_select_trades([], {}, {}, ctx=None)
        assert resp["pass_this_cycle"] is True
        assert resp["trades"] == []
        assert resp["portfolio_reasoning"] == "", (
            "an empty cycle must carry NO narrative — fabricated "
            "rotation stories are the 2026-07-21 bug")
        assert resp.get("no_candidates") is True

    def test_pipeline_guard_sits_before_ai_call(self):
        """Structural: run_trade_cycle bails out with the funnel
        accounting BEFORE ai_select_trades when gates emptied the
        list."""
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        guard = src.index("Gates emptied the cycle:")
        ai_call = src.index(
            "ai_response = ai_select_trades(candidates_data")
        pregate = src.index("_meta_pregate_candidates(candidates_data")
        assert pregate < guard < ai_call, (
            "the gates-emptied guard must run after the pregate/veto "
            "stages and before the AI call")


class TestSnapshotTruthfulness:
    def _ctx(self, pid=901):
        ctx = MagicMock()
        ctx.profile_id = pid
        ctx.display_name = "TestProfile"
        ctx.db_path = None
        return ctx

    def test_save_cycle_data_records_reason(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from trade_pipeline import _save_cycle_data
        _save_cycle_data(self._ctx(), [], [], [], "", {}, {},
                         no_candidates_reason="Gates emptied the cycle: "
                                              "5 shortlisted → 0 left")
        data = json.load(open(tmp_path / "cycle_data_901.json"))
        assert data["no_candidates_reason"].startswith(
            "Gates emptied the cycle")
        assert data["ai_reasoning"].startswith("PASS — "), (
            "the Decision line must read as an explicit pass, never "
            "as an action narrative")
        assert data["shortlist"] == []
        assert data["trades_selected"] == []

    def test_save_cycle_data_without_reason_keeps_legacy_default(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from trade_pipeline import _save_cycle_data
        _save_cycle_data(self._ctx(902), [], [], [], "", {}, {})
        data = json.load(open(tmp_path / "cycle_data_902.json"))
        assert data["ai_reasoning"] == "No candidates shortlisted"

    def test_scheduler_skip_snapshot_writes_reason(self, tmp_path,
                                                   monkeypatch):
        monkeypatch.chdir(tmp_path)
        from multi_scheduler import _write_skip_snapshot
        _write_skip_snapshot(self._ctx(903),
                             "Profile HALTED — test reason. No AI call "
                             "was made.")
        data = json.load(open(tmp_path / "cycle_data_903.json"))
        assert "HALTED" in data["no_candidates_reason"]
        assert data["ai_reasoning"].startswith("PASS — ")

    def test_all_three_skip_paths_write_snapshots(self):
        """Structural: funding-guard, empty-screen, and halt returns
        each call _write_skip_snapshot before returning."""
        src = open(os.path.join(REPO, "multi_scheduler.py")).read()
        for anchor in ("Account funding guard blocked",
                       "Screener returned 0 candidates",
                       "Profile HALTED — {halt_reason}"):
            assert anchor in src, f"skip snapshot missing: {anchor!r}"


class TestPromptContract:
    def test_batch_prompt_carries_reasoning_contract(self):
        from ai_analyst import _build_batch_prompt
        prompt = _build_batch_prompt(
            [{"symbol": "XYZ", "signal": "BUY", "score": 3,
              "price": 15.0}],
            {"cash": 1000, "equity": 1000, "positions": []},
            {}, None)
        assert "PORTFOLIO_REASONING CONTRACT" in prompt
        assert "you cannot trade what was not presented" in prompt


class TestDashboardRendersReason:
    def test_candidates_panel_uses_no_candidates_reason(self):
        src = open(os.path.join(
            REPO, "templates", "dashboard.html")).read()
        assert "data.no_candidates_reason" in src, (
            "the empty-candidates branch must render the pipeline's "
            "stated cause, not only the catch-all")
