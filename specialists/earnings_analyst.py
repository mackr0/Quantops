"""Earnings synthesis specialist (re-scoped 2026-05-18, Phase 3 of docs/17).

Originally read each candidate's earnings context, surprise streak,
guidance tone, etc. directly from alt-data. As of Phase 3 the
deterministic library has ~8 earnings-specific rules
(earnings_surprise_streak, earnings_miss_streak, earnings_within_
window, positive/negative_earnings_revisions, insider_buying/
selling_near_earnings, transcript_sentiment_bullish/bearish,
recent_8k_earnings_release, biotech_milestone_upcoming).

New role: SYNTHESIZE the earnings setup. Given which earnings
rules fired, is the earnings narrative compelling (beat-and-raise
trajectory)? Is it deteriorating (down-revisions + miss streak)?
Is the upcoming-event risk priced in (high IV)? The LLM's value
is in weaving the individual rule signals into a forward-looking
earnings thesis.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "earnings_analyst"
DESCRIPTION = "Synthesizes earnings trajectory from the earnings-cluster rule verdicts"
APPLIES_TO_PIPELINES = ("stock", "option")


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    return f"""You are an earnings synthesis specialist. The deterministic rule layer
has already flagged the standard earnings signals — surprise streak,
miss streak, in-window upcoming earnings, EPS revisions direction,
insider buying/selling near earnings, transcript sentiment tone,
recent 8-K Item 2.02 (earnings release), biotech milestones. Each
candidate below carries a `RULES: [V]name [C]name ...` suffix.

Your job is NOT to re-enumerate which rules fired. Your job is to
SYNTHESIZE the earnings trajectory: is this a beat-and-raise story
(revision-up + surprise-streak + bullish transcript), a deteriorating
story (revision-down + miss-streak + bearish transcript), or an
event-priced story (in-window + high IV + opposite insider activity)?

Candidates:
{candidates_block(candidates, specialist_name="earnings_analyst", ctx=ctx)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. No prose,
no markdown fences, no single top-level object. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence trajectory synthesis (not a rule re-statement)"
  }}

Verdict semantics:
  BUY  = trajectory synthesis is coherently bullish — multiple
         earnings rules reinforce each other
  SELL = trajectory synthesis is deteriorating
  HOLD = no clear synthesis from the rules (or rules silent)
  VETO = trajectory reveals catastrophic-earnings risk the rule layer
         hasn't fully escalated (e.g., compounding miss streak +
         insider selling + transcript tone all aligned to disaster)

CRITICAL — OMIT SYMBOLS WITH NO EARNINGS-RULE VERDICTS:
If a candidate's RULES suffix contains NO earnings-cluster rules
(none of the earnings_* / transcript_* / insider_*_near_earnings /
biotech_milestone_upcoming rules fired), OMIT that symbol from your
response. An empty array is valid. Silence beats noise when there's
no earnings dimension to synthesize.

For symbols you DO include, return high-confidence verdicts grounded
in the specific rule verdicts you weighted.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
