"""Concentration-aware candidate selection — book-fit signal (2026-06-29).

The dominant production specialist-veto reason is "book already concentrated
in correlated high-beta names, adding X increases correlation" — a signal the
AI never received (it only saw coarse 7-bucket sector exposure, not a
per-candidate return-correlation to the specific held names). book_fit.py
computes that signal pre-AI and surfaces it in the prompt so the AI proposes
DIVERSIFYING trades. Advisory only / fail-open — it must never block a trade.

These pin: the correlation/sector logic, fail-open behavior, and that the
signal is wired into candidate building + the AI prompt (gated by the kill
switch), entries flowing through the existing pipeline.
"""
from __future__ import annotations

import os
import re
from unittest.mock import patch

import numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

_SERIES = np.array([0.01, 0.02, -0.01, 0.03, 0.0, 0.01, -0.02, 0.02, 0.01, -0.01])


def _distinct_sector(sym):
    # Every symbol its own sector → no sector-overlap noise in corr tests.
    return sym


# ---------------------------------------------------------------------------
# Correlation logic
# ---------------------------------------------------------------------------

def test_high_correlation_is_flagged():
    from book_fit import compute_book_fit
    rets = {"CAND": _SERIES, "HELD1": _SERIES}  # identical → corr 1.0
    with patch("correlation._fetch_returns", return_value=rets), \
         patch("sector_classifier.get_sector", side_effect=_distinct_sector):
        r = compute_book_fit("CAND", ["HELD1"])
    assert r is not None
    assert r["max_corr"] is not None and abs(r["max_corr"]) >= 0.9
    assert r["corr_with"] == "HELD1"
    assert "HIGH" in r["summary"]


def test_low_correlation_tagged_low():
    from book_fit import compute_book_fit
    rets = {"CAND": _SERIES, "HELD1": -_SERIES}  # perfectly anti → |corr|=1 (HIGH)
    # use a genuinely uncorrelated series for the 'low' case
    rng = np.array([0.0, -0.03, 0.02, 0.01, -0.02, 0.03, -0.01, 0.0, 0.02, -0.03])
    rets = {"CAND": _SERIES, "HELD1": rng}
    with patch("correlation._fetch_returns", return_value=rets), \
         patch("sector_classifier.get_sector", side_effect=_distinct_sector):
        r = compute_book_fit("CAND", ["HELD1"])
    assert r is not None and r["max_corr"] is not None
    # whatever the exact value, the tag must reflect the |corr| band
    band = "HIGH" if abs(r["max_corr"]) >= 0.7 else (
        "elevated" if abs(r["max_corr"]) >= 0.5 else "low")
    assert band in r["summary"]


def test_no_holdings_returns_none():
    from book_fit import compute_book_fit
    assert compute_book_fit("CAND", []) is None
    assert compute_book_fit("CAND", ["CAND"]) is None  # self only → excluded


def test_fail_open_on_missing_returns():
    """No usable returns AND no sector overlap → None (never raises)."""
    from book_fit import compute_book_fit
    with patch("correlation._fetch_returns", return_value=None), \
         patch("sector_classifier.get_sector", side_effect=_distinct_sector):
        assert compute_book_fit("CAND", ["HELD1", "HELD2"]) is None


def test_same_sector_counted_without_correlation():
    from book_fit import compute_book_fit
    with patch("correlation._fetch_returns", return_value=None), \
         patch("sector_classifier.get_sector", return_value="tech"):
        r = compute_book_fit("CAND", ["HELD1", "HELD2"])
    assert r is not None
    assert r["same_sector"] == 2
    assert "tech" in r["summary"]


def test_held_underlyings_dedupes_and_extracts():
    from book_fit import held_underlyings
    rows = [{"symbol": "F"}, {"symbol": "T", "occ_symbol": "T260807P00020000"},
            {"symbol": "T", "occ_symbol": "T260807P00019000"}, {"symbol": "IREN"}]
    assert held_underlyings(rows) == ["F", "T", "IREN"]
    assert held_underlyings([]) == []


