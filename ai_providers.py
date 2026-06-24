"""Multi-provider AI abstraction — supports Anthropic, OpenAI, and Google."""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

PROVIDERS = {
    "anthropic": {
        "name": "Anthropic (Claude)",
        # 2026-06-24 — replaced the dated claude-sonnet-4-20250514 /
        # claude-opus-4-20250514 ids: Anthropic's live /v1/models no longer
        # lists them (deprecated) and they had no pricing entry, so they
        # showed blank cost. claude-sonnet-4-6 / claude-opus-4-6 are both
        # live AND priced.
        "models": {
            "claude-haiku-4-5-20251001": "Claude Haiku 4.5 (cheapest)",
            "claude-sonnet-4-6": "Claude Sonnet 4.6 (balanced)",
            "claude-opus-4-6": "Claude Opus 4.6 (most capable)",
        },
    },
    "openai": {
        "name": "OpenAI (GPT)",
        "models": {
            "gpt-4.1-nano": "GPT-4.1 Nano (cheapest)",
            "gpt-4o-mini": "GPT-4o Mini (cheap)",
            "gpt-4o": "GPT-4o (balanced)",
            "o3-mini": "o3-mini (reasoning)",
        },
    },
    "google": {
        "name": "Google (Gemini)",
        # 2026-06-24 — dropped gemini-2.0-flash (Google DEPRECATED it; live
        # generateContent returns 404) and added gemini-2.5-flash, the cheap
        # standard tier ($0.35/$0.70 per 1M — ~7x cheaper than Claude Haiku)
        # that was missing from the picker. Switched the pro entry to the
        # stable `gemini-2.5-pro` id.
        "models": {
            "gemini-2.5-flash-lite": "Gemini 2.5 Flash-Lite (cheapest)",
            "gemini-2.5-flash": "Gemini 2.5 Flash (cheap, reliable)",
            "gemini-3.1-flash-lite": "Gemini 3.1 Flash-Lite (newer cheap tier)",
            "gemini-2.5-pro": "Gemini 2.5 Pro (most capable)",
        },
    },
    "deepseek": {
        "name": "DeepSeek",
        "models": {
            "deepseek-chat": "DeepSeek V3.2 (cheap reasoning)",
            "deepseek-reasoner": "DeepSeek R1 (reasoning-heavy)",
        },
    },
}

# Default (cheapest) model per provider
_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4.1-nano",
    "google": "gemini-2.5-flash-lite",
    "deepseek": "deepseek-chat",
}

# Regex to strip markdown code fences (```json ... ``` or ``` ... ```)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?", re.MULTILINE)
_FENCE_END_RE = re.compile(r"\n?```\s*$", re.MULTILINE)


def get_providers(with_cost=True):
    """Return the providers dict for UI dropdowns.

    When `with_cost` (default), each model's display label is annotated with
    its per-1M-token price from `ai_pricing` so the picker shows cost inline
    — e.g. "Gemini 2.5 Flash (cheap, reliable) — $0.35 in / $0.70 out per 1M".
    Unpriced models are left with their plain label (no invented number).
    """
    if not with_cost:
        return PROVIDERS
    from ai_pricing import cost_label
    out = {}
    for pkey, pinfo in PROVIDERS.items():
        models = {}
        for mid, label in pinfo.get("models", {}).items():
            cl = cost_label(mid)
            models[mid] = "%s — %s" % (label, cl) if cl else label
        out[pkey] = {"name": pinfo["name"], "models": models}
    return out


def get_models_for_provider(provider):
    """Return {model_id: display_name} for a provider (raw labels, no cost)."""
    return PROVIDERS.get(provider, {}).get("models", {})


# ---------------------------------------------------------------------------
# Live model availability — query each provider's "list models" endpoint with
# a key that actually works, so the picker can show which models are real
# (the hardcoded list went stale: it offered the deprecated gemini-2.0-flash
# and omitted gemini-2.5-flash). Cached per-provider; degrades to "unknown"
# (None) on any failure so the picker NEVER breaks on a network hiccup.
# ---------------------------------------------------------------------------

_AVAIL_CACHE = {}          # provider -> (expiry_epoch, frozenset|None)
_AVAIL_TTL_SECONDS = 1800  # model lists change rarely; 30 min is plenty


