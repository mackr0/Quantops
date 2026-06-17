"""2026-06-17 — AI ranked-alternate substitution for shared-account
cross-profile conflicts.

13 virtual-account profiles share 3 Alpaca accounts. A trade one profile
wants is sometimes rejected because a SIBLING profile holds a conflicting
position on the same symbol/strike (Alpaca enforces position_intent /
cross-direction at the ACCOUNT level). Before this feature the blocked
trade just dropped and the slot was lost. Now, on such a conflict, the
dispatch loop executes the NEXT AI-vetted, AI-SIZED alternate instead.

Pieces under test:

  - ai_analyst._build_batch_prompt / _validate_ai_trades / ai_select_trades:
    the AI returns an "alternates" array (same schema as "trades"),
    validated by the identical rules, surfaced as result["alternates"].

  - trade_pipeline._is_cross_profile_conflict: detects ONLY the
    shared-account conflict signals (stock cross-direction + multileg
    position-intent collision) — NOT risk gates, blacklist,
    insufficient-buying-power, or sector halts.

  - trade_pipeline._select_backfill_alternate: pops the next eligible
    ranked alternate off the bench, skipping symbols already traded /
    attempted / held — the no-double-trade + never-trade-held invariants.

  - The dispatch loop in run_trade_cycle: merges alternates BEFORE the
    risk gates (so they're vetted identically), splits them into a deque
    right before the execution loop, and backfills on a cross-profile
    conflict via append-during-iteration. Pinned structurally + a
    behavioral simulation of the exact loop control flow.
"""

import ast
import collections
import inspect
import os
import unittest

import ai_analyst
import trade_pipeline
from trade_pipeline import (
    _is_cross_profile_conflict,
    _select_backfill_alternate,
)


# ---------------------------------------------------------------------------
# 1. _is_cross_profile_conflict — the substitution trigger predicate.
# ---------------------------------------------------------------------------
class TestIsCrossProfileConflict(unittest.TestCase):

    def test_stock_cross_direction_long_then_short(self):
        tr = {
            "symbol": "CWAN", "action": "SKIP",
            "reason": ("Alpaca rejected: cannot open a long buy while a "
                       "short sell order is open"),
        }
        self.assertTrue(_is_cross_profile_conflict(tr))

    def test_stock_cross_direction_short_then_long(self):
        tr = {
            "symbol": "VALE", "action": "SKIP",
            "reason": ("Alpaca rejected: cannot open a short sell while a "
                       "long buy order is open"),
        }
        self.assertTrue(_is_cross_profile_conflict(tr))

    def test_option_shared_account_strike_collision(self):
        tr = {
            "symbol": "AAL", "action": "SKIP",
            "reason": ("Shared-account strike collision on AAL260717C00014000: "
                       "the Alpaca account is net-short 3 there ..."),
        }
        self.assertTrue(_is_cross_profile_conflict(tr))

    def test_option_position_intent_mismatch(self):
        tr = {
            "symbol": "NOK", "action": "SKIP",
            "reason": ("Alpaca order rejected (422): position intent "
                       "mismatch, inferred: sell_to_close"),
        }
        self.assertTrue(_is_cross_profile_conflict(tr))

    def test_option_position_intent_hyphen_form(self):
        tr = {
            "symbol": "NOK", "action": "SKIP",
            "reason": "skipped pre-submit on a position-intent collision",
        }
        self.assertTrue(_is_cross_profile_conflict(tr))

    def test_case_insensitive(self):
        tr = {
            "symbol": "CWAN", "action": "SKIP",
            "reason": ("ALPACA REJECTED: CANNOT OPEN A LONG BUY WHILE A "
                       "SHORT SELL ORDER IS OPEN"),
        }
        self.assertTrue(_is_cross_profile_conflict(tr))

    # ---- the things that must NEVER trigger substitution ----

    def test_insufficient_buying_power_is_not_conflict(self):
        tr = {
            "symbol": "AAA", "action": "SKIP",
            "reason": "Alpaca rejected: insufficient buying power",
        }
        self.assertFalse(_is_cross_profile_conflict(tr))

    def test_blacklist_block_is_not_conflict(self):
        tr = {
            "symbol": "BBB", "action": "BLACKLIST_BLOCKED",
            "reason": ("AI wanted BUY but 0/4 win rate on resolved "
                       "predictions."),
        }
        self.assertFalse(_is_cross_profile_conflict(tr))

    def test_sector_halt_is_not_conflict(self):
        tr = {
            "symbol": "CCC", "action": "INTRADAY_RISK_HALT",
            "reason": "Sector 'Technology' halted: vol spike",
        }
        self.assertFalse(_is_cross_profile_conflict(tr))

    def test_successful_trade_is_not_conflict(self):
        tr = {"symbol": "DDD", "action": "BUY", "reason": ""}
        self.assertFalse(_is_cross_profile_conflict(tr))

    def test_generic_error_is_not_conflict(self):
        tr = {
            "symbol": "EEE", "action": "ERROR",
            "reason": "KeyError: 'price'",
        }
        self.assertFalse(_is_cross_profile_conflict(tr))

    def test_none_and_nondict_are_not_conflict(self):
        self.assertFalse(_is_cross_profile_conflict(None))
        self.assertFalse(_is_cross_profile_conflict("nope"))
        self.assertFalse(_is_cross_profile_conflict({}))


