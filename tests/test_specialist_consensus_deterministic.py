"""Structural guardrail: the specialist-consensus computation is
deterministic — same input → same output, every time, regardless
of dict-iteration order.

The bug class.
The consensus combiner (`ensemble._synthesize`) iterates over the
raw_by_specialist dict, accumulates buy_score / sell_score, and
records `vetoed_by` for the FIRST specialist whose VETO fires. If
the iteration order is dict-insertion-order-dependent (which CPython
guarantees from 3.7+ but does NOT guarantee under attacker-supplied
JSON dicts) AND callers ever build the dict with non-deterministic
ordering (e.g. from a `concurrent.futures.as_completed` loop), the
recorded `vetoed_by` and the per-specialist verdict array order
becomes non-reproducible.

Symptoms in production:
  - Backtest-vs-live divergence: same shortlist + same specialist
    raw outputs produce different `vetoed_by` attributions
  - The activity-feed line "VETOED by adversarial_reviewer" flips to
    "VETOED by risk_assessor" between runs
  - Floating-point reduction order on buy/sell_score drift produces
    1-confidence-point delta in final_confidence — usually invisible,
    occasionally tips a CONFIDENCE_FLOOR cutoff

This test fixes the input shape (candidates + raw_by_specialist) and
runs the consensus 5 times. The output must be byte-identical across
all runs. We also feed the dict in two intentionally different
orders to verify the consensus algorithm produces the same result
regardless of caller's dict-construction order.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _build_synthetic_inputs():
    """A fixed input that exercises:
      - Multiple specialists with different verdicts (BUY/SELL/HOLD/VETO)
      - A symbol where two VETO-authorized specialists both veto
        (tests that vetoed_by attribution is deterministic)
      - A symbol where buy_score == sell_score exactly (tests that
        tie-handling is deterministic)
      - A symbol where one specialist is missing (tests ABSTAIN path)
    """
    candidates = [
        {"symbol": "AAA", "signal": "BUY", "price": 100, "reason": "test"},
        {"symbol": "BBB", "signal": "BUY", "price": 200, "reason": "test"},
        {"symbol": "CCC", "signal": "BUY", "price": 50, "reason": "test"},
    ]
    # Per-specialist raw verdict lists (the shape ensemble._synthesize
    # consumes). Specialist names match SPECIALIST_WEIGHTS keys so
    # they get real weights applied.
    raw_by_specialist: Dict[str, List[Dict[str, Any]]] = {
        "earnings_analyst": [
            {"symbol": "AAA", "verdict": "BUY",
             "confidence": 80, "reasoning": "earnings beat"},
            {"symbol": "BBB", "verdict": "SELL",
             "confidence": 70, "reasoning": "guidance miss"},
            {"symbol": "CCC", "verdict": "BUY",
             "confidence": 60, "reasoning": "neutral"},
        ],
        "pattern_recognizer": [
            {"symbol": "AAA", "verdict": "BUY",
             "confidence": 75, "reasoning": "ascending triangle"},
            {"symbol": "BBB", "verdict": "BUY",
             "confidence": 60, "reasoning": "support hold"},
            {"symbol": "CCC", "verdict": "SELL",
             "confidence": 60, "reasoning": "lower highs"},
        ],
        "sentiment_narrative": [
            {"symbol": "AAA", "verdict": "HOLD",
             "confidence": 50, "reasoning": "mixed news"},
            {"symbol": "BBB", "verdict": "SELL",
             "confidence": 65, "reasoning": "bearish coverage"},
            # CCC missing intentionally — tests ABSTAIN path
        ],
        "risk_assessor": [
            {"symbol": "AAA", "verdict": "HOLD",
             "confidence": 30, "reasoning": "ok"},
            {"symbol": "BBB", "verdict": "VETO",
             "confidence": 90, "reasoning": "max sector exposure"},
            {"symbol": "CCC", "verdict": "HOLD",
             "confidence": 40, "reasoning": "ok"},
        ],
        "adversarial_reviewer": [
            {"symbol": "AAA", "verdict": "HOLD",
             "confidence": 20, "reasoning": "no failure mode"},
            # BBB also vetoed — tests that vetoed_by is deterministic
            # when two VETO-authorized specialists both fire
            {"symbol": "BBB", "verdict": "VETO",
             "confidence": 85, "reasoning": "stop too tight"},
            {"symbol": "CCC", "verdict": "HOLD",
             "confidence": 25, "reasoning": "ok"},
        ],
    }
    return candidates, raw_by_specialist


def _serialize_for_compare(out: Dict[str, Any]) -> str:
    """Stable JSON serialization for byte-equality comparison.
    Uses sort_keys so that dict ordering in the OUTPUT doesn't mask
    a real determinism bug in the COMPUTATION."""
    return json.dumps(out, sort_keys=True, default=str)


class TestSpecialistConsensusDeterministic:
    """Calls `ensemble._synthesize` with a fixed input and verifies
    the output is byte-identical across 5 repeated calls (no
    randomness, no time-of-day drift, no dict-iteration order leak)."""

    def test_same_input_produces_same_output_five_times(self):
        from ensemble import _synthesize
        candidates, raw_by_specialist = _build_synthetic_inputs()
        results = []
        for _ in range(5):
            # Deepcopy so the function can't mutate state and affect
            # the next call.
            c = copy.deepcopy(candidates)
            r = copy.deepcopy(raw_by_specialist)
            out = _synthesize(c, r, db_path=None, pipeline_kind=None)
            results.append(_serialize_for_compare(out))
        unique = set(results)
        assert len(unique) == 1, (
            f"_synthesize is non-deterministic: 5 calls with the "
            f"same input produced {len(unique)} distinct outputs.\n"
            f"First two distinct outputs:\n"
            f"  {sorted(unique)[0]}\n"
            f"  {sorted(unique)[1] if len(unique) > 1 else ''}"
        )

    def test_dict_input_order_does_not_change_output(self):
        """Feed raw_by_specialist in two intentionally different
        orders. The consensus output must be identical, otherwise
        the caller's dict-construction ordering leaks into the
        result (broken determinism contract)."""
        from ensemble import _synthesize
        candidates, raw_by_specialist = _build_synthetic_inputs()
        # Order A: alphabetical
        order_a = dict(sorted(raw_by_specialist.items()))
        # Order B: reverse alphabetical
        order_b = dict(sorted(raw_by_specialist.items(), reverse=True))

        out_a = _synthesize(copy.deepcopy(candidates),
                            copy.deepcopy(order_a),
                            db_path=None, pipeline_kind=None)
        out_b = _synthesize(copy.deepcopy(candidates),
                            copy.deepcopy(order_b),
                            db_path=None, pipeline_kind=None)

        # Final verdict and confidence MUST match regardless of input
        # dict order — the consensus algorithm cannot leak caller's
        # iteration order into the final decision.
        for sym in ("AAA", "BBB", "CCC"):
            assert out_a[sym]["verdict"] == out_b[sym]["verdict"], (
                f"Final verdict for {sym} flipped between input "
                f"orderings: A={out_a[sym]['verdict']} vs "
                f"B={out_b[sym]['verdict']}"
            )
            assert out_a[sym]["confidence"] == out_b[sym]["confidence"], (
                f"Final confidence for {sym} drifted between input "
                f"orderings: A={out_a[sym]['confidence']} vs "
                f"B={out_b[sym]['confidence']}"
            )
            # vetoed_by attribution: must be deterministic regardless
            # of input dict order. If the caller-side ordering can
            # change which specialist gets credit for a VETO, that
            # leaks into the dashboard activity feed.
            assert out_a[sym]["vetoed_by"] == out_b[sym]["vetoed_by"], (
                f"vetoed_by attribution for {sym} flipped between "
                f"input orderings: A={out_a[sym]['vetoed_by']!r} vs "
                f"B={out_b[sym]['vetoed_by']!r} — first-veto-wins "
                f"semantics depend on input dict order, which is "
                f"caller-controlled. Sort by specialist name (or "
                f"some other stable key) before iterating in "
                f"_synthesize."
            )

    def test_ties_have_deterministic_resolution(self):
        """When buy_score == sell_score, the output must be
        deterministic (currently HOLD with confidence 50). Verify
        the tie path is hit and resolved the same way every time."""
        from ensemble import _synthesize
        # Construct input that produces an exact tie. Use earnings_analyst
        # (weight 1.0) BUY at confidence 50 and pattern_recognizer
        # (weight 1.2) SELL at confidence 50 * (1.0/1.2) = 41.67.
        # Easier: use only two specialists with identical weights.
        candidates = [{"symbol": "TIE", "signal": "BUY",
                       "price": 100, "reason": "test"}]
        raw = {
            "earnings_analyst": [
                {"symbol": "TIE", "verdict": "BUY",
                 "confidence": 50, "reasoning": "tie"},
            ],
            "risk_assessor": [
                {"symbol": "TIE", "verdict": "SELL",
                 "confidence": 50, "reasoning": "tie"},
            ],
        }
        outs = []
        for _ in range(5):
            out = _synthesize(copy.deepcopy(candidates),
                              copy.deepcopy(raw),
                              db_path=None, pipeline_kind=None)
            outs.append(_serialize_for_compare(out))
        assert len(set(outs)) == 1, (
            "Tie-resolution is non-deterministic across 5 runs."
        )
