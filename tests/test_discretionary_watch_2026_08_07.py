"""No-rule-fired names are presented as tradeable — 2026-08-07.

Operator: "fix the conviction/confidence problem too."

**The measurement that redirected this fix.** 78.2% of zero-selection
cycles cited conviction/confidence, which reads like AI timidity. It was
not. Checking the menus instead of the wording:

    candidate |score| across all 8,744 shortlisted names:  median 0.0
    best candidate on cycles where the AI TRADED:          median 3.0
    best candidate on cycles where the AI PASSED:          median 0.0

The AI trades when a real candidate exists and passes when the menu is
all zeros. That is correct behaviour, not timidity.

**Why the menus were zeros.** 69% of every candidate shown to the AI
was a `_apply_menu_floor` backfill entry — no deterministic rule fired,
`signal="NEUTRAL"`, `score=0` — and **43.2% of cycles had a menu that
was 100% of them** (748 of 1,733). On those the AI passed 714 times.
That single bucket is ~59% of all zero-selection cycles.

**The defect was presentation, not judgment.** Those entries were being
appended to the section headed "LONG CANDIDATES (ranked by technical
score)". The AI read a long-ideas list full of score-0 non-ideas and
declined — exactly as written. Yet `_apply_menu_floor` always intended
them to be tradeable; its own reason string says "surfaced by indicator
extremity for AI judgment". Nothing in the prompt ever said so.

They now render in their own DISCRETIONARY WATCH section stating that
NEUTRAL/0 means no RULE fired, not that there is no opportunity.

Fourth mechanism found in one day quietly converting to "don't trade",
after the Kelly sizing anchor, the crisis-state false positive, and the
confidence-bar inversion before them.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ai_analyst import (  # noqa: E402
    _discretionary_block, _is_discretionary_candidate,
)


def _db(tmp_path, n=10):
    from journal import init_db
    db = str(tmp_path / "p.db")
    init_db(db)
    conn = sqlite3.connect(db)
    for i in range(n):
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "                    signal_type, status) VALUES (?,?,?,?,?,?,?)",
            (f"2026-08-0{i % 9 + 1}T10:00", f"TICK{i}", "buy", 100, 50.0,
             "BUY", "closed"))
    conn.commit()
    conn.close()
    return db


def _ctx(db_path, enable_shorts=False):
    return SimpleNamespace(
        db_path=db_path, max_position_pct=0.10, max_total_positions=10,
        enable_options=False, enable_short_selling=enable_shorts,
        ai_confidence_threshold=45, segment="stocks",
        target_short_pct=0.0, short_max_position_pct=0.05,
    )


def _floor(sym, price=100.0):
    return {"symbol": sym, "signal": "NEUTRAL", "score": 0, "price": price,
            "votes": {}, "_menu_floor": True,
            "reason": "menu floor: no deterministic trigger this cycle"}


def _real(sym, sig="BUY", score=3, price=100.0):
    return {"symbol": sym, "signal": sig, "score": score, "price": price,
            "votes": {"rsi_reversal": sig}}


def _candidate_sections(prompt):
    """Split the prompt's candidate listing into (triggered, watch).

    Tickers also appear in sector/rotation context elsewhere, so
    assertions must be scoped to the listing itself.
    """
    start = prompt.index("CANDIDATES")
    end = prompt.index("\nRULES:", start)
    body = prompt[start:end]
    marker = "DISCRETIONARY WATCH"
    if marker in body:
        i = body.index(marker)
        return body[:i], body[i:]
    return body, ""


def _prompt(tmp_path, cands, enable_shorts=False):
    from ai_analyst import _build_batch_prompt
    return _build_batch_prompt(
        cands,
        {"equity": 250_000, "cash": 125_000, "num_positions": 3,
         "peak_equity": 250_000, "positions": []},
        {"regime": "neutral", "vix": 18.0, "spy_trend": "flat"},
        ctx=_ctx(_db(tmp_path), enable_shorts),
    )


class TestClassification:
    def test_menu_floor_marker_is_discretionary(self):
        assert _is_discretionary_candidate(_floor("MSFT"))

    def test_a_real_signal_is_not(self):
        assert not _is_discretionary_candidate(_real("AAPL"))

    def test_falls_back_to_shape_when_marker_is_stripped(self):
        """`cycle_data["shortlist"]` serializes an explicit key
        whitelist that drops `_menu_floor`, so the marker cannot be
        relied on everywhere."""
        stripped = {"symbol": "X", "signal": "NEUTRAL", "score": 0,
                    "votes": {}}
        assert _is_discretionary_candidate(stripped)

    def test_neutral_WITH_votes_is_not_discretionary(self):
        """A strategy that voted and landed on NEUTRAL is a real
        evaluation, not backfill."""
        voted = {"symbol": "X", "signal": "NEUTRAL", "score": 0,
                 "votes": {"macd": "HOLD"}}
        assert not _is_discretionary_candidate(voted)

    def test_non_dict_is_safe(self):
        assert not _is_discretionary_candidate(None)
        assert not _is_discretionary_candidate("AAPL")


class TestBlockSaysWhatTheyAre:
    def test_states_that_neutral_means_no_rule_fired(self):
        b = _discretionary_block(["  1. MSFT"])
        assert "NO RULE FIRED" in b
        assert "does NOT mean there is no opportunity" in b

    def test_invites_a_trade_rather_than_only_permitting_one(self):
        """The lesson from the sizing anchor and the crisis state: a
        section that only says what is allowed reads as discouragement.
        It has to say what good looks like."""
        b = _discretionary_block(["  1. MSFT"])
        assert "take it" in b
        assert "learns only from positions it actually holds" in b


class TestPromptSeparatesTheTwoLists:
    def test_real_signals_and_discretionary_are_in_different_sections(
            self, tmp_path):
        p = _prompt(tmp_path, [_real("AAPL"), _floor("MSFT")])
        assert "DISCRETIONARY WATCH" in p
        # Scope to the candidate listing itself — both tickers also
        # appear elsewhere in the prompt (sector/rotation context), so
        # a bare p.index() would match the wrong occurrence.
        triggered, watch = _candidate_sections(p)
        assert "AAPL" in triggered and "AAPL" not in watch
        assert "MSFT" in watch and "MSFT" not in triggered

    def test_no_watch_section_when_every_candidate_is_real(self, tmp_path):
        p = _prompt(tmp_path, [_real("AAPL"), _real("MSFT", score=4)])
        assert "DISCRETIONARY WATCH" not in p

    def test_all_discretionary_menu_still_renders_them(self, tmp_path):
        """The 43.2%-of-cycles case. An all-backfill menu must still
        reach the AI as a real, actionable list."""
        p = _prompt(tmp_path, [_floor("MSFT"), _floor("NVDA")])
        assert "DISCRETIONARY WATCH" in p
        assert "MSFT" in p and "NVDA" in p

    def test_discretionary_names_are_not_labelled_long_candidates(
            self, tmp_path):
        """The precise defect: they were appended to 'LONG CANDIDATES
        (ranked by technical score)', so a score-0 entry read as a bad
        long idea."""
        p = _prompt(tmp_path, [_floor("MSFT")], enable_shorts=True)
        triggered, watch = _candidate_sections(p)
        assert "MSFT" in watch
        assert "MSFT" not in triggered, (
            "a no-rule-fired name must not appear under LONG CANDIDATES"
        )

    def test_shorts_enabled_path_keeps_real_shorts_separate(self, tmp_path):
        p = _prompt(tmp_path, [_real("KO", sig="SELL"), _floor("MSFT")],
                    enable_shorts=True)
        assert "SHORT CANDIDATES" in p
        triggered, watch = _candidate_sections(p)
        assert "KO" in triggered and "KO" not in watch
        assert "MSFT" in watch