# ---------------------------------------------------------------------------
# 2. _select_backfill_alternate — the bench-pop decision.
# ---------------------------------------------------------------------------
class TestSelectBackfillAlternate(unittest.TestCase):

    def test_picks_highest_ranked_eligible(self):
        pool = collections.deque([
            {"symbol": "BBB", "confidence": 70},
            {"symbol": "CCC", "confidence": 60},
        ])
        alt = _select_backfill_alternate(pool, set(), set(), set())
        self.assertEqual(alt["symbol"], "BBB")
        # The runner-up stays on the bench for the next conflict.
        self.assertEqual([a["symbol"] for a in pool], ["CCC"])

    def test_skips_already_traded_symbol(self):
        pool = collections.deque([
            {"symbol": "BBB"}, {"symbol": "CCC"},
        ])
        alt = _select_backfill_alternate(pool, {"BBB"}, set(), set())
        self.assertEqual(alt["symbol"], "CCC")

    def test_skips_already_attempted_symbol(self):
        # An alternate duplicating a primary symbol must not be chosen.
        pool = collections.deque([
            {"symbol": "AAA"}, {"symbol": "CCC"},
        ])
        alt = _select_backfill_alternate(pool, set(), {"AAA"}, set())
        self.assertEqual(alt["symbol"], "CCC")

    def test_skips_held_symbol(self):
        pool = collections.deque([
            {"symbol": "HELD"}, {"symbol": "CCC"},
        ])
        alt = _select_backfill_alternate(pool, set(), set(), {"HELD"})
        self.assertEqual(alt["symbol"], "CCC")

    def test_returns_none_when_pool_exhausted(self):
        pool = collections.deque([{"symbol": "BBB"}])
        alt = _select_backfill_alternate(pool, {"BBB"}, set(), set())
        self.assertIsNone(alt)
        self.assertEqual(len(pool), 0)

    def test_returns_none_on_empty_pool(self):
        self.assertIsNone(
            _select_backfill_alternate(collections.deque(), set(), set(), set())
        )

    def test_duplicate_symbols_in_pool_attempted_at_most_once(self):
        # Two alternates with the same symbol: the first is returned; the
        # caller adds it to attempted_syms, so a second conflict skips the
        # duplicate and pops the next distinct symbol.
        pool = collections.deque([
            {"symbol": "DUP"}, {"symbol": "DUP"}, {"symbol": "EEE"},
        ])
        attempted = set()
        first = _select_backfill_alternate(pool, set(), attempted, set())
        self.assertEqual(first["symbol"], "DUP")
        attempted.add(first["symbol"])  # caller does this
        second = _select_backfill_alternate(pool, set(), attempted, set())
        self.assertEqual(second["symbol"], "EEE")


