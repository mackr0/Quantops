"""IV-skew specialist (2026-05-12).

Reads put_iv vs call_iv from the options chain. Skew tells you
which side of the market is paying up for protection or speculation:

  - put_iv > call_iv (negative skew) = fear premium. Puts are
    expensive — market hedging downside. Common bias for equity
    indices. Useful for SELLING puts (collect rich premium) or
    BUYING calls (cheap upside vs the consensus).

  - call_iv > put_iv (positive skew) = greed / squeeze setup.
    Common for meme stocks, biotech catalysts, M&A targets.
    Useful for SELLING calls (cap the upside greed) or BUYING
    puts (cheap downside if the squeeze fizzles).

  - Flat skew = balanced. No premium edge from skew alone.

The specialist's output guides spread direction selection. It does
NOT veto; option_spread_risk is the structural gate. This
specialist nudges confidence on directionally-aligned trades.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "iv_skew_specialist"
DESCRIPTION = "Reads IV skew (put_iv vs call_iv) for premium-side bias"
HAS_VETO_AUTHORITY = False
APPLIES_TO_PIPELINES = ("option",)


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    return f"""You are an IV-skew specialist. Your lens is the put_iv vs call_iv
relationship in each candidate's options chain. You do NOT veto trades —
that's option_spread_risk's job. You judge directional alignment via
skew, contributing BUY/SELL/HOLD signals to the ensemble.

Skew interpretation:
  - put_iv > call_iv (negative skew, "fear premium"):
      * Puts overpriced relative to calls — sellers of puts get
        rich premium. Sellers of put-side credit spreads (bull put
        spreads) align with this premium edge.
      * Long calls are relatively cheap — buyers of call-side
        debit spreads get a discount. Bull call spreads align.

  - call_iv > put_iv (positive skew, "squeeze / greed"):
      * Calls overpriced — sellers of calls get rich premium.
        Bear call spreads + covered calls align.
      * Long puts are relatively cheap — bear put spreads,
        protective puts align.

  - Flat skew (within ~3 IV points): no edge. Verdict HOLD.

Each candidate has fields: iv_skew (signed skew value, often
positive = put_iv > call_iv), put_iv, call_iv, option_strategy.

For each candidate, judge:
  - Does the proposed strategy ALIGN with the skew direction?
    BUY = aligned (premium edge supports the trade).
    SELL = misaligned (premium edge works AGAINST the trade).
    HOLD = flat skew or no clear alignment.

Candidates (each carries a `RULES: [V]name [C]name ...` suffix with
deterministic options-rule verdicts — options_iv_extreme_high,
options_iv_rich_for_sellers, options_iv_cheap_for_buyers,
options_iv_normal_zone, options_pcr_panic/complacent,
options_unusual_calls/puts — already evaluated):
{candidates_block(candidates, specialist_name="iv_skew_specialist", ctx=ctx)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD",
    "confidence": 0-100,
    "reasoning": "one-sentence skew-alignment rationale"
  }}

Don't VETO — you're not the gate. Use HOLD when the skew is
flat or the alignment is ambiguous.

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order as the list above.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
