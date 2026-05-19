"""Technical synthesis specialist (re-scoped 2026-05-18, Phase 3 of docs/17).

Originally this specialist re-derived technical observations from
the candidate dict — RSI overbought, ADX strength, breakout
volume, etc. As of Phase 3 the deterministic specialist library
emits 25+ rule verdicts on exactly those questions, far more
reliably than the LLM ever could.

The new role: SYNTHESIZE from the deterministic rules' verdicts.
The candidate render now carries a `RULES: [V]name [C]name ...`
suffix. The LLM's job is to weigh the conflicting verdicts and
form a coherent technical thesis — the unique work the LLM is
actually good at.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "pattern_recognizer"
DESCRIPTION = "Synthesizes a coherent technical thesis from the deterministic rule panel"
APPLIES_TO_PIPELINES = ("stock",)


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    return f"""You are a technical synthesis specialist. The deterministic rule layer
has already evaluated every standard technical pattern (RSI bands, ADX
strength, Bollinger walks, VWAP relationship, squeeze release, volume
confirmation, Fibonacci levels, momentum factor, etc.). Each candidate
below carries a `RULES: [V]name [C]name ...` suffix where:
  [V] = VETO  — the rule has high confidence the trade should NOT happen
  [C] = CAUTION — the rule sees a yellow flag
  [C] = CONFIRM — the rule's pattern actively supports the candidate

Your job is NOT to re-derive what those rules already said. Your job is
to SYNTHESIZE: given the verdicts AND the underlying data, what is the
coherent technical thesis? Where do the verdicts agree, conflict, or
miss a pattern the deterministic layer can't see (multi-bar narratives,
chart symmetry, complex divergences spanning multiple indicators)?

Candidates:
{candidates_block(candidates, specialist_name="pattern_recognizer", ctx=ctx)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. No markdown fences, no prose, no
single top-level object. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence SYNTHESIS — reference the rule verdicts you weighted"
  }}

Verdict semantics:
  BUY  = rule confirms outweigh rule warnings AND the overall pattern
         is coherent in the signal direction
  SELL = rule warnings dominate the picture for a LONG candidate
  HOLD = rules conflict without a clear synthesis; weight is genuinely mixed
  VETO = your synthesis surfaces something the rule layer missed (rare —
         the deterministic VETOs usually catch this already)

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order as the list above.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
