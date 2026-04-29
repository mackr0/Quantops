"""AI-driven strategy proposer — Phase 7 of the Quant Fund Evolution roadmap.

The AI never writes Python. It writes structured JSON specs against the
allowlisted grammar in `strategy_generator`. We validate every proposal
before persisting it; malformed specs are silently rejected.

Caller pattern:
    proposals = propose_strategies(
        ctx_summary="midcap bull regime, mean-reverters decaying",
        recent_performance=[{"name": "...", "sharpe": 0.4, "win_rate": 0.42}, ...],
        n_proposals=3,
        ai_provider="anthropic",
        ai_model="claude-haiku-4-5-20251001",
        ai_api_key=...
    )
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from strategy_generator import (
    ALLOWED_DIRECTIONS,
    ALLOWED_FIELDS,
    ALLOWED_MARKETS,
    ALLOWED_OPS,
    SpecError,
    validate_spec,
)

logger = logging.getLogger(__name__)


def _build_prompt(
    ctx_summary: str,
    recent_performance: List[Dict[str, Any]],
    n_proposals: int,
    market_types: List[str],
    direction_mix: Optional[Dict[str, int]] = None,
) -> str:
    """Construct the strict JSON-only prompt for the AI proposer.

    P1.13 of LONG_SHORT_PLAN.md — direction_mix is a dict like
    {'BUY': 5, 'SELL': 5} that asks the AI for a specific count of
    each direction. Without it the AI defaults to whatever
    distribution it thinks is best, which historically skews 90%+
    bullish. Shorts-enabled profiles pass an explicit mix to ensure
    bearish proposals get fair representation.
    """
    perf_lines = []
    for p in recent_performance[:10]:
        perf_lines.append(
            f"  - {p.get('name', '?')}: sharpe={p.get('sharpe', 0):.2f}, "
            f"win_rate={p.get('win_rate', 0):.1%}, "
            f"n={p.get('n_predictions', 0)}"
        )
    perf_block = "\n".join(perf_lines) if perf_lines else "  (no track record yet)"

    return f"""You are proposing new quantitative trading strategies for a live fund.
You are NOT writing Python code. You are writing JSON specifications that a
deterministic code generator translates into sandboxed modules.

Current regime context:
{ctx_summary}

Existing strategies' recent performance:
{perf_block}

Allowed markets (subset of this list for "applicable_markets"):
{sorted(ALLOWED_MARKETS)}

Allowed directions (one string for "direction"):
{sorted(ALLOWED_DIRECTIONS)}

Allowed comparison operators (one string for condition "op"):
{sorted(ALLOWED_OPS)}

Allowed condition fields (for condition "field" and "field_ref"):
{sorted(ALLOWED_FIELDS)}

Each condition compares one field against either a numeric "value" or
another field referenced by "field_ref". ALL conditions must hold for a
candidate to trigger. Keep conditions tight — 2 to 4 conditions is ideal.

{("Direction mix required: " + ", ".join(f"{n} {d}" for d, n in direction_mix.items()) + ".") if direction_mix else ""}

Output strictly valid JSON — a top-level array of exactly {n_proposals}
proposal objects. Each object must have these fields:
  name                 lowercase snake_case starting with "auto_" (unique)
  description          one-line intent (what edge this captures)
  applicable_markets   subset of the allowed markets
  direction            "BUY" or "SELL"
  score                integer 1, 2, or 3 (conviction weight)
  conditions           array of 1-6 condition objects, each with:
                         field, op, and either value (number) OR field_ref

Do not include any other keys. Do not add commentary, preface, or trailing text.
Return ONLY the JSON array.

Example of a single proposal (for shape — do not copy verbatim):
{{
  "name": "auto_oversold_vol_confirm",
  "description": "Deep oversold with above-average volume and above SMA50",
  "applicable_markets": ["small", "midcap"],
  "direction": "BUY",
  "score": 2,
  "conditions": [
    {{"field": "rsi", "op": "<", "value": 25}},
    {{"field": "volume_ratio", "op": ">", "value": 1.8}},
    {{"field": "close", "op": ">", "field_ref": "sma_50"}}
  ]
}}

Propose {n_proposals} strategies that exploit patterns the existing library
does not already cover.
"""


_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _extract_json_array(raw: str) -> Optional[List[Any]]:
    """Best-effort: find the first JSON array in the response and parse it."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    match = _JSON_ARRAY_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        return None


def propose_strategies(
    ctx_summary: str,
    recent_performance: List[Dict[str, Any]],
    n_proposals: int,
    ai_provider: str,
    ai_model: str,
    ai_api_key: str,
    market_types: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    direction_mix: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Ask the AI for N new strategy specs. Returns only specs that validate.

    Silently drops any proposal that fails schema validation — we never
    trust the AI to follow instructions perfectly. The caller can retry
    or accept a partial batch.

    P1.13 of LONG_SHORT_PLAN.md — direction_mix forces a specific
    long/short proposal balance. Without it the AI's free-form
    output skews ~90% bullish.
    """
    from ai_providers import call_ai

    if n_proposals <= 0:
        return []

    market_types = market_types or sorted(ALLOWED_MARKETS)
    prompt = _build_prompt(ctx_summary, recent_performance, n_proposals,
                            market_types, direction_mix=direction_mix)

    try:
        raw = call_ai(
            prompt,
            provider=ai_provider,
            model=ai_model,
            api_key=ai_api_key,
            max_tokens=4096,
            db_path=db_path,
            purpose="strategy_proposal",
        )
    except Exception as exc:
        logger.warning("strategy proposer AI call failed: %s", exc)
        return []

    candidates = _extract_json_array(raw) or []
    valid: List[Dict[str, Any]] = []
    seen_names: set = set()
    for i, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or name in seen_names:
            continue
        try:
            validate_spec(item)
        except SpecError as exc:
            logger.info("rejected proposal #%d (%s): %s", i, name, exc)
            continue
        seen_names.add(name)
        valid.append(item)

    logger.info(
        "strategy proposer: %d/%d proposals passed validation",
        len(valid),
        len(candidates),
    )
    return valid
