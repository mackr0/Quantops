"""Sentiment and narrative specialist.

Role: judge each candidate through the lens of news flow, political /
macro narrative, and insider or options-flow tells. Ignores clean
technicals and pure earnings — those are other specialists' jobs.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "sentiment_narrative"
DESCRIPTION = "Reads narrative: news flow, political risk, insider/options tells"


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    # Surface MAGA/political context if the profile has it enabled, as that's
    # a narrative signal this specialist should consider.
    maga_line = ""
    maga_ctx = getattr(ctx, "maga_context", None)
    if maga_ctx:
        maga_line = f"\nMacro narrative this session: {str(maga_ctx)[:300]}\n"

    return f"""You are a narrative / sentiment specialist. Your lens is news flow,
political/macro context, insider buying clusters, unusual options flow,
and crowd positioning. Ignore pure chart patterns and ignore the
earnings table — other specialists handle those.
{maga_line}
For each candidate, judge:
  - Is recent news supportive or hostile?
  - Are insiders buying (real conviction) or selling (tax-loss or exit)?
  - Does options flow / IV skew suggest big money positioning for a move?
  - Does the macro/political narrative create a tailwind or headwind?

Candidates:
{candidates_block(candidates)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. No markdown fences, no prose, no
single top-level object. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence narrative rationale"
  }}

Verdict semantics:
  BUY  = narrative clearly supports upside (positive news, insider cluster,
         bullish options flow, favorable macro)
  SELL = narrative opposes longs (negative news, insider dumping,
         bearish options flow, hostile macro)
  HOLD = no meaningful narrative signal
  VETO = narrative risk is so acute this trade shouldn't happen
         (active litigation, regulatory shock, political crosshair)

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order as the list above.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
