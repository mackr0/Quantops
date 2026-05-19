"""Risk synthesis specialist (re-scoped 2026-05-18, Phase 3 of docs/17).

Originally enumerated risk factors directly from the candidate's
alt-data (FDA citations, NHTSA recalls, risk-factor diffs, macro
vol regime, etc.). As of Phase 3 the deterministic library has
~12 rules that fire on exactly those conditions.

New role: SYNTHESIZE a worst-plausible-outcome scenario from the
rule verdicts. Retains VETO authority — but VETOes are now only
issued when the LLM's synthesis reveals risk dynamics the rule
layer's individual checks cannot have surfaced (e.g., the
combination of two CAUTION rules creating compounded exposure
the individual rules don't capture).
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "risk_assessor"
DESCRIPTION = "Synthesizes worst-plausible-outcome scenarios from the rule panel"
HAS_VETO_AUTHORITY = True
APPLIES_TO_PIPELINES = ("stock", "option")


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    regime = getattr(ctx, "market_regime", None) or "unknown"
    return f"""You are a risk-synthesis specialist. The deterministic rule layer has
already flagged the standard risk conditions (PE extremes, FDA citations,
NHTSA recalls, EPA/OSHA violations, risk-factor-diff additions, recent
adverse 8-K items, macro vol regime, multiple-negative-catalyst stacking,
etc.). Each candidate below carries a `RULES: [V]name [C]name ...` suffix.

Current regime: {regime}

Your job is NOT to re-discover what those rules already flagged. Your
job is to SYNTHESIZE the worst plausible outcome: what's the SCENARIO
in which this trade goes badly, given the rule verdicts AND the broader
context? Look for COMPOUNDED risk (multiple CAUTIONs that the individual
rules don't catch but interact dangerously together) and HIDDEN risk
(a coherent failure path the rule library doesn't have a check for —
your unique value vs the deterministic layer).

Candidates:
{candidates_block(candidates, specialist_name="risk_assessor", ctx=ctx)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. No markdown fences, no prose, no
single top-level object. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence scenario synthesis — name the dominant risk path"
  }}

VERDICT DISCIPLINE:
  HOLD  = the DEFAULT. No coherent worst-case scenario stands out beyond
          what the deterministic rules already say.
  VETO  = your scenario synthesis reveals a SPECIFIC failure path with
          compounded probability — typically when 2+ CAUTION rules
          combine into a thesis the individual rules don't fully convey,
          or when you see a risk the rule library can't enumerate (novel
          regulatory action, idiosyncratic concentration, etc.). The
          deterministic VETOs already catch the obvious cases — don't
          duplicate them; add ONLY what synthesis uniquely reveals.
  SELL  = risk synthesis supports closing an existing position
  BUY   = synthesis reveals risk is ASYMMETRICALLY low (rare)

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order as the list above. Over-vetoing is the failure mode
to watch for — if you find yourself writing >2 VETOs in a batch of 5,
re-examine whether you're synthesizing or just rediscovering rules.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
