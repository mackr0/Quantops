"""Multi-provider AI abstraction — supports Anthropic, OpenAI, and Google."""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

PROVIDERS = {
    "anthropic": {
        "name": "Anthropic (Claude)",
        "models": {
            "claude-haiku-4-5-20251001": "Claude Haiku 4.5 (cheapest)",
            "claude-sonnet-4-20250514": "Claude Sonnet 4 (balanced)",
            "claude-opus-4-20250514": "Claude Opus 4 (most capable)",
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
        "models": {
            "gemini-2.5-flash-lite": "Gemini 2.5 Flash-Lite (cheapest)",
            "gemini-3.1-flash-lite": "Gemini 3.1 Flash-Lite (newer cheap tier)",
            "gemini-2.0-flash": "Gemini 2.0 Flash",
            "gemini-2.5-pro-preview-03-25": "Gemini 2.5 Pro (most capable)",
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


def get_providers():
    """Return the providers dict for UI dropdowns."""
    return PROVIDERS


def get_models_for_provider(provider):
    """Return {model_id: display_name} for a provider."""
    return PROVIDERS.get(provider, {}).get("models", {})


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


def _build_fallback_chain(primary_provider: str):
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

    fallback_chain = _build_fallback_chain(provider)
    attempts = [(provider, api_key, model)] + fallback_chain
    last_exc: BaseException = None

    for attempt_provider, attempt_key, attempt_model in attempts:
        # Skip a provider whose circuit is currently OPEN (cool-down
        # not elapsed). HALF_OPEN passes through.
        if _circuit_is_open(attempt_provider):
            logger.info(
                "AI failover: skipping %s — circuit OPEN", attempt_provider,
            )
            continue
        logger.info(
            "Calling AI: provider=%s, model=%s, max_tokens=%d",
            attempt_provider, attempt_model, max_tokens,
        )
        try:
            response_text, in_tok, out_tok = _call_provider(
                attempt_provider, prompt, attempt_model, attempt_key,
                max_tokens,
            )
        except Exception as exc:
            if _is_transient_failure(exc):
                _circuit_record_failure(attempt_provider, exc)
                last_exc = exc
                if attempt_provider != provider:
                    logger.warning(
                        "AI fallback %s also failed (transient): %s",
                        attempt_provider, exc,
                    )
                continue
            # Non-transient failures (auth, bad input, etc.) — don't
            # trip the circuit, just propagate.
            raise

        # Success path
        _circuit_record_success(attempt_provider)
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
    raise RuntimeError(
        f"AI provider chain exhausted ({len(attempts)} attempts). "
        f"Last error: {last_exc}"
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
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"max_output_tokens": max_tokens},
    )
    meta = getattr(response, "usage_metadata", None)
    in_tok = getattr(meta, "prompt_token_count", 0) if meta else 0
    out_tok = getattr(meta, "candidates_token_count", 0) if meta else 0
    return response.text, in_tok, out_tok