# ---------------------------------------------------------------------------
# 3. Behavioral: the dispatch loop's append-during-iteration backfill.
#
#    The real loop lives inside run_trade_cycle (too heavy to drive end to
#    end without mocking a dozen seams). This test reproduces the EXACT
#    control flow of that loop — using the REAL _is_cross_profile_conflict
#    and _select_backfill_alternate helpers and the same append-to-the-
#    iterated-list mechanism — and asserts the substitution invariants.
#    A structural pin (class 5) guarantees the source loop wires the same
#    helpers in the same order.
# ---------------------------------------------------------------------------
def _simulate_dispatch(primaries, alternates, outcome_for, held=None):
    """Mirror of run_trade_cycle's STEP-5 loop for the backfill path.

    `outcome_for(symbol) -> trade_result dict` stands in for execute_trade.
    Returns the ordered list of symbols that SUCCESSFULLY traded.
    """
    held_symbols = set(held or set())
    alt_pool = collections.deque(alternates)
    ai_trades = list(primaries)
    attempted_syms = {t["symbol"] for t in ai_trades}
    traded_syms = set()
    traded_order = []

    _SUCCESS_ACTIONS = ("BUY", "SELL", "SHORT", "COVER",
                        "MULTILEG_OPEN", "MULTILEG_CLOSE")

    for ai_trade in ai_trades:  # append-during-iteration (CPython)
        symbol = ai_trade["symbol"]
        trade_result = outcome_for(symbol)
        ta = (trade_result or {}).get("action")
        if ta in _SUCCESS_ACTIONS:
            traded_syms.add(symbol)
            traded_order.append(symbol)

        if _is_cross_profile_conflict(trade_result) and alt_pool:
            alt = _select_backfill_alternate(
                alt_pool, traded_syms, attempted_syms, held_symbols,
            )
            if alt is not None:
                attempted_syms.add(alt["symbol"])
                ai_trades.append(alt)  # picked up on the next turn

    return traded_order


_CONFLICT = {
    "action": "SKIP",
    "reason": ("Alpaca rejected: cannot open a long buy while a short "
               "sell order is open"),
}
_OK = {"action": "BUY", "reason": ""}
_IBP = {"action": "SKIP",
        "reason": "Alpaca rejected: insufficient buying power"}
_BLACKLIST = {"action": "BLACKLIST_BLOCKED", "reason": "0/3 win rate"}


