"""Option-specific risk specialist (Phase 4 of pipeline refactor).

Role: hunt structural failure modes specific to option positions —
risks that the stock-shaped specialists can't see because they don't
read IV, Greeks, DTE, or spread economics:

  - Max-loss-vs-budget: spread max loss exceeds the profile's
    per-trade risk budget. Closes audit finding #5 (multileg
    proposals bypass risk_assessor today, which only knows about
    stock-shaped 1:1 exposure).
  - IV-crush exposure: short premium with imminent earnings, or
    long premium bought at IV rank > 80 (premium will deflate
    even if direction is right).
  - Gamma blowup: short option positions inside 7 DTE with strike
    near spot — a 1% underlying move produces outsized P&L swings.
  - Spread economics: credit received doesn't justify max loss
    (credit/max-loss ratio < 0.20), making the trade negative-
    expectancy regardless of direction view.

Holds VETO authority — these are structural risks no other
specialist can catch (the stock-shaped risk_assessor reads
position_size only, not max-loss-at-expiry).

Phase 4a establishes this specialist's slot in the routing
framework. Phase 4b will integrate it into live ensemble cycles
when the option pipeline's execute() is wired.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "option_spread_risk"
DESCRIPTION = "Option-aware risk gatekeeper — IV crush, gamma, max-loss budget; VETO"
HAS_VETO_AUTHORITY = True
APPLIES_TO_PIPELINES = ("option",)


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    risk_budget = getattr(ctx, "max_per_trade_loss", None)
    budget_line = (
        f"Per-trade risk budget: ${risk_budget:.0f}\n"
        if isinstance(risk_budget, (int, float)) and risk_budget > 0 else ""
    )
    return f"""You are an option-specific risk specialist. Your lens is structural
option failure modes — not direction, not technicals. Other specialists
cover those. You judge each candidate ONLY through the option-economics
lens.

{budget_line}For each candidate, consider:
  - SPREAD MAX-LOSS: does the structural max loss
    (spread_max_loss × 100 × contracts) exceed the per-trade risk
    budget? If yes — VETO.
  - IV CRUSH: is this LONG premium with iv_rank > 80 (premium will
    deflate even on a directional win)? Or SHORT premium with
    earnings inside the spread's DTE (event will spike IV against
    you)? If yes — VETO.
  - GAMMA RISK: SHORT options with DTE < 7 and strike within 2% of
    spot — gamma exposure makes P&L unstable. VETO unless explicitly
    a 0DTE strategy.
  - CREDIT/MAX-LOSS ratio: for credit spreads, credit received /
    max loss should be at least 0.20. Below that, the trade is
    negative-expectancy regardless of directional view. VETO.

Candidates:
{candidates_block(candidates)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. No markdown fences, no prose, no
single top-level object. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence option-economics rationale"
  }}

VERDICT DISCIPLINE:
  HOLD = the DEFAULT. No structural option-economics red flag found.
         Don't VETO for "I don't like the trade" — that's HOLD.
  VETO = SPECIFIC structural problem: max loss exceeds budget,
         iv-crush exposure, near-expiry gamma blowup, credit
         insufficient vs max loss. Name the risk in the reasoning.
  BUY  = option economics ACTIVELY support the trade (rare —
         e.g., short premium at iv_rank > 90 with no event risk).
  SELL = the option position should be closed (only relevant when
         a candidate is an existing-position close decision).

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order as the list above.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