def _working_key_for_provider(provider):
    """Find a usable API key for `provider`, in priority order:
    operator Fallback LLM key (if its provider matches) → any enabled
    trading_profile configured for this provider → env-level key. Returns
    "" when none is available."""
    import sqlite3
    from contextlib import closing
    # 1. Operator-level Fallback LLM key (users row), if same provider.
    try:
        with closing(sqlite3.connect("quantopsai.db")) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT llm_provider, anthropic_api_key_enc FROM users "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()
        if row and (row["llm_provider"] or "").strip().lower() == provider:
            from crypto import decrypt
            k = decrypt(row["anthropic_api_key_enc"] or "")
            if k:
                return k
    except Exception as exc:
        logger.debug("operator-key lookup for %s failed (%s) — trying "
                     "profile keys next", provider, exc)
    # 2. Any enabled trading_profile configured for this provider.
    try:
        with closing(sqlite3.connect("quantopsai.db")) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT ai_api_key_enc FROM trading_profiles "
                "WHERE ai_provider = ? AND ai_api_key_enc != '' LIMIT 1",
                (provider,),
            ).fetchone()
        if row:
            from crypto import decrypt
            k = decrypt(row["ai_api_key_enc"] or "")
            if k:
                return k
    except Exception as exc:
        logger.debug("profile-key lookup for %s failed (%s) — trying env "
                     "key next", provider, exc)
    # 3. Env-level key.
    try:
        import config
        return {
            "google": config.GEMINI_API_KEY,
            "anthropic": config.ANTHROPIC_API_KEY,
            "openai": config.OPENAI_API_KEY,
        }.get(provider) or ""
    except Exception as exc:
        logger.debug("env-key lookup for %s failed (%s) — no key available",
                     provider, exc)
        return ""


def _http_get_json(url, headers=None, timeout=12):
    import json
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        # A non-JSON provider response → surface as an error the caller
        # (available_model_ids) turns into "availability unknown".
        raise ValueError("non-JSON response from list-models endpoint: %s"
                         % exc)


def _live_model_ids(provider, key):
    """Query a provider's list-models endpoint → set of model ids. Raises on
    any HTTP/parse failure (caller treats as 'unknown')."""
    if provider == "google":
        data = _http_get_json(
            "https://generativelanguage.googleapis.com/v1beta/models"
            "?key=%s&pageSize=400" % key)
        ids = set()
        for m in data.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                ids.add(m.get("name", "").replace("models/", ""))
        return ids
    if provider == "anthropic":
        data = _http_get_json(
            "https://api.anthropic.com/v1/models?limit=1000",
            headers={"x-api-key": key,
                     "anthropic-version": "2023-06-01"})
        return {m.get("id") for m in data.get("data", []) if m.get("id")}
    if provider == "openai":
        data = _http_get_json(
            "https://api.openai.com/v1/models",
            headers={"Authorization": "Bearer %s" % key})
        return {m.get("id") for m in data.get("data", []) if m.get("id")}
    # deepseek and any other: no cheap list endpoint wired — unknown.
    raise ValueError("no list-models endpoint for provider %r" % provider)


def available_model_ids(provider, force=False):
    """Return a frozenset of model ids the provider currently lists as
    callable, or None if it can't be determined (no key / network error).
    Cached for `_AVAIL_TTL_SECONDS`."""
    import time
    now = time.time()
    cached = _AVAIL_CACHE.get(provider)
    if cached and not force and cached[0] > now:
        return cached[1]
    result = None
    key = _working_key_for_provider(provider)
    if key:
        try:
            result = frozenset(_live_model_ids(provider, key))
        except Exception as exc:
            logger.info("availability check for %s failed (%s: %s) — "
                        "picker will show models without an availability "
                        "badge", provider, type(exc).__name__, exc)
            result = None
    _AVAIL_CACHE[provider] = (now + _AVAIL_TTL_SECONDS, result)
    return result


def get_model_catalog(provider, check_availability=True):
    """Rich model list for the picker: each entry has id, label, the cost
    label, the raw input/output $/M prices, and an `available` flag
    (True/False, or None when availability can't be determined).

    Drives the Settings model dropdown so the operator sees cost AND which
    models actually work before committing to one.
    """
    from ai_pricing import cost_label, price_for
    models = PROVIDERS.get(provider, {}).get("models", {})
    live = available_model_ids(provider) if check_availability else None
    out = []
    for mid, label in models.items():
        price = price_for(mid)
        out.append({
            "id": mid,
            "label": label,
            "cost_label": cost_label(mid),
            "input_per_m": price["input"] if price else None,
            "output_per_m": price["output"] if price else None,
            "available": (mid in live) if live is not None else None,
        })
    return out


def get_provider_for_model(model_id):
    """Look up which provider a model belongs to by checking PROVIDERS dict.

    Returns provider name (e.g. "anthropic", "openai", "google") or None.
    """
    if not model_id:
        return None
    for provider_key, provider_info in PROVIDERS.items():
        if model_id in provider_info.get("models", {}):
            return provider_key
    return None


def _strip_markdown_fences(text):
    """Remove markdown code fences and extract the first complete JSON object.

    Handles all known provider quirks:
    - Haiku/Sonnet: wraps JSON in ```json ... ```
    - GPT models: sometimes adds preamble text before JSON
    - Gemini: may add explanation after the JSON
    - Any model: extra text after the closing }
    """
    text = text.strip()

    # Strip markdown fences
    if "```" in text:
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Extract the first complete JSON object using brace matching
    if "{" in text:
        start = text.index("{")
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]

    return text


# ---------------------------------------------------------------------------
# Circuit-breaker / failover
# ---------------------------------------------------------------------------

