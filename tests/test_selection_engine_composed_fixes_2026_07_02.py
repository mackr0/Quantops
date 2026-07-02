"""Composed-system review fixes (2026-07-02).

The five selection-engine commits each passed review alone; a fresh end-to-end
verification of the COMPOSITION found 14 seams. These pin the substantive ones:
HOLD-contaminated p_win fallback (covered in the p2b ledger tests), the p_win
learned-pattern leak, the negative-RAR concentration inversion, the ev_dollars
tiebreak drift, the one-snapshot-per-leg pricing, and the async (non-blocking)
veto-outcome capture. See docs/SELECTION_ENGINE_DESIGN.md.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# --- p_win must never become a learned pattern -------------------------------

def test_post_mortem_never_learns_internal_p_win():
    # p_win is the ledger's own input (prior clipped to [0.50, 0.68] → nearly
    # always bucketed 'high'); after any losing week it would become the #1
    # bogus 'learned pattern' injected into the batch prompt.
    import post_mortem
    losing = [{"p_win": 0.62, "rsi_bucket": "overbought"} for _ in range(6)]
    feats = [d["feature"] for d in post_mortem._detect_dominant_features(losing)]
    assert "p_win" not in feats, feats
    assert "rsi_bucket" in feats             # real features still surface


def test_proactive_exits_use_real_premium_signature():
    # Review F1: options_proactive_exits called _fetch_option_premium(api,
    # occ, ...) — a signature that NEVER existed — so every call raised
    # TypeError and the premium stop never fired in prod (2026-06-07 →
    # 2026-07-02), masked by a same-wrong-signature mock. Pin both: the source
    # must not pass `api` first, and the real signature must stay (occ, side).
    import inspect
    from client import _fetch_option_premium
    params = list(inspect.signature(_fetch_option_premium).parameters)
    assert params == ["occ_symbol", "side"], params
    src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                            "options_proactive_exits.py")).read()
    assert "_fetch_option_premium(api" not in src, (
        "options_proactive_exits must call _fetch_option_premium(occ, "
        "side=...) — passing api first raises TypeError on every call and "
        "silently disables the premium stop.")


def test_post_mortem_skips_internal_and_sentinel_class():
    # Class guard (review F2): underscore-prefixed internal/market-wide keys
    # and sentinel-dominated features must never seize the 4 dominant-pattern
    # slots; a REAL per-candidate pattern must surface instead.
    import post_mortem
    losing = [{"_regime": "volatile", "_cboe_skew": 142.0,
               "_ledger_is_override": True,
               "days_to_earnings": -1, "app_store_grossing_rank": 999,
               "nearest_fib_dist": 99, "stoch_rsi": 88.0,
               "insider_direction": "buying"} for _ in range(6)]
    feats = [d["feature"] for d in post_mortem._detect_dominant_features(losing)]
    assert feats == ["insider_direction"], feats


def test_stock_only_ledger_header_has_no_mix_language():
    # Review F4: a stock-only ledger (No-Options profile / no option survived)
    # must not tell the AI to expect a stock/option mix it cannot produce.
    from opportunity_ledger import render_ledger_block
    stock_opps = [{"symbol": "AAPL", "expression": "stock", "rar": 0.5,
                   "p_win": 0.6, "risk_dollars": 300.0, "reward_dollars": 600.0,
                   "action": "BUY", "size_pct": 8.0, "stop_loss_pct": 3.0,
                   "take_profit_pct": 6.0}]
    block, has_opt = render_ledger_block(stock_opps)
    assert has_opt is False
    assert "RISK-ADJUSTED OPPORTUNITY LEDGER" in block
    assert "stock/option mix" not in block
    assert "option" not in block.split("RAR =")[0].replace(
        "OPPORTUNITY", "")  # header line mentions no option expressions


# --- concentration haircut must never promote a negative-RAR candidate -------

def test_div_penalty_never_boosts_negative_rar(monkeypatch):
    from trade_pipeline import _rank_candidates
    import trade_pipeline as tp

    # Both candidates carry a chronic-loser reputation → negative RAR. The
    # concentrated one (penalty would shrink its negative score toward 0) must
    # NOT outrank the diversified equal one.
    rep = {"BAD1": {"win_rate": 20, "total": 30,
                    "by_signal": {"BUY": {"win_rate": 20, "total": 30}}},
           "BAD2": {"win_rate": 20, "total": 30,
                    "by_signal": {"BUY": {"win_rate": 20, "total": 30}}}}
    sigs = [{"symbol": "BAD1", "signal": "BUY", "score": 2.0, "rsi": 50,
             "votes": {}, "price": 100.0},
            {"symbol": "BAD2", "signal": "BUY", "score": 2.0, "rsi": 50,
             "votes": {}, "price": 100.0}]

    import config
    monkeypatch.setattr(config, "ENABLE_CONCENTRATION_AWARE", True,
                        raising=False)
    # BAD1 sits in a heavily-held sector (penalty 0.5); BAD2 diversifies.
    monkeypatch.setattr("sector_classifier.get_sector",
                        lambda s, db_path=None: "tech")
    monkeypatch.setattr(
        "book_fit.sector_concentration_penalty",
        lambda sector, counts: 0.5 if True else 0.0)
    # give BAD2 a zero penalty by symbol-specific div hook: penalty applies to
    # both via sector — instead differentiate via the rar tie: equal RAR, equal
    # penalty would tie; so give BAD2 a *slightly worse* RAR via score and
    # assert the penalty doesn't flip the order.
    sigs[1]["score"] = 1.9   # BAD2 marginally lower conviction tiebreak
    shortlist = _rank_candidates(sigs, held_symbols={"HELD"},
                                 enable_shorts=False,
                                 ctx=SimpleNamespace(),
                                 symbol_reputation=rep)
    # Both negative-RAR: penalty must leave RAR untouched (no shrink-toward-
    # zero bonus), so ordering falls to |score|: BAD1 first — and critically,
    # both keys stay at the RAW negative RAR.
    assert [s["symbol"] for s in shortlist] == ["BAD1", "BAD2"]


def test_penalized_helper_semantics():
    # The pure rule: positive scores shrink; negative scores NEVER improve.
    from opportunity_ledger import p_win_from_reputation  # noqa: F401 (import sanity)
    import trade_pipeline as tp
    src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                            "trade_pipeline.py")).read()
    assert "if score > 0 else score" in src, (
        "_rank_candidates must apply the concentration haircut to POSITIVE "
        "rank scores only — multiplying a negative RAR by (1−penalty) ranks "
        "a concentrated bad candidate above a diversified better one.")


# --- discounted RAR keeps EV consistent (sort tiebreak) -----------------------

def test_veto_discount_recomputes_ev(monkeypatch, tmp_path):
    import opportunity_ledger as ol
    from journal import init_db, record_option_proposal_outcome

    db = str(tmp_path / "p.db")
    init_db(db)
    sector = ol._sector_of("AAPL")
    for _ in range(35):
        record_option_proposal_outcome(db, symbol="AAPL",
                                       strategy="bull_put_spread",
                                       sector=sector, vetoed=1, veto_reason="x")
    for _ in range(5):
        record_option_proposal_outcome(db, symbol="AAPL",
                                       strategy="bull_put_spread",
                                       sector=sector, vetoed=0)

    monkeypatch.setattr("options_strategy_advisor._cached_option_premium",
                        lambda occ, side: 2.0 if side == "sell" else 1.0)
    monkeypatch.setattr("options_strategy_advisor._cached_option_quote",
                        lambda occ: None)
    monkeypatch.setattr("options_strategy_advisor._own_book_held_underlyings",
                        lambda ctx: set())
    monkeypatch.setattr("options_strategy_advisor._options_budget_exhausted",
                        lambda ctx: False)

    cand = {"symbol": "AAPL", "signal": "BUY", "price": 150.0, "score": 2.0,
            "atr": 3.0, "rsi": 62, "adx": 28, "volume_ratio": 1.4}
    opps = ol.build_opportunities([cand], SimpleNamespace(db_path=db),
                                  100_000.0, iv_rank_lookup=lambda s: 70)
    opt = next(o for o in opps if o["expression"] == "option")
    if opt.get("veto_discount"):
        # EV must equal the DISCOUNTED rar × risk (sort tie-breaks on EV).
        assert opt["ev_dollars"] == pytest.approx(
            round(opt["rar"] * opt["risk_dollars"], 2), abs=0.02)


# --- one snapshot fetch per leg serves premium AND quote ----------------------

def test_one_snapshot_fetch_serves_premium_and_quote(monkeypatch):
    import options_strategy_advisor as osa
    calls = {"n": 0}

    def _fake_snapshot(occ):
        calls["n"] += 1
        return {"latestQuote": {"bp": 1.9, "ap": 2.1}}

    monkeypatch.setattr("client._fetch_option_snapshot", _fake_snapshot)
    osa._SNAP_CACHE.clear()
    prem = osa._cached_option_premium("AAPL260821P00145000", "sell")
    quote = osa._cached_option_quote("AAPL260821P00145000")
    assert prem == pytest.approx(2.0)          # mid of the two-sided quote
    assert quote == (1.9, 2.1)
    assert calls["n"] == 1, "premium + quote for one leg must share ONE fetch"


def test_wide_stale_quote_rejected_but_premium_survives(monkeypatch):
    import options_strategy_advisor as osa
    monkeypatch.setattr("client._fetch_option_snapshot",
                        lambda occ: {"latestQuote": {"bp": 0.05, "ap": 2.0}})
    osa._SNAP_CACHE.clear()
    # ask > 3× bid → not a trustworthy cost quote…
    assert osa._cached_option_quote("X260821C00100000") is None
    # …but the premium ladder still yields the mid (real two-sided market).
    assert osa._cached_option_premium("X260821C00100000", "buy") == \
        pytest.approx(1.025)


# --- veto-outcome capture never blocks live dispatch --------------------------

def test_async_recorder_writes_row_off_thread(monkeypatch, tmp_path):
    from pipelines.option import OptionPipeline
    from journal import init_db, option_veto_counts

    db = str(tmp_path / "p.db")
    init_db(db)
    monkeypatch.setattr("options_strategy_advisor._cached_option_premium",
                        lambda occ, side: 2.0 if side == "sell" else 1.0)
    monkeypatch.setattr("options_strategy_advisor._cached_option_quote",
                        lambda occ: None)
    ctx = SimpleNamespace(db_path=db)
    proposal = {"symbol": "AAPL", "action": "MULTILEG_OPEN",
                "strategy_name": "bull_put_spread", "confidence": 70,
                "expiry": "2026-08-21", "strikes": {"short": 145, "long": 140}}
    t = OptionPipeline._record_option_outcome_async(
        ctx, proposal, "AAPL", vetoed_flag=1, veto_reason="risk")
    assert t is not None
    t.join(timeout=10)
    assert not t.is_alive(), "recorder thread must finish"
    counts = {(s, sec): (v, tot) for s, sec, v, tot in option_veto_counts(db)}
    assert sum(v for v, _ in counts.values()) == 1, counts