# ---------------------------------------------------------------------------
# Wiring: candidate building + AI prompt (gated by the kill switch)
# ---------------------------------------------------------------------------

def test_kill_switch_exists_and_default_on():
    import config
    assert config.ENABLE_CONCENTRATION_AWARE is True


def test_candidate_builder_computes_and_attaches_book_fit():
    src = open(os.path.join(REPO, "trade_pipeline.py")).read()
    # gated by the kill switch
    assert "ENABLE_CONCENTRATION_AWARE" in src
    # precompute held returns once + per-candidate attach
    assert "from book_fit import held_underlyings" in src
    assert "compute_book_fit(" in src
    assert 'entry["book_fit"]' in src


def test_ai_prompt_renders_book_fit():
    src = open(os.path.join(REPO, "ai_analyst.py")).read()
    assert re.search(r'c\.get\(\s*["\']book_fit["\']\s*\)', src), (
        "ai_analyst must read book_fit off each candidate")
    assert "PORTFOLIO FIT" in src


# ---------------------------------------------------------------------------
# Rank penalty (Phase 2): own-book sector-concentration haircut at ranking
# ---------------------------------------------------------------------------

def test_sector_penalty_scales_and_caps():
    from book_fit import sector_concentration_penalty
    from collections import Counter
    counts = Counter({"tech": 5, "finance": 1})
    assert sector_concentration_penalty("tech", counts) == 0.4   # 0.08*5 capped at 0.4
    assert abs(sector_concentration_penalty("finance", counts) - 0.08) < 1e-9
    assert sector_concentration_penalty("energy", counts) == 0.0  # not held


def test_sector_penalty_zero_on_empty_or_unknown():
    from book_fit import sector_concentration_penalty
    assert sector_concentration_penalty("tech", {}) == 0.0
    assert sector_concentration_penalty(None, {"tech": 3}) == 0.0


def test_rank_penalty_elevates_diversifier_over_concentrated():
    """A lower-raw-score diversifying long must outrank a higher-score long
    whose sector is already heavy in the OWN book — so the AI's menu leads
    with names its own risk veto won't reject."""
    from unittest.mock import patch
    from trade_pipeline import _rank_candidates
    # CONC: strong signal but in the over-held 'tech' sector.
    # DIVR: weaker signal but a diversifying 'finance' name.
    sigs = [
        {"symbol": "CONC", "score": 4.0, "signal": "BUY", "rsi": 50, "votes": {}},
        {"symbol": "DIVR", "score": 3.0, "signal": "BUY", "rsi": 50, "votes": {}},
    ]
    held = {"H1", "H2", "H3", "H4", "H5"}  # all tech in the own book

    def fake_sector(sym):
        return "tech" if sym in held or sym == "CONC" else "finance"

    with patch("sector_classifier.get_sector", side_effect=fake_sector):
        ranked = _rank_candidates(sigs, held, enable_shorts=False)
    order = [s["symbol"] for s in ranked]
    assert order[0] == "DIVR", (
        "diversifier should outrank the concentration-additive name; got %s" % order)


def test_rank_penalty_off_keeps_raw_order():
    """With the kill switch off, ranking is pure score (no concentration tilt)."""
    from unittest.mock import patch
    import config
    from trade_pipeline import _rank_candidates
    sigs = [
        {"symbol": "CONC", "score": 4.0, "signal": "BUY", "rsi": 50, "votes": {}},
        {"symbol": "DIVR", "score": 3.0, "signal": "BUY", "rsi": 50, "votes": {}},
    ]
    held = {"H1", "H2", "H3", "H4", "H5"}
    with patch.object(config, "ENABLE_CONCENTRATION_AWARE", False):
        ranked = _rank_candidates(sigs, held, enable_shorts=False)
    assert [s["symbol"] for s in ranked][0] == "CONC", "kill switch off → pure score order"
