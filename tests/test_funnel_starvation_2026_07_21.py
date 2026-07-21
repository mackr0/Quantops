"""2026-07-21 (evening) — two funnel-starvation fixes, found by the
corrected trade_drops query + the "SPCX was the only asset presented"
cycle.

1. CHRONIC-ZERO-WIN shortlist exclusion (_rank_candidates): a symbol
   with 0% win rate over >=10 resolved predictions stops being
   nominated to the AI at all. SPCX (0W/102L, a dead flat ticker whose
   RSI~50/ADX~0 profile permanently satisfies range-based entry rules)
   was presented as the ONLY candidate cycle after cycle — the AI
   passed every time, and the whole cycle produced nothing. The
   downstream blacklist gate (0% >= 3) stays for young losers so the
   learning loop survives; this cutoff only retires proven zombies.
   Held symbols are exempt — exits must always flow.

2. CRISIS GATE truthfulness + exit exemption (STEP 4.9): it was the
   last silent eater — a symbol-less log line and NO trade_drops
   record, and its keep-list was literally ("SELL", "SHORT"), which
   also discarded STRONG_SELL / MULTILEG_CLOSE exits (the stranded-
   position class). Blocked entries now log symbols and record
   CRISIS_GATE drops; the block-set is NEW_ENTRY_ACTIONS minus SHORT
   so exits are structurally exempt.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _sig(sym, action="BUY", score=1):
    return {"symbol": sym, "signal": action, "score": score, "votes": {}}


def _rep(total, win_rate):
    return {"wins": int(total * win_rate), "losses": total,
            "total": total, "win_rate": win_rate}


class TestChronicZeroWinExclusion:

    def test_zombie_excluded_from_shortlist(self):
        from trade_pipeline import _rank_candidates
        out = _rank_candidates(
            [_sig("SPCX")], held_symbols=set(), enable_shorts=False,
            ctx=None, symbol_reputation={"SPCX": _rep(102, 0.0)},
        )
        assert out == [], "0W/102L symbol must not reach the AI's menu"

    def test_young_loser_still_included(self):
        """0/9 stays visible — the downstream blacklist gate handles it
        and the learning loop keeps recording predictions."""
        from trade_pipeline import _rank_candidates
        out = _rank_candidates(
            [_sig("NEWB")], held_symbols=set(), enable_shorts=False,
            ctx=None, symbol_reputation={"NEWB": _rep(9, 0.0)},
        )
        assert [s["symbol"] for s in out] == ["NEWB"]

    def test_held_zombie_exit_still_flows(self):
        """A SELL on a HELD chronic loser is an exit — never excluded."""
        from trade_pipeline import _rank_candidates
        out = _rank_candidates(
            [_sig("SPCX", action="SELL")], held_symbols={"SPCX"},
            enable_shorts=False, ctx=None,
            symbol_reputation={"SPCX": _rep(102, 0.0)},
        )
        assert [s["symbol"] for s in out] == ["SPCX"]

    def test_any_win_keeps_symbol(self):
        from trade_pipeline import _rank_candidates
        out = _rank_candidates(
            [_sig("OK")], held_symbols=set(), enable_shorts=False,
            ctx=None, symbol_reputation={"OK": _rep(50, 0.10)},
        )
        assert [s["symbol"] for s in out] == ["OK"]

    def test_no_reputation_keeps_symbol(self):
        from trade_pipeline import _rank_candidates
        out = _rank_candidates(
            [_sig("FRESH")], held_symbols=set(), enable_shorts=False,
            ctx=None, symbol_reputation={},
        )
        assert [s["symbol"] for s in out] == ["FRESH"]

    def test_threshold_is_well_above_blacklist_gate(self):
        """The upstream cutoff must stay far above the downstream
        gate's >=3 so young losers keep their learning loop."""
        from trade_pipeline import CHRONIC_ZERO_WIN_MIN_ATTEMPTS
        assert CHRONIC_ZERO_WIN_MIN_ATTEMPTS >= 10


class TestCrisisGateStructure:

    def _crisis_block(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        idx = src.index("if crisis_size_multiplier <= 0:")
        return src[idx:idx + 3500]

    def test_crisis_gate_records_drops(self):
        block = self._crisis_block()
        assert "record_trade_drop" in block and "CRISIS_GATE" in block, (
            "the crisis gate is a silent eater again — blocked entries "
            "must record CRISIS_GATE drops")

    def test_crisis_gate_blocks_by_new_entry_actions(self):
        """The block-set derives from NEW_ENTRY_ACTIONS (minus SHORT),
        so exits (STRONG_SELL / MULTILEG_CLOSE / SELL) are structurally
        exempt — the old literal keep-list ('SELL', 'SHORT') stranded
        every other exit action."""
        block = self._crisis_block()
        assert "NEW_ENTRY_ACTIONS" in block
        assert '!= "SHORT"' in block

    def test_crisis_gate_logs_symbols(self):
        """The old log line was symbol-less — a blocked BUY vanished
        without a trace (the MS shape)."""
        block = self._crisis_block()
        assert "_blocked_syms" in block

    def test_crisis_gate_badge_is_cross_cutting(self):
        from views import _drop_action_class
        assert _drop_action_class("CRISIS_GATE", "") == "any"
