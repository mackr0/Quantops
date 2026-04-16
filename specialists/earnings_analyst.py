"""Earnings-focused specialist.

Role: read each candidate through the lens of its most recent earnings
release and any outstanding SEC filing signals. Flags stocks where the
earnings picture (surprise, guidance direction, post-announcement drift,
going-concern / material-weakness alerts) materially supports or opposes
the technical setup.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "earnings_analyst"
DESCRIPTION = "Interprets earnings context, guidance tone, and SEC filing alerts"


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    return f"""You are a specialist equity analyst focused on earnings and financial filings.
Your job is a FOCUSED LENS — not a full investment decision. You only
judge each candidate through the earnings / filing dimension. Ignore
pure technicals; other specialists cover those.

For each candidate below, return one of:
  BUY   = earnings picture strongly supports the long thesis
  SELL  = earnings / filing signals are a red flag for longs
  HOLD  = specific earnings context exists and is neutral
  VETO  = a material earnings-related risk makes this trade unacceptable
          (e.g. going-concern disclosure, restatement, massive guide-down)

Candidates (symbol, current signal, one-line context):
{candidates_block(candidates)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. No prose,
no markdown fences, no single top-level object. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence earnings/filing-specific rationale"
  }}

CRITICAL — OMIT SYMBOLS YOU CAN'T ASSESS:
If the context does not include specific earnings data (upcoming
earnings date, recent EPS surprise, guidance commentary, or a SEC
filing alert), DO NOT include that symbol in your response. Return
ONLY the symbols you have specific earnings-dimension information
about. An empty array is a valid response.

This is a CHANGE from prior behavior. Previously the instruction was
"return HOLD with low confidence" for unknown symbols — that polluted
the consensus. Now: silence is the right answer when you have no data.

For symbols you DO include, return high-confidence verdicts based on
the actual earnings/filing evidence you can see.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