class TestDispatchBackfillBehavior(unittest.TestCase):

    def test_alternate_traded_when_primary_blocked(self):
        # A is blocked by a cross-profile conflict; B (alternate) executes.
        outcomes = {"A": _CONFLICT, "B": _OK}
        traded = _simulate_dispatch(
            primaries=[{"symbol": "A"}],
            alternates=[{"symbol": "B"}],
            outcome_for=lambda s: outcomes[s],
        )
        self.assertEqual(traded, ["B"])

    def test_alternate_not_traded_when_primary_succeeds(self):
        outcomes = {"A": _OK, "B": _OK}
        traded = _simulate_dispatch(
            primaries=[{"symbol": "A"}],
            alternates=[{"symbol": "B"}],
            outcome_for=lambda s: outcomes[s],
        )
        self.assertEqual(traded, ["A"])

    def test_non_cross_profile_drop_does_not_substitute(self):
        # Insufficient buying power must NOT consume an alternate.
        outcomes = {"A": _IBP, "B": _OK}
        traded = _simulate_dispatch(
            primaries=[{"symbol": "A"}],
            alternates=[{"symbol": "B"}],
            outcome_for=lambda s: outcomes[s],
        )
        self.assertEqual(traded, [])

    def test_blacklist_drop_does_not_substitute(self):
        outcomes = {"A": _BLACKLIST, "B": _OK}
        traded = _simulate_dispatch(
            primaries=[{"symbol": "A"}],
            alternates=[{"symbol": "B"}],
            outcome_for=lambda s: outcomes[s],
        )
        self.assertEqual(traded, [])

    def test_alternate_equal_to_primary_symbol_not_double_traded(self):
        # A succeeds; an alternate ALSO on A must never be re-traded even
        # if some later primary conflicts.
        outcomes = {"A": _OK, "Z": _CONFLICT, "B": _OK}
        traded = _simulate_dispatch(
            primaries=[{"symbol": "A"}, {"symbol": "Z"}],
            alternates=[{"symbol": "A"}, {"symbol": "B"}],
            outcome_for=lambda s: outcomes[s],
        )
        # Z conflicts → alternate A is skipped (already traded) → B trades.
        self.assertEqual(traded, ["A", "B"])
        self.assertEqual(traded.count("A"), 1)

    def test_chained_substitution(self):
        # A conflicts → B pulled; B also conflicts → C pulled; C trades.
        outcomes = {"A": _CONFLICT, "B": _CONFLICT, "C": _OK}
        traded = _simulate_dispatch(
            primaries=[{"symbol": "A"}],
            alternates=[{"symbol": "B"}, {"symbol": "C"}],
            outcome_for=lambda s: outcomes[s],
        )
        self.assertEqual(traded, ["C"])

    def test_never_backfills_a_held_symbol(self):
        outcomes = {"A": _CONFLICT, "HELD": _OK, "C": _OK}
        traded = _simulate_dispatch(
            primaries=[{"symbol": "A"}],
            alternates=[{"symbol": "HELD"}, {"symbol": "C"}],
            outcome_for=lambda s: outcomes[s],
            held={"HELD"},
        )
        # HELD is skipped (already in the book); C backfills instead.
        self.assertEqual(traded, ["C"])

    def test_bounded_total_cannot_exceed_primaries_plus_alternates(self):
        # Every primary conflicts and every alternate trades: at most
        # len(primaries)+len(alternates) attempts, no infinite loop.
        outcomes = {
            "A": _CONFLICT, "B": _CONFLICT,
            "C": _OK, "D": _OK, "E": _OK,
        }
        traded = _simulate_dispatch(
            primaries=[{"symbol": "A"}, {"symbol": "B"}],
            alternates=[{"symbol": "C"}, {"symbol": "D"}, {"symbol": "E"}],
            outcome_for=lambda s: outcomes[s],
        )
        # 2 conflicts pull 2 alternates (C, D); E is never reached.
        self.assertEqual(traded, ["C", "D"])
        self.assertLessEqual(len(traded), 2 + 3)


# ---------------------------------------------------------------------------
# 4. ai_select_trades / _validate_ai_trades — alternates parse + validate.
# ---------------------------------------------------------------------------
class _Ctx:
    max_position_pct = 0.10
    enable_short_selling = False


