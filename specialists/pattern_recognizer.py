"""Technical pattern specialist.

Role: evaluate each candidate purely through chart structure — breakouts,
reversals, support/resistance, volume confirmation, trend strength. The
generalist AI sees these indicators too, but a specialist with a narrow
mandate produces sharper verdicts than a jack-of-all-trades.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "pattern_recognizer"
DESCRIPTION = "Judges chart structure, trend quality, and volume confirmation"


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    return f"""You are a technical pattern specialist. Your lens is purely chart
structure, indicator confluence, and volume behavior — NOT fundamentals,
NOT news, NOT macro. Other specialists cover those.

For each candidate, judge:
  - Is the pattern clean (well-defined breakout, confirmed reversal,
    coherent trend) or is it noise?
  - Does volume confirm price? (breakouts without volume are traps)
  - Is the entry location advantageous relative to support/resistance?
  - Are momentum indicators in confluence or diverging?

Candidates:
{candidates_block(candidates)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. No markdown fences, no prose, no
single top-level object. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence technical rationale"
  }}

Verdict semantics:
  BUY  = pattern is structurally clean and supports the shortlist signal
  SELL = pattern contradicts the shortlist signal
  HOLD = pattern is ambiguous or mid-range
  VETO = chart is actively dangerous (late-stage extension, clear failed
         breakout, broken support) — regardless of other factors

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order as the list above.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