# 2026-05-19 — in-call retry tuning. When a provider returns a
# transient failure (503 / 504 / 529 / timeout / overload), the
# SAME provider gets retried this many times with these sleeps
# (seconds) between attempts before we move to the fallback chain
# or trip the circuit. Two retries with 2s + 4s catches the
# overwhelming majority of Google's "service unavailable" 503s
# (per their "spikes in demand are usually temporary" note) without
# meaningful added latency on success. Tests monkeypatch this to
# () to disable sleeps.
_RETRY_DELAYS_SECONDS = (2.0, 4.0)


def _is_transient_failure(exc: BaseException) -> bool:
    """True when the exception looks like a provider-overload / 5xx /
    network-timeout — the kind that should TRIP the circuit. Auth
    errors and bad-input errors do NOT trip (we'd just fail forever)."""
    msg = str(exc).lower()
    transient_markers = (
        "529", "503", "502", "504", "overloaded", "overload_error",
        "timeout", "timed out", "connection", "service unavailable",
        "internal server error", "rate limit", "too many requests",
    )
    return any(m in msg for m in transient_markers)


class AIProviderUnavailable(RuntimeError):
    """Raised when every eligible provider in the chain was skipped
    (circuit open, no key, fallback gate suppression) — no actual call
    was made and no exception was raised by a provider. Distinct from
    a real `RuntimeError` so callers (ai_analyst, dashboard renderer)
    can present this as a transient "waiting for AI provider" state
    rather than a "system error."

    The exception carries `skip_reasons` (list of human-readable
    strings, one per provider that was skipped) and `next_retry_hint`
    (optional seconds-until-circuit-reset for the primary). These let
    consumers build informative UI without re-deriving the state.
    """

    def __init__(self, message: str, skip_reasons=None,
                 next_retry_hint=None):
        super().__init__(message)
        self.skip_reasons = list(skip_reasons or [])
        self.next_retry_hint = next_retry_hint


