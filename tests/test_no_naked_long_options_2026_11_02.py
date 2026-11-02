"""2026-11-02 — no naked long premium: single-leg long_call / long_put
are not traded. Since the 08-24 reset they lost in every arm (NANO 159
closes, 28% win, −$16K; the fleet's option book −$17K of which −$16.9K
was single-leg longs), concentrated in 6–14-day holds — theta decay on
coin-flip direction. The menu no longer offers them; the pipeline gate
is the fail-closed backstop. Covered calls, protective puts, cash-
secured puts and defined-risk spreads are unaffected.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _pipeline():
    from pipelines.option import OptionPipeline
    return OptionPipeline


class TestExecutionGate:
    def test_long_call_and_long_put_are_skipped_before_any_broker_call(self, monkeypatch):
        import pipelines.option as po
        import client
        called = []
        monkeypatch.setattr(client, "get_api", lambda ctx: called.append("api") or object())
        for strat in ("long_call", "long_put", "LONG_CALL"):
            res = po.OptionPipeline._execute_single_leg(
                object(), {"option_strategy": strat, "strike": 100, "expiry": "2026-12-18",
                           "contracts": 1}, "AAPL")
            assert res["action"] == "SKIP" and "naked long premium" in res["reason"]
        assert called == [], "the gate must refuse before touching the broker"

    def test_other_single_leg_strategies_pass_the_gate(self, monkeypatch):
        import pipelines.option as po
        import client
        seen = []
        monkeypatch.setattr(client, "get_api", lambda ctx: object())
        import options_trader
        monkeypatch.setattr(options_trader, "execute_option_strategy",
                            lambda api, proposal, ctx=None, log=None, **k: seen.append(proposal["option_strategy"]) or {"action": "OPTIONS_OPEN"})
        for strat in ("cash_secured_put", "covered_call", "protective_put"):
            res = po.OptionPipeline._execute_single_leg(
                object(), {"option_strategy": strat, "strike": 100, "expiry": "2026-12-18",
                           "contracts": 1}, "AAPL")
            assert res.get("action") != "SKIP", res
        assert seen == ["cash_secured_put", "covered_call", "protective_put"]

    def test_constant(self):
        import pipelines.option as po
        assert po.NAKED_LONG_PREMIUM_STRATEGIES == frozenset({"long_call", "long_put"})


class TestMenu:
    def test_menu_no_longer_offers_naked_longs(self):
        src = open(os.path.join(REPO, "ai_analyst.py")).read()
        i = src.index("option_strategy MUST be EXACTLY one of:")
        block = src[i:i + 600]
        assert "covered_call, protective_put, cash_secured_put" in block
        assert "long_call / long_put are NOT traded" in block
        assert '"option_strategy": "long_call"' not in src, "the example must not model a naked long"
