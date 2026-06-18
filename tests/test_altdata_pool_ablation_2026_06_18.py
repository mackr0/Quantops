"""2026-06-18 — the NoAltData ablation arm was contaminated UPSTREAM of
the AI in two ways, and the candidate-pool audit confirmed both against
the live code. This pins the fixes (fix the CLASS, not the instance):

1. POOL channel. `aggregate_candidates` ran every active strategy with no
   alt-data gate, so four alt-data-sourced strategies (insider_cluster
   score 3, insider_selling_cluster 3, short_squeeze_setup 1-2,
   analyst_upgrade_drift 1) ranked alt-data names into the AI's top-15
   shortlist even for enable_alt_data=0 profiles — whose `alt_data` block
   was only blanked in the PROMPT. Now `get_active_strategies` drops any
   strategy declaring `USES_ALT_DATA = True` when enable_alt_data is
   False, and a structural test pins that every strategy importing an
   alt-data source module declares the marker (so a future 5th one
   can't silently re-open the leak).

2. ENSEMBLE-CACHE channel. `_get_shared_ensemble` keyed its specialist
   verdicts on ctx.segment ONLY, so the first concurrent caller's
   verdicts + risk VETOs were served to every same-segment arm — leaking
   the alt-data-enriched / meta-pregated cohort into the arms that lack
   it. The key is now content-sensitive: identical ensemble input still
   shares; any difference (blanked alt_data, different symbol set,
   different disabled list, different model) recomputes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ALT_STRATEGIES = {
    "insider_cluster", "insider_selling_cluster",
    "short_squeeze_setup", "analyst_upgrade_drift",
}
# Modules whose data IS the alt-data block enable_alt_data gates. Extend
# this set if a new alt-data feed is added that strategies can source.
ALT_DATA_SOURCE_MODULES = {"alternative_data", "analyst_data"}


class _Ctx:
    def __init__(self, **kw):
        self.segment = "stocks"
        self.enable_stocks = True
        self.enable_crypto = False
        self.enable_alt_data = True
        self.db_path = None
        self.disabled_specialists = "[]"
        self.ai_provider = "test"
        self.ai_model = "test-model"
        self.ai_api_key = "k"
        for k, v in kw.items():
            setattr(self, k, v)


# ── 1. Structural class-pin ────────────────────────────────────────────

def test_every_alt_data_strategy_declares_the_marker():
    """Any strategy that imports an alt-data source module MUST declare
    USES_ALT_DATA = True, or the NoAltData pool gate silently misses it.
    """
    strat_dir = REPO / "strategies"
    offenders = []
    for py in sorted(strat_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        src = py.read_text()
        imports_alt = any(
            (f"import {m}" in src or f"from {m}" in src)
            for m in ALT_DATA_SOURCE_MODULES
        )
        if imports_alt and "USES_ALT_DATA = True" not in src:
            offenders.append(py.name)
    assert not offenders, (
        "these strategies source alt-data but don't declare "
        "USES_ALT_DATA = True (the NoAltData pool gate will miss them): "
        + ", ".join(offenders))


def test_the_four_known_alt_strategies_carry_the_marker():
    from strategies import discover_strategies
    by_name = {getattr(m, "NAME", ""): m for m in discover_strategies("stocks")}
    for nm in ALT_STRATEGIES:
        assert nm in by_name, f"{nm} not discovered"
        assert getattr(by_name[nm], "USES_ALT_DATA", False) is True, nm


# ── 2. Registry gate ───────────────────────────────────────────────────

def _active_names(enable_alt_data):
    from strategies import get_active_strategies
    mods = get_active_strategies("stocks", db_path=None,
                                 enable_alt_data=enable_alt_data)
    return {getattr(m, "NAME", "") for m in mods}


def test_registry_drops_alt_strategies_when_alt_data_disabled():
    names = _active_names(enable_alt_data=False)
    assert not (names & ALT_STRATEGIES), (
        "NoAltData arm still has alt-data strategies in its pool: "
        + ", ".join(names & ALT_STRATEGIES))
    # non-alt strategies must still be there — we didn't empty the pool
    assert names, "gating alt-data must not remove every strategy"


def test_registry_keeps_alt_strategies_when_alt_data_enabled():
    names = _active_names(enable_alt_data=True)
    assert ALT_STRATEGIES <= names, (
        "anchor/FullSystem arm lost alt-data strategies: missing "
        + ", ".join(ALT_STRATEGIES - names))


def test_default_keeps_alt_strategies():
    # default (no kwarg) must preserve pre-fix behavior for every other
    # caller of get_active_strategies.
    from strategies import get_active_strategies
    names = {getattr(m, "NAME", "")
             for m in get_active_strategies("stocks", db_path=None)}
    assert ALT_STRATEGIES <= names


# ── 3. aggregate_candidates wires the flag through ─────────────────────

def test_aggregate_candidates_passes_enable_alt_data(monkeypatch):
    import strategies
    seen = {}

    def spy(market_type, db_path=None, *, enable_stocks=True,
            enable_crypto=False, enable_alt_data=True):
        seen["enable_alt_data"] = enable_alt_data
        return []

    monkeypatch.setattr(strategies, "get_active_strategies", spy)
    from multi_strategy import aggregate_candidates
    aggregate_candidates(_Ctx(enable_alt_data=False), ["AAA"], db_path=None)
    assert seen["enable_alt_data"] is False
    aggregate_candidates(_Ctx(enable_alt_data=True), ["AAA"], db_path=None)
    assert seen["enable_alt_data"] is True


# ── 4. Ensemble content hash ───────────────────────────────────────────

def _cand(sym, alt=None):
    return {"symbol": sym, "signal": "BUY", "price": 10.0,
            "reason": "r", "alt_data": alt or {}}


def test_hash_differs_when_alt_data_differs():
    from trade_pipeline import _ensemble_content_hash
    ctx = _Ctx()
    populated = [_cand("AAA", {"insider": {"buys": 3}}), _cand("BBB")]
    blanked = [_cand("AAA", {}), _cand("BBB")]
    assert (_ensemble_content_hash(populated, ctx)
            != _ensemble_content_hash(blanked, ctx))


def test_hash_differs_when_symbol_set_differs():
    from trade_pipeline import _ensemble_content_hash
    ctx = _Ctx()
    full = [_cand("AAA"), _cand("BBB"), _cand("CCC")]
    pregated = [_cand("AAA"), _cand("CCC")]  # meta-pregate dropped BBB
    assert (_ensemble_content_hash(full, ctx)
            != _ensemble_content_hash(pregated, ctx))


def test_hash_differs_on_disabled_list_and_model():
    from trade_pipeline import _ensemble_content_hash
    cands = [_cand("AAA")]
    base = _ensemble_content_hash(cands, _Ctx())
    assert base != _ensemble_content_hash(
        cands, _Ctx(disabled_specialists='["risk_assessor"]'))
    assert base != _ensemble_content_hash(cands, _Ctx(ai_model="other"))


def test_hash_same_for_identical_input():
    from trade_pipeline import _ensemble_content_hash
    cands = [_cand("AAA", {"short": {"dtc": 5}}), _cand("BBB")]
    assert (_ensemble_content_hash(cands, _Ctx())
            == _ensemble_content_hash(list(cands), _Ctx()))


# ── 5. _get_shared_ensemble: no cross-arm leak ─────────────────────────

def test_shared_ensemble_does_not_leak_across_arms(monkeypatch):
    import ensemble
    import shared_ai_cache
    import trade_pipeline as tp

    # Hermetic: no AI calls, no L2 persistence.
    calls = {"n": 0}

    def fake_run_ensemble(candidates, ctx, **kw):
        calls["n"] += 1
        return {"per_symbol": {}, "raw": {}, "cost_calls": 0,
                "_marker": calls["n"]}

    monkeypatch.setattr(ensemble, "run_ensemble", fake_run_ensemble)
    monkeypatch.setattr(shared_ai_cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(shared_ai_cache, "put", lambda *a, **k: None)
    tp._ensemble_cache.clear()
    tp._per_key_ensemble_locks.clear()

    ctx = _Ctx()
    alt = [_cand("AAA", {"insider": {"buys": 3}}), _cand("BBB")]
    noalt = [_cand("AAA", {}), _cand("BBB")]  # NoAltData: blanked

    r_alt = tp._get_shared_ensemble(alt, ctx)
    r_noalt = tp._get_shared_ensemble(noalt, ctx)
    # Different ensemble inputs -> the NoAltData arm got its OWN verdicts,
    # not the alt-data arm's. This is the contamination fix.
    assert r_alt["_marker"] != r_noalt["_marker"]
    assert calls["n"] == 2

    # Same-config replay still hits the cache (cost-saving preserved).
    r_alt2 = tp._get_shared_ensemble(list(alt), ctx)
    assert r_alt2["_marker"] == r_alt["_marker"]
    assert calls["n"] == 2
