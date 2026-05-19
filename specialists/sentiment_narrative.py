"""Narrative synthesis specialist (re-scoped 2026-05-18, Phase 3 of docs/17).

Previously enumerated insider activity, congressional trades,
StockTwits sentiment, options flow, etc. directly from alt-data.
As of Phase 3 the deterministic library has ~15 rules covering
those data points individually.

New role: SYNTHESIZE the coherent NARRATIVE — what story is
being told when the insider-buying-cluster rule fires AND the
unusual-options-activity rule fires AND congressional buying is
detected? What story is being told when retail sentiment is
euphoric while insiders are selling? The LLM's unique value is
weaving the individual signals into a narrative.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "sentiment_narrative"
DESCRIPTION = "Synthesizes a coherent narrative from sentiment + smart-money rule verdicts"
APPLIES_TO_PIPELINES = ("stock", "option")


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    maga_line = ""
    maga_ctx = getattr(ctx, "maga_context", None)
    if maga_ctx:
        maga_line = f"\nMacro narrative this session: {str(maga_ctx)[:300]}\n"

    return f"""You are a narrative synthesis specialist. The deterministic rule layer
has already evaluated the individual signals — insider buying clusters
(rule: insider_cluster_buying), insider track record (insider_track_
record_strong/weak), insider buys/sells near earnings, congressional
buying, dark-pool accumulation, 13D activist filings, unusual options
flow direction (options_unusual_calls/puts), StockTwits sentiment
extremes (extreme_bullish/bearish), Google Trends spikes, Wikipedia
attention surges, app-store ranking shifts, star-manager holdings,
analyst EPS revisions. Each candidate below carries a `RULES: [V]name
[C]name ...` suffix.
{maga_line}
Your job is NOT to repeat those rule verdicts. Your job is to weave
them into a coherent NARRATIVE: WHO is positioning here, WHY, and
HOW DO THE SIGNALS RECONCILE? The interesting cases are usually:
  - All signals AGREE: high-conviction narrative (smart money +
    retail + analysts + insiders all aligned)
  - Signals DISAGREE: who's likely right? (insiders typically beat
    retail; analysts typically lag insiders; congress is mixed)
  - Crowding/contrarian: euphoric retail vs insider selling = top;
    capitulation retail vs insider buying = bottom

Candidates:
{candidates_block(candidates, specialist_name="sentiment_narrative", ctx=ctx)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. No markdown fences, no prose, no
single top-level object. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence narrative — who is positioning and what story they tell"
  }}

Verdict semantics:
  BUY  = narrative coherently supports upside (smart money + retail +
         analyst signals weave into a consistent bullish story)
  SELL = narrative is bearish-coherent (insider exit + analyst cuts +
         macro headwind agree)
  HOLD = signals don't reconcile into one story — wait for clarity
  VETO = narrative reveals a SPECIFIC catalyst risk the technical /
         risk specialists may have missed (political crosshair,
         narrative crowding hitting a known reversal pattern)

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order as the list above.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
