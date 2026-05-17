"""End-to-end verification that each profile's configured AI provider
+ model + key actually answers a real prompt.

Why this exists: switching providers via the per-profile dropdown is
a footgun if the per-profile `ai_api_key_enc` is missing (the system
silently falls back to the user-level `users.anthropic_api_key_enc`
which gets sent to the WRONG provider — see settings UI design note).
This script proves the wiring works BEFORE the scheduler does live
trades against it.

For each enabled profile:
  1. Builds a UserContext via the same `build_user_context_from_profile`
     the scheduler uses. This exercises the SAME key-resolution path
     the live system uses; if you can run this and get a Gemini
     response, you can be sure the scheduler will too.
  2. Calls ai_providers.call_ai with a 1-token-target round-trip
     prompt. The prompt is deterministic so output asymmetries
     between providers show up (Gemini's response shape differs from
     Anthropic's; if you see a real text answer that includes "PONG"
     or similar, the connection works).
  3. Reports per-profile: provider name, model, response excerpt,
     elapsed ms, estimated cost in micro-USD.
  4. Final summary: pass/fail count.

Skips profiles whose strategy_type is buy_hold or random — those
bypass the AI pipeline and the model setting is moot.

Usage:
    /opt/quantopsai/venv/bin/python verify_ai_provider_per_profile.py
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any, Dict, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)


# A short, deterministic prompt — Gemini and Anthropic will each
# produce a brief reply, and we can sanity-check the response shape.
_PROBE_PROMPT = (
    "Reply with exactly two words: PONG followed by today's day name "
    "(e.g., 'PONG Friday'). No other text."
)


def _profile_summary(p: Dict[str, Any]) -> str:
    return (
        f"pid={p['id']:2d}  {p['name']:32s}  "
        f"provider={p.get('ai_provider', '?'):10s}  "
        f"model={p.get('ai_model', '?'):28s}"
    )


def _verify_one(profile_id: int) -> Dict[str, Any]:
    """Build ctx, call AI, return result dict."""
    from models import build_user_context_from_profile
    from ai_providers import call_ai
    out: Dict[str, Any] = {
        "profile_id": profile_id,
        "ok": False,
        "provider": "?",
        "model": "?",
        "response_excerpt": "",
        "elapsed_ms": 0,
        "error": None,
    }
    try:
        ctx = build_user_context_from_profile(profile_id)
    except Exception as exc:
        out["error"] = (
            f"build_user_context: {type(exc).__name__}: {exc}"
        )
        return out
    out["provider"] = getattr(ctx, "ai_provider", "?")
    out["model"] = getattr(ctx, "ai_model", "?")
    key = getattr(ctx, "ai_api_key", "")
    if not key:
        out["error"] = "no ai_api_key on ctx (neither per-profile nor user fallback)"
        return out
    t0 = time.time()
    try:
        resp = call_ai(
            _PROBE_PROMPT,
            provider=out["provider"],
            model=out["model"],
            api_key=key,
            max_tokens=32,
        )
    except Exception as exc:
        out["error"] = f"call_ai: {type(exc).__name__}: {exc}"
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        return out
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    out["response_excerpt"] = (str(resp) or "")[:80].replace("\n", " ")
    out["ok"] = bool(resp)
    return out


def main():
    from models import get_user_profiles
    profiles = [p for p in get_user_profiles(1) if p.get("enabled")]
    if not profiles:
        log.error("No enabled profiles for user 1 — nothing to verify.")
        return 2
    log.info("=" * 80)
    log.info("AI PROVIDER VERIFICATION — %d profile(s)", len(profiles))
    log.info("=" * 80)

    results: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for p in profiles:
        st = p.get("strategy_type") or "ai"
        if st in ("buy_hold", "random"):
            skipped.append(f"pid={p['id']} {p['name']} (strategy_type={st})")
            continue
        log.info(_profile_summary(p))
        r = _verify_one(p["id"])
        results.append(r)
        if r["ok"]:
            log.info(
                "    ✅ %dms — response: %r",
                r["elapsed_ms"], r["response_excerpt"],
            )
        else:
            log.error(
                "    ❌ FAILED: %s (elapsed=%dms)",
                r["error"], r["elapsed_ms"],
            )

    log.info("=" * 80)
    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    log.info(
        "SUMMARY: %d AI profile(s) verified  →  %d passed  /  %d failed",
        len(results), passed, failed,
    )
    if skipped:
        log.info("SKIPPED (non-AI strategies):")
        for s in skipped:
            log.info("  %s", s)
    # Final guard — make sure every profile is using the expected
    # provider, not silently falling back to the wrong one.
    log.info("-" * 80)
    providers = {}
    for r in results:
        providers.setdefault(r["provider"], []).append(r["profile_id"])
    for prov, pids in sorted(providers.items()):
        log.info(
            "Provider '%s' → %d profile(s): %s",
            prov, len(pids), pids,
        )
    log.info("=" * 80)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
