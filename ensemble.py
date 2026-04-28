"""Specialist ensemble — Phase 8 of the Quant Fund Evolution roadmap.

Orchestrates the AI specialists in `specialists/` against a shortlist of
candidates. Each specialist sees every candidate in one batch call, so
total AI cost scales with the number of specialists (constant) rather
than the number of candidates.

Output: per-candidate ensemble verdict plus the raw specialist breakdown
so the final AI (or the dashboard) can see who agreed and who dissented.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Weight each specialist's confidence-scaled vote. Risk has lower raw
# weight but unique VETO authority, so its influence is binary when it
# fires, zero otherwise.
SPECIALIST_WEIGHTS = {
    "earnings_analyst": 1.0,
    "pattern_recognizer": 1.2,
    "sentiment_narrative": 0.9,
    "risk_assessor": 1.0,
}

# Confidence floor below which a verdict is ignored (specialist was
# genuinely unsure and shouldn't tilt the consensus).
CONFIDENCE_FLOOR = 25.0


# Chunk size for specialist calls. With tool_use (Anthropic) the model
# reliably returns every requested entry, so chunking is only a hedge.
# With plain-prompt fallback, chunks of 5 help reduce drop rate.
CHUNK_SIZE = 15


# Some specialists don't have usable input data for certain markets.
# Running them just produces ABSTAIN/HOLD noise that pollutes consensus
# and costs AI tokens. For crypto specifically: no earnings calendars,
# no insider/Form 4, no SEC filings, no standardized options chains —
# earnings_analyst, sentiment_narrative, and risk_assessor all add
# noise without signal. Only pattern_recognizer genuinely reads price
# action and produces useful verdicts.
APPLICABLE_SPECIALISTS_BY_MARKET = {
    "crypto": {"pattern_recognizer"},
    # Equity markets get the full ensemble — specialists have rich data
    # (SEC, earnings, options, insider, news).
}


def _specialists_for_market(market_type: str, all_specialists):
    """Filter the specialist list to those applicable to this market."""
    allowed = APPLICABLE_SPECIALISTS_BY_MARKET.get(market_type)
    if allowed is None:
        return list(all_specialists)
    return [s for s in all_specialists if s.NAME in allowed]


# Window (days) around now in which upcoming earnings make the
# earnings_analyst specialist's work genuinely productive. When no
# candidate in the shortlist has earnings inside this window, the
# specialist almost always returns ABSTAIN with a 45-token response —
# we still pay for input tokens for no signal. Gate saves ~10% of
# ensemble cost in steady state.
EARNINGS_ANALYST_WINDOW_DAYS = 14


def _any_candidate_has_upcoming_earnings(candidates: List[Dict[str, Any]],
                                          window_days: int) -> bool:
    """Return True if any candidate has earnings reporting within window_days.

    Uses `earnings_calendar.check_earnings` which is DB-cached; shortlist
    symbols are usually warm in cache since the pre-filter already checks
    earnings dates. Fail-open on error (return True) so we never silently
    skip the specialist when our knowledge is incomplete.
    """
    try:
        from earnings_calendar import check_earnings
    except ImportError:
        return True  # don't silence the specialist on import failure

    for c in candidates:
        sym = c.get("symbol", "")
        if not sym or "/" in sym:
            continue
        try:
            result = check_earnings(sym)
        except Exception:
            # Per-symbol failure shouldn't disable the gate for everyone —
            # skip this symbol and keep checking. If ALL symbols error,
            # we return False (gate fires). That's fine: the specialist
            # had nothing to work with anyway.
            continue
        if result and 0 <= result.get("days_until", 999) <= window_days:
            return True
    return False


def _verdicts_schema():
    """JSON schema forcing an array of verdicts with all required fields."""
    return {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol":     {"type": "string"},
                        "verdict":    {"type": "string",
                                       "enum": ["BUY", "SELL", "HOLD", "VETO"]},
                        "confidence": {"type": "number",
                                       "minimum": 0, "maximum": 100},
                        "reasoning":  {"type": "string"},
                    },
                    "required": ["symbol", "verdict", "confidence"],
                },
            },
        },
        "required": ["verdicts"],
    }


def run_ensemble(
    candidates: List[Dict[str, Any]],
    ctx: Any,
    ai_provider: str,
    ai_model: str,
    ai_api_key: str,
    max_candidates: int = 15,
) -> Dict[str, Any]:
    """Run every specialist against the shortlist and synthesize verdicts.

    Candidates are processed in chunks of `CHUNK_SIZE` to avoid Haiku's
    tendency to drop entries on long lists. Total AI calls = specialists
    × ceil(N/CHUNK_SIZE).

    Returns
    -------
    dict with:
        per_symbol:  {symbol: {verdict, confidence, vetoed, specialists: [...]}}
        raw:         {specialist_name: [verdict_dicts]}
        cost_calls:  number of AI calls made
    """
    from ai_providers import call_ai
    from specialists import discover_specialists

    if not candidates:
        return {"per_symbol": {}, "raw": {}, "cost_calls": 0}

    # Cap so a 200-candidate shortlist doesn't blow the specialist prompts.
    batch = candidates[:max_candidates]
    chunks = [batch[i:i + CHUNK_SIZE] for i in range(0, len(batch), CHUNK_SIZE)]

    market_type = getattr(ctx, "segment", "") or ""
    specialists = _specialists_for_market(market_type, discover_specialists())

    # Lever 3 of COST_AND_QUALITY_LEVERS_PLAN.md — per-profile
    # disable list. When calibration data shows a specialist is
    # anti-correlated on this profile, skip its API call entirely.
    # Daily `_task_specialist_health_check` maintains the list.
    # Hard floor: ensure at least 2 specialists remain so the
    # ensemble still has a meaningful synthesis.
    disabled = set()
    try:
        import json as _json
        raw = getattr(ctx, "disabled_specialists", "[]") or "[]"
        if isinstance(raw, str):
            disabled = set(_json.loads(raw))
        elif isinstance(raw, list):
            disabled = set(raw)
    except Exception:
        disabled = set()
    if len(specialists) - len(disabled) < 2:
        # Floor enforcement — un-disable arbitrarily until at least
        # 2 specialists run. Logged so operators can see when this
        # protective fallback fired.
        kept = [s.NAME for s in specialists]
        excess = (len(disabled) - (len(specialists) - 2))
        if excess > 0:
            for nm in list(disabled)[:excess]:
                disabled.discard(nm)
            logger.warning(
                "ensemble: disabled_specialists list would leave fewer "
                "than 2 specialists active; restoring %d specialists "
                "(floor enforcement)", excess,
            )

    raw_by_specialist: Dict[str, List[Dict[str, Any]]] = {}
    cost_calls = 0

    # Pre-compute earnings relevance once per ensemble run, not per specialist
    earnings_in_window = _any_candidate_has_upcoming_earnings(
        batch, EARNINGS_ANALYST_WINDOW_DAYS
    )

    for spec in specialists:
        name = spec.NAME

        # Per-profile disabled list: skip the API call entirely.
        # Synthesizer treats a missing specialist as ABSTAIN.
        if name in disabled:
            logger.debug(
                "ensemble: skipping %s — in profile disabled_specialists",
                name,
            )
            continue
        combined: List[Dict[str, Any]] = []
        seen_syms: set = set()

        # Cost gate: skip earnings_analyst entirely when no candidate has
        # earnings in the next EARNINGS_ANALYST_WINDOW_DAYS. The specialist
        # produces ABSTAIN/short responses in those cases and costs input
        # tokens for no signal.
        if name == "earnings_analyst" and not earnings_in_window:
            logger.debug(
                "ensemble: skipping earnings_analyst — no candidate has "
                "earnings in next %d days",
                EARNINGS_ANALYST_WINDOW_DAYS,
            )
            continue

        use_tools = (ai_provider == "anthropic")

        for chunk in chunks:
            try:
                prompt = spec.build_prompt(chunk, ctx)
            except Exception as exc:
                logger.warning("specialist %s build_prompt failed: %s", name, exc)
                continue

            verdicts: List[Dict[str, Any]] = []
            if use_tools:
                try:
                    from ai_providers import call_ai_structured
                    result = call_ai_structured(
                        prompt,
                        schema=_verdicts_schema(),
                        tool_name="submit_verdicts",
                        provider=ai_provider,
                        model=ai_model,
                        api_key=ai_api_key,
                        max_tokens=2048,
                        db_path=getattr(ctx, "db_path", None),
                        purpose=f"ensemble:{name}",
                    )
                    cost_calls += 1
                    if result and isinstance(result.get("verdicts"), list):
                        # Normalize shape — parse_response clamps/validates
                        from specialists._common import VALID_VERDICTS
                        for v in result["verdicts"]:
                            if not isinstance(v, dict):
                                continue
                            sym = v.get("symbol")
                            verdict = v.get("verdict")
                            if not isinstance(sym, str) or verdict not in VALID_VERDICTS:
                                continue
                            try:
                                conf = float(v.get("confidence", 0))
                            except (TypeError, ValueError):
                                conf = 0.0
                            verdicts.append({
                                "symbol": sym,
                                "verdict": verdict,
                                "confidence": max(0.0, min(100.0, conf)),
                                "reasoning": str(v.get("reasoning", ""))[:400],
                            })
                except Exception as exc:
                    logger.warning(
                        "specialist %s tool call failed on chunk: %s", name, exc
                    )
                    verdicts = []
            else:
                # Non-Anthropic fallback — plain prompt + text parser
                try:
                    raw = call_ai(
                        prompt,
                        provider=ai_provider,
                        model=ai_model,
                        api_key=ai_api_key,
                        max_tokens=2048,
                        db_path=getattr(ctx, "db_path", None),
                        purpose=f"ensemble:{name}",
                    )
                    cost_calls += 1
                    verdicts = spec.parse_response(raw) or []
                except Exception as exc:
                    logger.warning(
                        "specialist %s AI call failed on chunk: %s", name, exc
                    )
                    verdicts = []

            # Dedupe in case the same symbol shows up in multiple chunks
            for v in verdicts:
                sym = v.get("symbol")
                if sym and sym not in seen_syms:
                    seen_syms.add(sym)
                    combined.append(v)

        raw_by_specialist[name] = combined

    # Synthesize per-symbol consensus
    per_symbol = _synthesize(batch, raw_by_specialist,
                              db_path=getattr(ctx, "db_path", None))

    return {
        "per_symbol": per_symbol,
        "raw": raw_by_specialist,
        "cost_calls": cost_calls,
    }


def _synthesize(candidates: List[Dict[str, Any]],
                raw_by_specialist: Dict[str, List[Dict[str, Any]]],
                db_path: Optional[str] = None) -> Dict[str, Any]:
    """Combine specialist verdicts into a per-symbol final consensus.

    Wave 3 / Fix #9 (METHODOLOGY_FIX_PLAN.md): when `db_path` is
    provided, each specialist's RAW confidence is replaced with a
    calibrated confidence derived from a Platt-scaling model fitted
    on that specialist's historical raw_confidence → was_correct
    pairs. Without calibration, an over-confident specialist would
    dominate ensemble contributions; with it, every specialist's
    weight reflects its empirical accuracy.

    When the calibrator hasn't been fitted yet (insufficient resolved
    data), `apply_calibration` returns the raw value unchanged so
    the ensemble degrades gracefully to pre-fix behavior.
    """
    # Reindex per (symbol, specialist) for fast lookup
    by_symbol_and_spec: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for name, verdicts in raw_by_specialist.items():
        for v in verdicts:
            by_symbol_and_spec[(v["symbol"], name)] = v

    # Pre-load calibrators once per ensemble run.
    calibrators: Dict[str, Any] = {}
    if db_path:
        try:
            from specialist_calibration import get_calibrator
            for name in raw_by_specialist:
                calibrators[name] = get_calibrator(db_path, name)
        except Exception:
            calibrators = {}

    out: Dict[str, Any] = {}
    for c in candidates:
        sym = c.get("symbol", "")
        if not sym:
            continue
        symbol_verdicts: List[Dict[str, Any]] = []
        buy_score = 0.0
        sell_score = 0.0
        vetoed = False
        veto_reason: Optional[str] = None

        for name in raw_by_specialist:
            v = by_symbol_and_spec.get((sym, name))
            if not v:
                symbol_verdicts.append({
                    "specialist": name,
                    "verdict": "ABSTAIN",
                    "confidence": 0,
                    "raw_confidence": 0,
                    "reasoning": "",
                })
                continue

            raw_conf = int(v["confidence"])
            cal = calibrators.get(name)
            if cal is not None:
                try:
                    from specialist_calibration import apply_calibration
                    eff_conf = apply_calibration(raw_conf, cal)
                except Exception:
                    eff_conf = raw_conf
            else:
                eff_conf = raw_conf

            symbol_verdicts.append({
                "specialist": name,
                "verdict": v["verdict"],
                "confidence": eff_conf,
                "raw_confidence": raw_conf,
                "reasoning": v["reasoning"],
            })

            # Apply VETO authority (from risk_assessor specifically)
            if v["verdict"] == "VETO" and name == "risk_assessor":
                vetoed = True
                veto_reason = v["reasoning"] or "risk veto"
                continue

            if eff_conf < CONFIDENCE_FLOOR:
                continue

            weight = SPECIALIST_WEIGHTS.get(name, 1.0)
            contribution = (eff_conf / 100.0) * weight
            if v["verdict"] == "BUY":
                buy_score += contribution
            elif v["verdict"] == "SELL":
                sell_score += contribution

        # Final consensus
        if vetoed:
            final_verdict = "VETO"
            final_confidence = 100
        elif buy_score == 0 and sell_score == 0:
            final_verdict = "HOLD"
            final_confidence = 0
        elif buy_score > sell_score:
            final_verdict = "BUY"
            total = buy_score + sell_score
            final_confidence = int(round(100 * buy_score / total)) if total > 0 else 0
        elif sell_score > buy_score:
            final_verdict = "SELL"
            total = buy_score + sell_score
            final_confidence = int(round(100 * sell_score / total)) if total > 0 else 0
        else:
            final_verdict = "HOLD"
            final_confidence = 50

        out[sym] = {
            "verdict": final_verdict,
            "confidence": final_confidence,
            "vetoed": vetoed,
            "veto_reason": veto_reason,
            "buy_score": round(buy_score, 3),
            "sell_score": round(sell_score, 3),
            "specialists": symbol_verdicts,
        }
    return out


def format_for_final_prompt(per_symbol: Dict[str, Any], symbol: str) -> str:
    """Compact one-liner for injection into the final-decision AI prompt."""
    entry = per_symbol.get(symbol)
    if not entry:
        return ""
    tag = entry["verdict"]
    if entry["vetoed"]:
        tag = f"VETOED by risk ({entry.get('veto_reason', '')[:60]})"
    specs = entry.get("specialists", [])
    breakdown = ", ".join(
        f"{s['specialist'][:4]}={s['verdict']}({int(s['confidence'])})"
        for s in specs
    )
    return f"ENSEMBLE: {tag} @ {entry['confidence']}% — {breakdown}"
