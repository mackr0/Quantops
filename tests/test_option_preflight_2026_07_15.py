"""Option-proposal preflight + veto_class split (2026-07-15).

The specialists reviewed option proposals through a STOCK-shaped
renderer that dropped every option field — option_spread_risk saw
`- COST [? @ $0]:` for a fully-priced bear-call spread and truthfully
vetoed "missing strike and premium data" 78 times (100% of its vetoes
since 07-08; the vetoed rows carried real premiums stamped seconds
later by the SAME process). Those plumbing vetoes fed P(veto)
identically to genuine risk judgments and sat one sample from locking
a 0.5 RAR discount on p212's bull_call/staples pair.

Pinned here:
  - the preflight enriches option proposals with priced economics and
    REFUSES to send an unpriceable one to the LLM (dropped loudly,
    blocked from execution, veto_class='invalid_input');
  - the renderer's option branch shows strategy/strikes/expiry/DTE/
    premium/max-loss/spot/IV rank;
  - invalid_input rows are excluded from P(veto) counts, veto-quality
    counts, and the would-be-P&L resolver;
  - the veto_class column exists in DDL + the explicit migration list.
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _complete_proposal():
    return {
        "symbol": "COST", "action": "MULTILEG_OPEN",
        "strategy_name": "bear_call_spread",
        "strikes": {"short": 960, "long": 1005},
        "expiry": "2099-08-14", "contracts": 3,
        "confidence": 70, "reasoning": "iv elevated",
    }


def _patch_market(monkeypatch, priced=True, spot=945.12, iv=62.0):
    import options_oracle
    import options_strategy_advisor as osa

    def _oracle(symbol):
        return {"has_options": True, "current_price": spot,
                "iv_rank": {"rank_pct": iv}}
    monkeypatch.setattr(options_oracle, "get_options_oracle", _oracle)

    def _price(prec):
        prec["is_credit"] = True
        prec["spread_width_points"] = 45.0
        if priced:
            prec["priced"] = True
            prec["entry_net_premium"] = 682.0
            prec["max_loss_per_contract"] = 3818.0
            prec["max_gain_per_contract"] = 682.0
            prec["breakeven"] = 966.82
            prec["legs"] = [{"occ": "S", "side": "sell"},
                            {"occ": "L", "side": "buy"}]
        else:
            prec["priced"] = False
            prec["max_loss_per_contract"] = 4500.0  # width fallback
    monkeypatch.setattr(osa, "_price_option_rec", _price)


class TestPreflightEnrichment:
    def test_complete_vertical_enriched_and_ready(self, monkeypatch):
        from pipelines import _preflight_option_proposals
        _patch_market(monkeypatch)
        p = _complete_proposal()
        ready, dropped = _preflight_option_proposals(None, [p])
        assert ready == [p] and not dropped
        econ = p["_specialist_econ"]
        assert econ["entry_net_premium"] == 682.0
        assert econ["max_loss_per_contract"] == 3818.0
        assert econ["spot"] == 945.12
        assert econ["iv_rank"] == 62.0
        assert econ["dte"] is not None and econ["dte"] > 0

    def test_missing_strikes_dropped_loudly(self, monkeypatch, caplog):
        import logging
        from pipelines import _preflight_option_proposals
        _patch_market(monkeypatch)
        p = _complete_proposal()
        del p["strikes"]
        with caplog.at_level(logging.ERROR):
            ready, dropped = _preflight_option_proposals(None, [p])
        assert not ready and dropped == [p]
        assert p["_veto_class"] == "invalid_input"
        assert any("OPTION PREFLIGHT dropped" in r.message
                   for r in caplog.records)

    def test_unpriceable_vertical_dropped(self, monkeypatch):
        """A vertical whose legs can't be trusted-priced is exactly what
        the specialist used to veto for 'missing premium data' — now it
        never reaches the LLM at all."""
        from pipelines import _preflight_option_proposals
        _patch_market(monkeypatch, priced=False)
        p = _complete_proposal()
        ready, dropped = _preflight_option_proposals(None, [p])
        assert not ready and dropped == [p]
        assert "spread pricing" in ", ".join(p["_preflight_missing"])

    def test_stock_proposals_pass_untouched(self, monkeypatch):
        from pipelines import _preflight_option_proposals
        p = {"symbol": "AAPL", "action": "BUY", "confidence": 70}
        ready, dropped = _preflight_option_proposals(None, [p])
        assert ready == [p] and not dropped
        assert "_specialist_econ" not in p

    def test_dropped_proposal_recorded_as_trade_drop(self, monkeypatch):
        import journal
        from pipelines import _preflight_option_proposals
        _patch_market(monkeypatch)
        drops = []
        monkeypatch.setattr(
            journal, "record_trade_drop",
            lambda **kw: drops.append(kw))
        ctx = MagicMock()
        ctx.db_path = "x.db"
        p = _complete_proposal()
        p["contracts"] = 0
        _preflight_option_proposals(ctx, [p])
        assert drops and drops[0]["drop_code"] == "OPTION_INPUT_INCOMPLETE"
        assert drops[0]["symbol"] == "COST"


class TestRouteToSpecialistsIntegration:
    def test_dropped_proposals_are_vetoed_not_approved(self, monkeypatch):
        """A preflight drop must BLOCK execution (join `vetoed`) — if it
        vanished from both lists the legacy callers would read it as
        approved and execute an unreviewed spread."""
        import pipelines
        from pipelines import AIResult
        from pipelines.option import OptionPipeline
        _patch_market(monkeypatch)
        p = _complete_proposal()
        del p["expiry"]
        ensemble_called = []
        import ensemble
        monkeypatch.setattr(
            ensemble, "run_ensemble",
            lambda **kw: ensemble_called.append(kw) or {"per_symbol": {}})
        verdict = OptionPipeline().route_to_specialists(
            None, AIResult(proposals=[p]))
        assert p in verdict.vetoed
        assert p not in verdict.approved
        assert not ensemble_called, (
            "an incomplete proposal must never reach the LLM ensemble")
        assert any("input_incomplete" in line for line in verdict.veto_log)

    def test_complete_proposals_reach_ensemble_enriched(self, monkeypatch):
        import pipelines
        from pipelines import AIResult
        from pipelines.option import OptionPipeline
        _patch_market(monkeypatch)
        p = _complete_proposal()
        seen = {}
        import ensemble
        def _fake_ensemble(**kw):
            seen["candidates"] = kw.get("candidates")
            return {"per_symbol": {}}
        monkeypatch.setattr(ensemble, "run_ensemble", _fake_ensemble)
        verdict = OptionPipeline().route_to_specialists(
            None, AIResult(proposals=[p]))
        assert p in verdict.approved
        assert seen["candidates"][0].get("_specialist_econ"), (
            "the ensemble must receive the ENRICHED proposal")


class TestOptionRender:
    def test_option_proposal_renders_its_economics(self):
        from specialists._common import format_candidate_for_specialist
        p = _complete_proposal()
        p["_specialist_econ"] = {
            "strategy": "bear_call_spread", "contracts": 3,
            "expiry": "2099-08-14", "dte": 32, "spot": 945.12,
            "iv_rank": 62.0, "is_credit": True,
            "entry_net_premium": 682.0, "max_loss_per_contract": 3818.0,
            "max_gain_per_contract": 682.0, "breakeven": 966.82,
            "spread_width_points": 45.0, "legs": [], "single_leg": False,
        }
        line = format_candidate_for_specialist(p, "option_spread_risk")
        for needle in ("bear_call_spread", "CREDIT", "$682.00",
                       "short 960", "long 1005", "2099-08-14", "32 DTE",
                       "$3,818.00", "$11,454.00 total", "966.82",
                       "$945.12", "IV rank 62"):
            assert needle in line, f"render missing {needle!r}: {line}"
        assert "[? @ $0]" not in line

    def test_option_branch_fires_even_without_econ(self):
        """Belt: an option proposal that somehow skipped the preflight
        still renders its own fields honestly ('unavailable') instead
        of falling into the stock shape's '? @ $0'."""
        from specialists._common import format_candidate_for_specialist
        p = _complete_proposal()
        line = format_candidate_for_specialist(p, "option_spread_risk")
        assert "[? @ $0]" not in line
        assert "bear_call_spread" in line
        assert "short 960" in line

    def test_stock_render_unchanged(self):
        from specialists._common import format_candidate_for_specialist
        line = format_candidate_for_specialist(
            {"symbol": "AAPL", "signal": "BUY", "price": 212.5,
             "reason": "momentum"}, "adversarial_reviewer")
        assert line.startswith("- AAPL [BUY @ $212.5]")