def _resolve_operator_fallback_model(primary_provider: str,
                                      primary_model: Optional[str]):
    """Look up the operator-configured fallback model (Settings → Fallback
    LLM). May be ANY provider — including one different from the primary.

    2026-05-21 — added so the operator can configure (via Settings →
    Fallback LLM Model) a more-reliable model the chain will try BEFORE
    giving up on the primary.

    2026-06-24 — generalized to allow a DIFFERENT provider than the primary
    (e.g. primary google/gemini-2.5-flash-lite, fallback anthropic/claude-
    haiku). This is an EXPLICIT operator choice, which is NOT the *silent*
    cross-provider fallback the anthropic-spend gate (`_build_fallback_chain`)
    blocks — the operator deliberately picked it, so it is honored regardless
    of provider, using the Fallback LLM Key they configured alongside it.

    Typical use: profile primary is `gemini-2.5-flash-lite` (cheap,
    heavily throttled by Google); operator sets the fallback model to
    `gemini-2.5-flash` (same provider) OR `claude-haiku` (different
    provider, its own key). When the primary trips its (provider, model)
    circuit, the chain tries the operator fallback before giving up.

    Returns a tuple `(fallback_model, fallback_api_key)`, or None when:
      - The user hasn't picked a model
      - The user's chosen llm_provider doesn't match the primary
      - The fallback model equals the primary model (no-op)
      - The user hasn't set a Fallback LLM Key (no credential to use)
      - The DB lookup fails (no master DB on test fixtures, etc.)

    The Fallback LLM Key (stored in `users.anthropic_api_key_enc` —
    the column name is historical from when the field was Anthropic-
    specific; it now holds the key for whichever provider llm_provider
    selects) IS the credential used. We do NOT reuse the caller's
    per-profile key here because that would route the operator's
    "fallback" plumbing through a key they may not want to use for
    fallback (e.g., a profile that uses a paper-tier Gemini key,
    where the operator's user-level fallback key targets a higher-
    tier billing account with looser rate limits).

    Note: this is a single-row read from quantopsai.db. Cached values
    aren't needed — the read is ~ms and only happens on chain-build.
    """
    try:
        import sqlite3 as _sq3
        from contextlib import closing as _closing
        with _closing(_sq3.connect("quantopsai.db")) as conn:
            conn.row_factory = _sq3.Row
            row = conn.execute(
                "SELECT llm_provider, llm_model, anthropic_api_key_enc "
                "FROM users ORDER BY id ASC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        fb_provider = (row["llm_provider"] or "").strip().lower()
        fb_model = (row["llm_model"] or "").strip()
        if not fb_provider or not fb_model:
            return None
        if (fb_provider == primary_provider and primary_model
                and fb_model == primary_model):
            # Operator picked the EXACT same provider+model for primary +
            # fallback — nothing to gain; skip the no-op entry. (A different
            # model on the same provider, or any other provider, is kept.)
            return None
        # Decrypt the fallback key (stored encrypted in the legacy-
        # named `anthropic_api_key_enc` column). Without a key we
        # can't actually call the provider, so suppress the chain
        # entry instead of returning a key-less tuple that would
        # blow up later.
        try:
            from crypto import decrypt as _decrypt
            enc = row["anthropic_api_key_enc"] or ""
            fb_key = _decrypt(enc) if enc else ""
        except Exception as _decrypt_exc:
            logger.warning(
                "operator fallback key decrypt failed (%s: %s) — "
                "chain will skip the operator fallback entry",
                type(_decrypt_exc).__name__, _decrypt_exc,
            )
            return None
        if not fb_key:
            logger.info(
                "operator fallback model is set (%s/%s) but no "
                "Fallback LLM Key configured — skipping. Set the key "
                "in Settings → Fallback LLM Key to enable.",
                fb_provider, fb_model,
            )
            return None
        return (fb_provider, fb_model, fb_key)
    except Exception as exc:
        logger.debug(
            "operator fallback lookup failed (%s: %s) — chain "
            "will proceed with cross-provider fallbacks only",
            type(exc).__name__, exc,
        )
        return None


def _build_fallback_chain(primary_provider: str,
                           primary_model: Optional[str] = None):
    """Return list of (provider, api_key, model) tuples to try after
    primary fails.

    Default policy (2026-05-19): NEVER silently fall back to Anthropic
    from a non-Anthropic primary. Profiles configured for Gemini or
    OpenAI made that choice deliberately (paid Gemini included — the
    user is on the paid tier and still does not want silent Claude
    spend); a transient primary failure must fail loudly rather than
    secretly route to Claude. The fallback chain is therefore filtered:
      - Anthropic is excluded unless `AI_ALLOW_ANTHROPIC_FALLBACK=1`
        is set in the environment (opt-in escape hatch).
      - Anthropic IS still in the chain when it is the primary —
        i.e., a profile explicitly configured for Anthropic can still
        fall back to itself via a different model is not the concern;
        this gate only affects cross-provider fallback.

    Was the 2026-05-19 incident: Gemini 503s opened the google
    circuit; every subsequent call fell back to Anthropic at $0.01-
    $0.02/call across batch_select + 4 ensemble specialists. Profile
    16 alone accumulated ~$0.11 in unauthorized Anthropic spend
    before the fix landed."""
    import os
    import config as _config
    chain = []
    candidates = [
        ("openai", _config.OPENAI_API_KEY, _config.OPENAI_MODEL),
        ("google", _config.GEMINI_API_KEY, _config.GEMINI_MODEL),
        ("anthropic", _config.ANTHROPIC_API_KEY, _config.CLAUDE_MODEL),
    ]
    # 2026-05-21 / 2026-06-24 — OPERATOR-CONFIGURED fallback model
    # (Settings → Fallback LLM). Inserted at the head of the chain (BEFORE
    # the cross-provider candidates below) so the operator's explicit pick
    # is tried first when the primary model's circuit trips. May be the same
    # provider (e.g. a more-reliable tier) OR a different provider entirely
    # (e.g. anthropic/claude-haiku) — a DELIBERATE operator choice, so it is
    # NOT subject to the anthropic-spend gate below (that gate only blocks
    # *silent* cross-provider fallback from the config-level candidates).
    # Uses the operator's user-level Fallback LLM Key (Settings → Fallback
    # LLM section) — independent of the per-profile API key used by primary.
    fb_resolution = _resolve_operator_fallback_model(
        primary_provider, primary_model,
    )
    if fb_resolution is not None:
        fb_provider, fb_model, fb_key = fb_resolution
        chain.append((fb_provider, fb_key, fb_model))
        logger.info(
            "Operator fallback enabled: %s/%s -> %s/%s",
            primary_provider, primary_model or "(default)",
            fb_provider, fb_model,
        )

    allow_anthropic_fallback = (
        os.getenv("AI_ALLOW_ANTHROPIC_FALLBACK", "").strip() == "1"
    )
    for prov, key, model in candidates:
        if prov == primary_provider:
            continue
        if not key:
            continue
        if prov == "anthropic" and not allow_anthropic_fallback:
            # Explicit gate — profiles configured for Gemini/OpenAI
            # must NOT silently route to paid Claude when their primary
            # has a transient outage. Logging the skip so dashboard
            # surfaces "fallback blocked" instead of the call vanishing.
            logger.warning(
                "AI fallback to anthropic SUPPRESSED for primary=%s "
                "(set AI_ALLOW_ANTHROPIC_FALLBACK=1 to allow paid "
                "fallback to Claude)",
                primary_provider,
            )
            continue
        chain.append((prov, key, model))
    return chain


def _enumerate_chain_skip_reasons(primary_provider: str):
    """Companion to `_build_fallback_chain` — returns a list of
    human-readable strings describing every provider that COULD have
    been in the chain but was excluded, and why.

    Used by `call_ai` when the whole chain is exhausted, to build a
    diagnostic that explains WHICH providers were tried/skipped and
    WHY. Without this, a chain exhausted by circuit-open + gate
    suppression produces the misleading "Last error: None" (because
    no provider was actually called — every one was skipped before
    the call).
    """
    import os
    import config as _config
    notes = []
    allow_anthropic_fallback = (
        os.getenv("AI_ALLOW_ANTHROPIC_FALLBACK", "").strip() == "1"
    )
    candidates = [
        ("openai", _config.OPENAI_API_KEY),
        ("google", _config.GEMINI_API_KEY),
        ("anthropic", _config.ANTHROPIC_API_KEY),
    ]
    for prov, key in candidates:
        if prov == primary_provider:
            continue
        if not key:
            notes.append(f"{prov}: not configured (no API key in env)")
            continue
        if prov == "anthropic" and not allow_anthropic_fallback:
            notes.append(
                "anthropic: fallback suppressed (paid-provider gate; "
                "set AI_ALLOW_ANTHROPIC_FALLBACK=1 to allow)"
            )
            continue
    return notes


def _call_provider(provider, prompt, model, api_key, max_tokens):
    """Dispatch to provider-specific helper. Returns (text, in_tok, out_tok)."""
    if provider == "anthropic":
        return _call_anthropic(prompt, model, api_key, max_tokens)
    if provider == "openai":
        return _call_openai(prompt, model, api_key, max_tokens)
    if provider == "google":
        return _call_google(prompt, model, api_key, max_tokens)
    if provider == "deepseek":
        return _call_deepseek(prompt, model, api_key, max_tokens)
    raise ValueError(f"Unknown AI provider: {provider!r}")


def _enforce_cost_cap(prompt: str, model: Optional[str], max_tokens: int,
                       db_path: Optional[str], purpose: Optional[str]) -> None:
    """Pre-call gate: if `db_path` resolves to a user_id, and the
    worst-case cost of this call would push today's spend past that
    user's daily ceiling, raise CostCapExceeded so the call never hits
    the provider.

    Worst-case estimate: input ≈ len(prompt) // 3 chars-to-tokens
    (overestimate vs the typical 3.5-4 ratio so we err on the side of
    blocking, not letting through), output = max_tokens (the actual
    upper bound we permitted). Priced via ai_pricing.estimate_cost_usd.

    Falls open (call proceeds) if db_path is missing or doesn't map to
    a known user — there's no way to attribute spend to a ceiling
    without a user_id, and silently routing to the wrong user's cap
    would be worse than no cap at all.
    """
    from cost_guard import (
        user_id_for_db_path, can_afford_action, CostCapExceeded,
    )
    from ai_pricing import estimate_cost_usd

    user_id = user_id_for_db_path(db_path) if db_path else None
    if user_id is None:
        return  # No user attribution → can't enforce per-user cap

    est_input_tokens = max(1, len(prompt) // 3)
    est_output_tokens = max(1, int(max_tokens or 0))
    est_cost = estimate_cost_usd(model, est_input_tokens, est_output_tokens)

    if not can_afford_action(user_id, est_cost):
        # Surface to activity_log so it shows on the dashboard. Look
        # up the profile_id from db_path so the log entry is scoped
        # correctly. Fire-and-forget — a logging failure must NOT
        # turn into a "cap fires but we ate the call anyway" bug.
        m = re.search(r"profile_(\d+)\.db$", db_path or "")
        if m:
            try:
                from models import log_activity
                log_activity(
                    profile_id=int(m.group(1)),
                    user_id=user_id,
                    activity_type="cost_cap_blocked",
                    title="Daily cost cap reached — AI call blocked",
                    detail=(
                        f"{purpose or 'AI call'}: would add ${est_cost:.4f} "
                        f"(est {est_input_tokens}+{est_output_tokens} tokens). "
                        "Set a higher ceiling on the settings page if this "
                        "is intentional."
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "cost cap fired but activity_log write failed: %s: %s",
                    type(exc).__name__, exc,
                )
        logger.warning(
            "Cost cap blocked %s for user %d: est $%.4f would exceed ceiling",
            purpose or "AI call", user_id, est_cost,
        )
        raise CostCapExceeded(
            user_id, est_cost,
            action_summary=f"{purpose or 'AI call'} (~{est_input_tokens}+"
                            f"{est_output_tokens} tokens)",
        )


def call_ai(prompt, provider="anthropic", model=None, api_key=None, max_tokens=1024,
            db_path=None, purpose=None):
    """Send a prompt to the specified AI provider and return the response text.

    Args:
        prompt: The user prompt string
        provider: "anthropic", "openai", or "google"
        model: Model ID string (if None, uses cheapest for provider)
        api_key: API key for the provider
        max_tokens: Max response tokens
        db_path: Optional per-profile DB path. When provided, the call is
            logged to that profile's ai_cost_ledger table AND the daily
            cost cap (from `cost_guard`) is enforced — callers without
            a db_path bypass the cap (no way to attribute to a user).
        purpose: Optional short tag (e.g., "ensemble:earnings_analyst",
            "batch_select", "sec_diff") for the ledger — lets the dashboard
            break spend down by what the call was for.

    Failover behavior:
        - Each provider has a per-process circuit (provider_circuit).
          Three consecutive 5xx/timeout failures OPEN the circuit for
          5 minutes (exponential backoff up to 30 min on repeated
          half-open failures).
        - If the requested provider's circuit is OPEN, requests
          automatically route to the first configured fallback
          (config.OPENAI_API_KEY / config.GEMINI_API_KEY).
        - If the active provider call raises a transient error, we
          immediately try the next provider in the fallback chain
          before giving up.
        - When NO fallback is configured (only Anthropic key set), the
          circuit still opens to surface the issue but we have nowhere
          to fall back to — the call raises and the caller's existing
          error handling takes over.

    Returns:
        str: The raw response text (with markdown fences stripped)

    Raises:
        ValueError: If provider is unknown or api_key is missing
        RuntimeError: If primary + every fallback raise transient failures
        CostCapExceeded: If db_path resolves to a user whose daily
            spend ceiling would be exceeded by this call's worst-case
            cost. Caught by the pipeline's existing Exception handler
            so the cycle skips this call instead of crashing.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown AI provider: {provider!r}. "
                         f"Supported: {', '.join(PROVIDERS.keys())}")

    if not api_key:
        raise ValueError(f"API key is required for provider {provider!r}")

    if model is None:
        model = _DEFAULT_MODELS.get(provider)

    # Cost cap (added 2026-05-15 — pipeline-wide hard stop). Raises
    # CostCapExceeded when over budget; falls open when db_path can't
    # be attributed to a user.
    _enforce_cost_cap(prompt, model, max_tokens, db_path, purpose)

    # Build attempt order: primary first, then fallbacks. If primary
    # circuit is currently OPEN, primary is skipped — but we still
    # include it in the list at position 0 so callers see "tried
    # primary" semantics in logs (and so HALF_OPEN fall-throughs work).
    from provider_circuit import (
        is_open as _circuit_is_open,
        record_success as _circuit_record_success,
        record_failure as _circuit_record_failure,
    )

    fallback_chain = _build_fallback_chain(provider, model)
    attempts = [(provider, api_key, model)] + fallback_chain
    last_exc: BaseException = None
    # Track every provider's skip reason for the diagnostic that
    # gets raised if the entire chain ends up skipped without any
    # actual call being attempted (the "Last error: None" case).
    skip_reasons = []

    for attempt_provider, attempt_key, attempt_model in attempts:
        # Skip a (provider, model) whose circuit is currently OPEN
        # (cool-down not elapsed). HALF_OPEN passes through. Circuits
        # are keyed per-(provider, model) as of 2026-05-21 — a single
        # throttled model no longer locks out same-provider fallback
        # models the operator explicitly configured.
        if _circuit_is_open(attempt_provider, attempt_model):
            logger.info(
                "AI failover: skipping %s/%s — circuit OPEN",
                attempt_provider, attempt_model,
            )
            # Try to surface the cool-down remaining so the diagnostic
            # tells operators when to expect recovery.
            try:
                from provider_circuit import seconds_until_close
                remaining = seconds_until_close(
                    attempt_provider, attempt_model)
                _circuit_id = f"{attempt_provider}/{attempt_model}"
                if remaining:
                    skip_reasons.append(
                        f"{_circuit_id}: circuit OPEN "
                        f"(retry in ~{int(remaining)}s)"
                    )
                else:
                    skip_reasons.append(f"{_circuit_id}: circuit OPEN")
            except Exception:
                skip_reasons.append(
                    f"{attempt_provider}/{attempt_model}: circuit OPEN")
            continue
        logger.info(
            "Calling AI: provider=%s, model=%s, max_tokens=%d",
            attempt_provider, attempt_model, max_tokens,
        )
        # In-call retry on transient failures (2026-05-19). 503s and
        # similar are usually temporary — Google's own error body says
        # "spikes in demand are usually temporary, please try again
        # later." So before failing over to a different provider (or
        # tripping the circuit), retry the SAME provider after a
        # short sleep. Non-transient errors (auth, bad input) skip
        # the retry loop and propagate immediately.
        import time as _time
        response_text = in_tok = out_tok = None
        per_call_last_exc = None
        # Build the attempt schedule: first try is at delay=0, then
        # each entry of _RETRY_DELAYS_SECONDS is a sleep-before-retry.
        attempt_schedule = [0.0] + list(_RETRY_DELAYS_SECONDS)
        for retry_idx, sleep_seconds in enumerate(attempt_schedule):
            if sleep_seconds > 0:
                logger.info(
                    "AI retry %d/%d for %s after %.1fs sleep "
                    "(transient: %s)",
                    retry_idx, len(_RETRY_DELAYS_SECONDS),
                    attempt_provider, sleep_seconds,
                    per_call_last_exc,
                )
                _time.sleep(sleep_seconds)
            try:
                response_text, in_tok, out_tok = _call_provider(
                    attempt_provider, prompt, attempt_model,
                    attempt_key, max_tokens,
                )
                per_call_last_exc = None
                break  # success — drop out of the retry loop
            except Exception as call_exc:
                if not _is_transient_failure(call_exc):
                    # Non-transient (auth / bad input / unknown) —
                    # propagate immediately, don't waste retries.
                    raise
                per_call_last_exc = call_exc
                # On the final attempt this exits the for-loop and we
                # fall through to the post-loop handler below.
        if per_call_last_exc is not None:
            # All retries on this provider returned transient errors.
            # NOW record the failure (one circuit-tick per provider
            # call, not per HTTP retry — the circuit ticks per cycle
            # of failure, not per sub-second retry).
            _circuit_record_failure(
                attempt_provider, per_call_last_exc, attempt_model)
            last_exc = per_call_last_exc
            skip_reasons.append(
                f"{attempt_provider}: {len(_RETRY_DELAYS_SECONDS)+1} "
                f"attempts all transient (last: {per_call_last_exc})"
            )
            if attempt_provider != provider:
                logger.warning(
                    "AI fallback %s also failed (transient after "
                    "%d retries): %s",
                    attempt_provider, len(_RETRY_DELAYS_SECONDS),
                    per_call_last_exc,
                )
            continue

        # Fall through to the original success path that follows.
        # Success path
        _circuit_record_success(attempt_provider, attempt_model)
        if attempt_provider != provider:
            logger.warning(
                "AI failover: primary %s circuit open, served by %s",
                provider, attempt_provider,
            )
        cleaned_response = _strip_markdown_fences(response_text)

        # Shadow model evaluation — fire candidate models in parallel.
        # Returns the call_id used to join shadow rows to this primary
        # call's ledger entry, or None when shadow eval is disabled /
        # not configured for this profile. Operational behavior is
        # unchanged: we return cleaned_response regardless of what
        # shadow eval does (or fails to do).
        call_id = None
        try:
            from shadow_eval import dispatch_shadow_calls
            call_id = dispatch_shadow_calls(
                db_path=db_path,
                prompt=prompt,
                max_tokens=max_tokens,
                purpose=purpose,
                primary_provider=attempt_provider,
                primary_model=attempt_model or "?",
                primary_response=cleaned_response,
            )
        except Exception as exc:
            logger.debug("shadow eval dispatch skipped: %s", exc)

        # Fire-and-forget cost logging — never raise from here
        if db_path:
            try:
                from ai_cost_ledger import log_ai_call
                log_ai_call(db_path, attempt_provider, attempt_model or "?",
                            in_tok, out_tok, purpose or "",
                            call_id=call_id)
            except Exception as exc:
                logger.debug("cost ledger skipped: %s", exc)
        return cleaned_response

    # Every attempt either had its circuit open or raised transient.
    # Build a diagnostic that names each provider and its skip reason.
    # The legacy "Last error: None" failure mode came from skipping
    # every provider (circuit_open + gate suppression) without
    # actually CALLING any — last_exc stayed None, so the original
    # error string was useless. Now we surface what we know.
    suppression_notes = _enumerate_chain_skip_reasons(provider)
    # Combine actively-tried skips with suppression-time skips
    all_reasons = list(skip_reasons) + suppression_notes
    if last_exc is not None:
        # Truncate the last exception (Google error bodies are long).
        last_str = str(last_exc)
        if len(last_str) > 200:
            last_str = last_str[:200] + "…"
        all_reasons.append(f"last actual error: {last_str}")
    reason_text = "; ".join(all_reasons) if all_reasons else "no providers eligible"
    # When the only reason for failure was circuit-open + gate
    # suppression (no actual error from any provider), surface this
    # as a transient unavailability rather than a hard failure.
    is_transient_unavailable = (
        last_exc is None
        and any("circuit OPEN" in r for r in skip_reasons)
    )
    primary_retry_hint = None
    if is_transient_unavailable:
        try:
            from provider_circuit import seconds_until_close
            # 2026-05-21 — circuits are keyed per-(provider, model)
            # now, so the retry hint for the primary must include the
            # specific model the caller picked.
            primary_retry_hint = seconds_until_close(provider, model)
        except Exception:
            primary_retry_hint = None
    raise AIProviderUnavailable(
        f"AI provider chain exhausted: {reason_text}",
        skip_reasons=all_reasons,
        next_retry_hint=primary_retry_hint,
    )


def call_ai_structured(prompt, schema, tool_name="emit",
                        provider="anthropic", model=None, api_key=None,
                        max_tokens=4096,
                        db_path=None, purpose=None):
    """Force a structured JSON response matching `schema` via tool_use.

    Solves the Haiku-drops-candidates bug: when asked for an array in a
    normal prompt, Haiku sometimes returns a single object or truncated
    list. Tool-use forces the model to call a function with an argument
    matching the schema — the SDK returns a validated dict, no parsing.

    Currently implemented for Anthropic only. OpenAI/Google fall back to
    a plain call_ai and the caller must parse normally.

    Returns
    -------
    dict (the tool input), or None on failure.
    """
    if provider != "anthropic":
        # Fallback: plain text call, caller parses
        raw = call_ai(prompt, provider=provider, model=model,
                      api_key=api_key, max_tokens=max_tokens,
                      db_path=db_path, purpose=purpose)
        try:
            import json as _json
            return _json.loads(raw)
        except Exception:
            return None

    if not api_key:
        raise ValueError("api_key required")
    if model is None:
        model = _DEFAULT_MODELS.get("anthropic")

    # Cost cap (added 2026-05-15 — pipeline-wide hard stop). Same gate
    # as call_ai. Raises CostCapExceeded when over budget.
    _enforce_cost_cap(prompt, model, max_tokens, db_path, purpose)

    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "The 'anthropic' package is required. pip install anthropic"
        )

    client = anthropic.Anthropic(api_key=api_key)
    tool_spec = {
        "name": tool_name,
        "description": "Submit the structured result for this request.",
        "input_schema": schema,
    }
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[tool_spec],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": prompt}],
    )

    # Cost logging
    usage = getattr(message, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) if usage else 0
    out_tok = getattr(usage, "output_tokens", 0) if usage else 0
    if db_path:
        try:
            from ai_cost_ledger import log_ai_call
            log_ai_call(db_path, "anthropic", model or "?",
                        in_tok, out_tok, purpose or "")
        except (ImportError, AttributeError, OSError) as _cl_exc:
            # ai_cost_ledger telemetry write; AI call result already
            # returned to caller. Surface for follow-up so cost
            # tracking gaps are diagnosed.
            logger.debug(
                "ai_cost_ledger telemetry write failed: %s: %s",
                type(_cl_exc).__name__, _cl_exc,
            )

    # Find the tool_use block and return its input
    for block in message.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(getattr(block, "input", {}) or {})
    return None


def _call_anthropic(prompt, model, api_key, max_tokens):
    """Call Anthropic Claude API. Returns (text, input_tokens, output_tokens)."""
    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "The 'anthropic' package is required for the Anthropic provider. "
            "Install it with: pip install anthropic"
        )

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = getattr(message, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) if usage else 0
    out_tok = getattr(usage, "output_tokens", 0) if usage else 0
    return message.content[0].text, in_tok, out_tok


def _call_openai(prompt, model, api_key, max_tokens):
    """Call OpenAI API. Returns (text, input_tokens, output_tokens)."""
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "The 'openai' package is required for the OpenAI provider. "
            "Install it with: pip install openai"
        )

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = getattr(response, "usage", None)
    in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
    out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
    return response.choices[0].message.content, in_tok, out_tok


def _call_deepseek(prompt, model, api_key, max_tokens):
    """Call DeepSeek API via the OpenAI-compatible endpoint.

    DeepSeek exposes a Chat Completions endpoint at
    https://api.deepseek.com — the official OpenAI Python SDK works
    against it by overriding `base_url`. No separate dependency
    needed; reuses the `openai` package the OpenAI provider already
    requires.

    Returns (text, input_tokens, output_tokens).
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "The 'openai' package is required for the DeepSeek provider "
            "(DeepSeek uses an OpenAI-compatible endpoint). "
            "Install it with: pip install openai"
        )

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = getattr(response, "usage", None)
    in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
    out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
    return response.choices[0].message.content, in_tok, out_tok


def _call_google(prompt, model, api_key, max_tokens):
    """Call Google Gemini API. Returns (text, input_tokens, output_tokens).

    Uses the new `google-genai` SDK (replaces the deprecated
    `google-generativeai`). API surface verified 2026-05-17 against
    google-genai 1.47.0:
      - Client init:  genai.Client(api_key=key)
      - Generate:     client.models.generate_content(model=, contents=, config=)
      - Response:     .text + .usage_metadata.{prompt,candidates}_token_count
                      (same field names as the old SDK — drop-in)
      - `config` accepts a dict mapping with keys matching
        types.GenerateContentConfig (e.g. max_output_tokens).
    """
    try:
        from google import genai
    except ImportError:
        raise ImportError(
            "The 'google-genai' package is required for the Google provider. "
            "Install it with: pip install google-genai"
        )

    client = genai.Client(api_key=api_key)
    # response_mime_type forces strict JSON. Without it, gemini-2.5-flash-lite
    # intermittently returns markdown ("Here's an evaluation…"), which downstream
    # parsers reject with JSONDecodeError → retry cascade → ~10× cycle slowdown
    # (observed 2026-05-20: pid21-24 stuck for 13+ min/cycle).
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={
            "max_output_tokens": max_tokens,
            "response_mime_type": "application/json",
        },
    )
    meta = getattr(response, "usage_metadata", None)
    in_tok = getattr(meta, "prompt_token_count", 0) if meta else 0
    out_tok = getattr(meta, "candidates_token_count", 0) if meta else 0
    return response.text, in_tok, out_tok
