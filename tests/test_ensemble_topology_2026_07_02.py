"""Ensemble topology (2026-07-02 token/latency review): derived veto
authority, two-stage blockers→advisors execution, parallel specialist calls,
and the one-batched-review-per-cycle multileg dispatch.

Pins:
- Every specialist declaring HAS_VETO_AUTHORITY=True actually BLOCKS
  (option_spread_risk's declared authority was a silent no-op under the
  hardcoded legacy set — the structural option gate blocked nothing).
- Advisors (no veto authority) run ONLY on blocker-stage survivors; when
  every candidate is vetoed the advisor calls are skipped entirely.
- A no-blocker specialist set (crypto) still runs advisors on everything.
- batch_check_multileg_specialist_vetoes: one ensemble pass for a cycle's
  multileg proposals, symbol-keyed verdicts, duplicate-underlying and
  failure fallbacks are fail-open.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import ensemble as ens


class _Spec:
    def __init__(self, name, veto=False, verdicts=None, calls=None):
        self.NAME = name
        self.HAS_VETO_AUTHORITY = veto
        self._verdicts = verdicts or []
        self._calls = calls if calls is not None else []

    def build_prompt(self, chunk, ctx):
        self._calls.append([c.get("symbol") for c in chunk])
        return f"prompt:{self.NAME}"

    def parse_response(self, raw):
        return list(self._verdicts)


def _ctx():
    return SimpleNamespace(segment="stocks", db_path=None,
                           disabled_specialists="[]")


def _run(specs, candidates, monkeypatch, pipeline_kind=None):
    monkeypatch.setattr("ai_providers.call_ai", lambda *a, **k: "[]")
    return ens.run_ensemble(
        candidates, _ctx(), ai_provider="google", ai_model="m", ai_api_key="k",
        specialists_override=specs, pipeline_kind=pipeline_kind,
    )


def test_declared_veto_authority_actually_blocks(monkeypatch):
    # A specialist that DECLARES authority (like option_spread_risk) must
    # block even though its name is not in the legacy VETO_AUTHORIZED set.
    v = [{"symbol": "AAPL", "verdict": "VETO", "confidence": 90,
          "reasoning": "max loss exceeds budget"}]
    spec = _Spec("option_spread_risk", veto=True, verdicts=v)
    out = _run([spec], [{"symbol": "AAPL"}], monkeypatch)
    assert out["per_symbol"]["AAPL"]["vetoed"] is True
    assert out["per_symbol"]["AAPL"]["vetoed_by"] == "option_spread_risk"


def test_derived_veto_set_is_class_wide():
    # Class guard: the live set = legacy baseline + every declarer.
    specs = [_Spec("option_spread_risk", veto=True),
             _Spec("iv_skew_specialist", veto=False)]
    names = ens.veto_authorized_names(specs)
    assert "option_spread_risk" in names
    assert "risk_assessor" in names            # legacy baseline kept
    assert "iv_skew_specialist" not in names


def test_real_specialists_declaring_authority_are_derived():
    # The live modules: everything declaring HAS_VETO_AUTHORITY must land in
    # the derived set (pin against a future specialist being silently inert).
    from specialists import discover_specialists
    specs = discover_specialists()
    derived = ens.veto_authorized_names(specs)
    for s in specs:
        if getattr(s, "HAS_VETO_AUTHORITY", False):
            assert s.NAME in derived, s.NAME


def test_option_path_advisors_skipped_when_all_vetoed(monkeypatch):
    # Survivors-only staging applies on the OPTION pipeline, where the
    # caller consumes nothing but `vetoed` — an advisor verdict on a dead
    # proposal is decoration.
    blocker_calls, advisor_calls = [], []
    blocker = _Spec("risk_assessor", veto=True, calls=blocker_calls,
                    verdicts=[{"symbol": "AAPL", "verdict": "VETO",
                               "confidence": 90, "reasoning": "r"}])
    advisor = _Spec("iv_skew_specialist", veto=False, calls=advisor_calls)
    out = _run([blocker, advisor], [{"symbol": "AAPL"}], monkeypatch,
               pipeline_kind="option")
    assert blocker_calls == [["AAPL"]]
    assert advisor_calls == [], "advisors must not run when nothing survived"
    assert out["per_symbol"]["AAPL"]["vetoed"] is True
    assert out["cost_calls"] == 1


def test_option_path_advisors_run_on_survivors_only(monkeypatch):
    advisor_calls = []
    blocker = _Spec("risk_assessor", veto=True,
                    verdicts=[{"symbol": "AAPL", "verdict": "VETO",
                               "confidence": 90, "reasoning": "r"},
                              {"symbol": "MSFT", "verdict": "BUY",
                               "confidence": 80, "reasoning": "ok"}])
    advisor = _Spec("gamma_pin_specialist", veto=False, calls=advisor_calls)
    _run([blocker, advisor],
         [{"symbol": "AAPL"}, {"symbol": "MSFT"}], monkeypatch,
         pipeline_kind="option")
    assert advisor_calls == [["MSFT"]], (
        "advisor must see only the survivor, not the vetoed candidate")


def test_stock_path_advisors_see_full_batch_despite_veto(monkeypatch):
    # Review M1: on the STOCK path a vetoed held-symbol exit candidate is
    # veto-EXEMPT downstream (kept in the prompt as advisory context), so
    # advisors must still opine on the FULL batch — survivors-only would
    # render hollow ABSTAINs exactly on hold-vs-exit decisions.
    advisor_calls = []
    blocker = _Spec("risk_assessor", veto=True,
                    verdicts=[{"symbol": "AAPL", "verdict": "VETO",
                               "confidence": 90, "reasoning": "r"}])
    advisor = _Spec("earnings_analyst", veto=False, calls=advisor_calls,
                    verdicts=[{"symbol": "AAPL", "verdict": "SELL",
                               "confidence": 70, "reasoning": "e"}])
    monkeypatch.setattr(ens, "_any_candidate_has_upcoming_earnings",
                        lambda *a, **k: True)
    _run([blocker, advisor], [{"symbol": "AAPL"}, {"symbol": "MSFT"}],
         monkeypatch)   # no pipeline_kind → stock-shaped legacy path
    assert advisor_calls == [["AAPL", "MSFT"]], (
        "stock-path advisors must see the full batch, vetoed included")


def test_no_blockers_advisors_see_everything(monkeypatch):
    # Crypto-style set: pattern-only, no veto authority anywhere.
    advisor_calls = []
    advisor = _Spec("pattern_recognizer", veto=False, calls=advisor_calls,
                    verdicts=[{"symbol": "BTC/USD", "verdict": "BUY",
                               "confidence": 70, "reasoning": "trend"}])
    out = _run([advisor], [{"symbol": "BTC/USD"}], monkeypatch)
    assert advisor_calls == [["BTC/USD"]]
    assert out["per_symbol"]["BTC/USD"]["verdict"] == "BUY"


def test_plain_prompt_path_uses_small_chunks(monkeypatch):
    # Review M4: on the non-tool (plain-prompt) path a dropped verdict in a
    # batched review fails OPEN (missing symbol = not vetoed), so chunks
    # shrink to CHUNK_SIZE_PLAIN — the documented reliable size.
    calls = []
    spec = _Spec("risk_assessor", veto=True, calls=calls)
    cands = [{"symbol": f"S{i}"} for i in range(7)]
    _run([spec], cands, monkeypatch)   # google → plain-prompt path
    assert all(len(c) <= ens.CHUNK_SIZE_PLAIN for c in calls), calls
    assert sum(len(c) for c in calls) == 7


# --- batched multileg dispatch ------------------------------------------------

def _ml(symbol):
    return {"symbol": symbol, "action": "MULTILEG_OPEN",
            "strategy_name": "bull_put_spread",
            "strikes": {"short": 100, "long": 95}, "expiry": "2026-08-21",
            "contracts": 1}


def test_batch_multileg_vetoes_one_pass(monkeypatch):
    from trade_pipeline import batch_check_multileg_specialist_vetoes
    calls = {"n": 0}

    class _FakeVerdict:
        vetoed = [{"symbol": "AAPL"}]
        approved = [{"symbol": "MSFT"}]
        veto_log = ["AAPL: VETO (option_spread_risk) — max loss"]

    def _fake_route(self, ctx, ai_result):
        calls["n"] += 1
        assert len(ai_result.proposals) == 2
        return _FakeVerdict()

    monkeypatch.setattr("pipelines.option.OptionPipeline.route_to_specialists",
                        _fake_route)
    out = batch_check_multileg_specialist_vetoes(
        SimpleNamespace(), [_ml("AAPL"), _ml("MSFT"),
                            {"symbol": "NVDA", "action": "BUY"}])
    assert calls["n"] == 1                     # ONE ensemble pass
    assert out["AAPL"][0] is True and "max loss" in out["AAPL"][1]
    assert out["MSFT"] == (False, "")
    assert "NVDA" not in out                    # non-multileg ignored


def test_batch_multileg_duplicate_underlying_falls_back(monkeypatch):
    from trade_pipeline import batch_check_multileg_specialist_vetoes
    monkeypatch.setattr(
        "pipelines.option.OptionPipeline.route_to_specialists",
        lambda self, ctx, r: (_ for _ in ()).throw(AssertionError(
            "must not batch when <2 unique symbols remain")))
    # Two spreads on the SAME underlying → symbol-keyed verdicts would
    # collide → both excluded → <2 remain → no batch call, empty map.
    out = batch_check_multileg_specialist_vetoes(
        SimpleNamespace(), [_ml("AAPL"), _ml("AAPL"), _ml("TSLA")])
    assert out == {}


def test_batch_multileg_failopen(monkeypatch):
    from trade_pipeline import batch_check_multileg_specialist_vetoes
    monkeypatch.setattr(
        "pipelines.option.OptionPipeline.route_to_specialists",
        lambda self, ctx, r: (_ for _ in ()).throw(RuntimeError("boom")))
    out = batch_check_multileg_specialist_vetoes(
        SimpleNamespace(), [_ml("AAPL"), _ml("MSFT")])
    assert out == {}, "batch failure → empty map → solo fallback"