# ---------------------------------------------------------------------------
# veto_class — storage + learning exclusion
# ---------------------------------------------------------------------------

@pytest.fixture
def outcomes_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = str(tmp_path / "quantopsai_profile_7.db")
    from journal import init_db
    init_db(path)
    return path


class TestVetoClassStorage:
    def test_column_exists_in_fresh_ddl_and_migration_list(self, outcomes_db):
        conn = sqlite3.connect(outcomes_db)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(option_proposal_outcomes)").fetchall()}
        conn.close()
        assert "veto_class" in cols
        # explicit _migrate_all_columns entry (the 2026-07-02 lesson)
        import journal
        src = inspect.getsource(journal._migrate_all_columns)
        assert '"option_proposal_outcomes"' in src
        assert '"veto_class"' in src

    def test_genuine_veto_defaults_to_risk(self, outcomes_db):
        from journal import record_option_proposal_outcome
        rid = record_option_proposal_outcome(
            outcomes_db, symbol="COST", strategy="bear_call_spread",
            sector="staples", vetoed=1, veto_reason="book concentration")
        conn = sqlite3.connect(outcomes_db)
        vc = conn.execute(
            "SELECT veto_class FROM option_proposal_outcomes WHERE id=?",
            (rid,)).fetchone()[0]
        conn.close()
        assert vc == "risk"

    def test_accepted_row_has_null_class(self, outcomes_db):
        from journal import record_option_proposal_outcome
        rid = record_option_proposal_outcome(
            outcomes_db, symbol="COST", strategy="bear_call_spread",
            sector="staples", vetoed=0)
        conn = sqlite3.connect(outcomes_db)
        vc = conn.execute(
            "SELECT veto_class FROM option_proposal_outcomes WHERE id=?",
            (rid,)).fetchone()[0]
        conn.close()
        assert vc is None

    def test_invalid_input_excluded_from_pveto_counts(self, outcomes_db):
        """The load-bearing exclusion: 29 plumbing vetoes + 1 real one
        must read as 1 veto / 1 proposal — not 30/30 locking a max
        discount on an innocent pair."""
        from journal import (record_option_proposal_outcome,
                             option_veto_counts)
        for _ in range(29):
            record_option_proposal_outcome(
                outcomes_db, symbol="COST", strategy="bull_call_spread",
                sector="staples", vetoed=1,
                veto_reason="missing strike data",
                veto_class="invalid_input")
        record_option_proposal_outcome(
            outcomes_db, symbol="COST", strategy="bull_call_spread",
            sector="staples", vetoed=1, veto_reason="real risk judgment")
        counts = option_veto_counts(outcomes_db)
        assert counts == [("bull_call_spread", "staples", 1, 1)]

    def test_invalid_input_never_resolved_by_the_shadow_resolver(
            self, outcomes_db):
        from journal import (record_option_proposal_outcome,
                             pending_veto_outcomes)
        record_option_proposal_outcome(
            outcomes_db, symbol="COST", strategy="bear_call_spread",
            sector="staples", vetoed=1, veto_class="invalid_input",
            entry_net_premium=682.0, max_loss_per_contract=3818.0,
            max_gain_per_contract=682.0, lo_strike=960, hi_strike=1005,
            expiry="2000-01-01")
        assert pending_veto_outcomes(outcomes_db, "2099-01-01") == []

    def test_invalid_input_excluded_from_quality_counts(self, outcomes_db):
        from journal import (record_option_proposal_outcome,
                             option_veto_quality_counts)
        rid = record_option_proposal_outcome(
            outcomes_db, symbol="COST", strategy="bear_call_spread",
            sector="staples", vetoed=1, veto_class="invalid_input")
        conn = sqlite3.connect(outcomes_db)
        conn.execute("UPDATE option_proposal_outcomes SET "
                     "status='resolved', wouldbe_outcome='loss' "
                     "WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        assert option_veto_quality_counts(outcomes_db) == []

    def test_recorder_falls_back_to_reason_text_marker(
            self, outcomes_db, monkeypatch):
        """The legacy dispatch path rebuilds the proposal dict and loses
        the in-place _veto_class marker; only the veto_log text (with
        the 'input_incomplete' attribution) survives. The recorder must
        classify from that text."""
        import sector_classifier
        monkeypatch.setattr(sector_classifier, "get_sector",
                            lambda *a, **k: "staples")
        from pipelines.option import OptionPipeline
        ctx = MagicMock()
        ctx.db_path = outcomes_db
        proposal = _complete_proposal()  # NO _veto_class marker
        OptionPipeline._record_option_outcome(
            ctx, proposal, "COST", vetoed_flag=1,
            veto_reason=("input_incomplete: VETO (input_incomplete) — "
                         "option proposal missing usable strikes"))
        conn = sqlite3.connect(outcomes_db)
        vc = conn.execute(
            "SELECT veto_class FROM option_proposal_outcomes "
            "ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.close()
        assert vc == "invalid_input"


# ---------------------------------------------------------------------------
# Adversarial-review round (2026-07-15) pins
# ---------------------------------------------------------------------------

class TestReviewRoundPreflight:
    def test_counterfactual_corpus_excludes_invalid_input(self, outcomes_db):
        """Review #10: the fine-tune counterfactual corpus is the
        FOURTH consumer — a plumbing veto resolved before the backfill
        must never train as a specialist judgment."""
        from journal import (record_option_proposal_outcome,
                             resolved_veto_counterfactuals)
        rid = record_option_proposal_outcome(
            outcomes_db, symbol="COST", strategy="bear_call_spread",
            sector="staples", vetoed=1, veto_class="invalid_input",
            veto_reason="option_spread_risk: missing strike data")
        conn = sqlite3.connect(outcomes_db)
        conn.execute(
            "UPDATE option_proposal_outcomes SET status='resolved', "
            "wouldbe_outcome='loss', wouldbe_pnl=-100 WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        assert resolved_veto_counterfactuals(outcomes_db) == []
        # a genuine resolved risk veto still exports
        rid2 = record_option_proposal_outcome(
            outcomes_db, symbol="CVX", strategy="bull_put_spread",
            sector="energy", vetoed=1, veto_reason="credit too thin")
        conn = sqlite3.connect(outcomes_db)
        conn.execute(
            "UPDATE option_proposal_outcomes SET status='resolved', "
            "wouldbe_outcome='loss', wouldbe_pnl=-50 WHERE id=?", (rid2,))
        conn.commit()
        conn.close()
        rows = resolved_veto_counterfactuals(outcomes_db)
        assert len(rows) == 1 and rows[0]["symbol"] == "CVX"

    def test_veto_reason_prefers_exact_symbol_prefix(self):
        """Review #12: 'A' must not inherit AAPL's preflight line via
        the substring fallback — that laundered a genuine risk veto
        into veto_class='invalid_input' on the batch path."""
        from pipelines import SpecialistVerdict
        from pipelines.option import OptionPipeline
        verdict = SpecialistVerdict(
            vetoed=[{"symbol": "AAPL"}, {"symbol": "A"}],
            veto_log=[
                "AAPL: VETO (input_incomplete) — option proposal "
                "missing usable spot price; dropped before specialist "
                "review",
                "A: VETO (option_spread_risk) — credit too thin vs "
                "max loss",
            ])
        reason, by = OptionPipeline._veto_reason_for(verdict, "A")
        assert by == "option_spread_risk"
        assert "credit too thin" in reason
        assert "input_incomplete" not in reason

    def test_protective_put_priced_as_buy(self, monkeypatch):
        """Review #14: protective_put is a BOUGHT hedge — 'long'-token
        side derivation priced it as a CREDIT with no max loss."""
        import options_oracle
        import options_strategy_advisor as osa
        from pipelines import _enrich_option_proposal
        monkeypatch.setattr(
            options_oracle, "get_options_oracle",
            lambda s: {"has_options": True, "current_price": 100.0,
                       "iv_rank": {"rank_pct": 40.0}})
        seen = {}

        def _prem(occ, side):
            seen["side"] = side
            return 2.50
        monkeypatch.setattr(osa, "_cached_option_premium", _prem)
        p = {"symbol": "NVDA", "action": "OPTIONS",
             "option_strategy": "protective_put", "strike": 95,
             "expiry": "2099-08-14", "contracts": 1, "confidence": 60}
        missing = _enrich_option_proposal(None, p)
        assert missing == []
        assert seen["side"] == "buy"
        econ = p["_specialist_econ"]
        assert econ["is_credit"] is False
        assert econ["max_loss_per_contract"] == pytest.approx(250.0)

    def test_dispatch_recorder_skips_preflight_duplicates(self):
        import inspect
        import trade_pipeline
        src = inspect.getsource(trade_pipeline)
        assert '_already_recorded = "input_incomplete" in str(drop_reason)' \
            in src, (
            "review #13: a preflight-dropped proposal must not be "
            "double-recorded as SPECIALIST_VETOED")

    def test_prompt_formula_matches_dollar_render(self):
        """Review #11: the prompt told the specialist to multiply a
        DOLLAR max-loss by 100 × contracts — ~$1.1M vs a $ budget would
        re-create the veto storm as genuine 'risk' vetoes."""
        import inspect
        import specialists.option_spread_risk as osr
        src = inspect.getsource(osr)
        assert "spread_max_loss × 100 × contracts" not in src
        assert "do NOT multiply again" in src