class TestAlternatesValidation(unittest.TestCase):

    def test_alternates_parsed_and_returned(self):
        result = {
            "trades": [
                {"symbol": "AAA", "action": "BUY", "size_pct": 7.5,
                 "confidence": 80, "reasoning": "primary"},
            ],
            "alternates": [
                {"symbol": "BBB", "action": "BUY", "size_pct": 5.0,
                 "confidence": 65, "reasoning": "alt1"},
            ],
            "portfolio_reasoning": "x",
            "pass_this_cycle": False,
        }
        candidates = [{"symbol": "AAA"}, {"symbol": "BBB"}]
        out = ai_analyst._validate_ai_trades(result, candidates, ctx=_Ctx())
        self.assertIn("alternates", out)
        self.assertEqual([t["symbol"] for t in out["trades"]], ["AAA"])
        self.assertEqual([t["symbol"] for t in out["alternates"]], ["BBB"])

    def test_alternates_validated_by_same_rules(self):
        # Oversized alternate is capped; non-candidate alternate dropped —
        # exactly the rules applied to primary trades.
        result = {
            "trades": [{"symbol": "AAA", "action": "BUY", "size_pct": 7.5,
                        "confidence": 80, "reasoning": "p"}],
            "alternates": [
                {"symbol": "BBB", "action": "BUY", "size_pct": 99.0,
                 "confidence": 60, "reasoning": "oversized"},
                {"symbol": "ZZZ", "action": "BUY", "size_pct": 4.0,
                 "confidence": 50, "reasoning": "not a candidate"},
            ],
            "portfolio_reasoning": "x", "pass_this_cycle": False,
        }
        candidates = [{"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "CCC"}]
        out = ai_analyst._validate_ai_trades(result, candidates, ctx=_Ctx())
        alt_syms = [t["symbol"] for t in out["alternates"]]
        self.assertEqual(alt_syms, ["BBB"])  # ZZZ dropped (not a candidate)
        # 99% capped to max_position_pct (10%) — same cap as primaries.
        self.assertEqual(out["alternates"][0]["size_pct"], 10.0)

    def test_missing_alternates_defaults_empty(self):
        result = {
            "trades": [{"symbol": "AAA", "action": "BUY", "size_pct": 5.0,
                        "confidence": 70, "reasoning": "p"}],
            "portfolio_reasoning": "x", "pass_this_cycle": False,
        }
        out = ai_analyst._validate_ai_trades(
            result, [{"symbol": "AAA"}], ctx=_Ctx(),
        )
        self.assertEqual(out["alternates"], [])

    def test_pass_cycle_returns_empty_alternates(self):
        result = {"trades": [], "pass_this_cycle": True,
                  "portfolio_reasoning": "pass"}
        out = ai_analyst._validate_ai_trades(
            result, [{"symbol": "AAA"}], ctx=_Ctx(),
        )
        self.assertEqual(out["alternates"], [])

    def test_prompt_instructs_alternates_array(self):
        # The batch prompt must teach the AI to return an "alternates"
        # array used only on shared-account conflicts.
        src = inspect.getsource(ai_analyst._build_batch_prompt)
        self.assertIn('"alternates"', src)
        self.assertIn("shared-account conflict", src.lower())


# ---------------------------------------------------------------------------
# 5. Structural pins — the source actually wires the feature.
# ---------------------------------------------------------------------------
class TestSourceStructuralPins(unittest.TestCase):

    def setUp(self):
        self.tp_src = inspect.getsource(trade_pipeline.run_trade_cycle)

    def test_loop_reads_alternates_from_ai_response(self):
        self.assertIn('ai_response.get("alternates"', self.tp_src)

    def test_alternates_merged_before_gates_then_split(self):
        # Tagged + merged before the gates, split into a deque before
        # the execution loop.
        self.assertIn('"_is_alt"', self.tp_src)
        self.assertIn("collections.deque", self.tp_src)
        # The merge ( + _tagged_alternates ) precedes the split
        # (deque comprehension) which precedes the for-loop.
        merge_at = self.tp_src.index("_tagged_alternates")
        split_at = self.tp_src.index("collections.deque")
        loop_at = self.tp_src.index("for ai_trade in ai_trades:")
        self.assertLess(merge_at, split_at)
        self.assertLess(split_at, loop_at)

    def test_loop_calls_conflict_predicate_and_backfill_helper(self):
        self.assertIn("_is_cross_profile_conflict(", self.tp_src)
        self.assertIn("_select_backfill_alternate(", self.tp_src)

    def test_backfill_appends_to_iterated_list(self):
        # The append-during-iteration mechanism must target ai_trades —
        # the exact list the for-loop iterates.
        self.assertIn("ai_trades.append(_alt)", self.tp_src)

    def test_loop_tracks_traded_symbols(self):
        self.assertIn("_traded_syms.add(symbol)", self.tp_src)

    def test_module_imports_collections(self):
        tree = ast.parse(
            open(os.path.join(os.path.dirname(trade_pipeline.__file__),
                              "trade_pipeline.py")).read()
        )
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        self.assertIn("collections", imported)


if __name__ == "__main__":
    unittest.main()
