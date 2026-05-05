"""Multi-provider AI abstraction — supports Anthropic, OpenAI, and Google."""

import logging
import re

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
            "gpt-4o-mini": "GPT-4o Mini (cheapest)",
            "gpt-4o": "GPT-4o (balanced)",
            "o3-mini": "o3-mini (reasoning)",
        },
    },
    "google": {
        "name": "Google (Gemini)",
        "models": {
            "gemini-2.0-flash": "Gemini 2.0 Flash (cheapest)",
            "gemini-2.5-pro-preview-03-25": "Gemini 2.5 Pro (most capable)",
        },
    },
}

# Default (cheapest) model per provider
_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "google": "gemini-2.0-flash",
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
    primary fails. Sources keys from config — only providers with a
    configured key are eligible. Order: openai → google (by default;
    skipping the primary)."""
    import config as _config
    chain = []
    candidates = [
        ("openai", _config.OPENAI_API_KEY, _config.OPENAI_MODEL),
        ("google", _config.GEMINI_API_KEY, _config.GEMINI_MODEL),
        ("anthropic", _config.ANTHROPIC_API_KEY, _config.CLAUDE_MODEL),
    ]
    for prov, key, model in candidates:
        if prov == primary_provider:
            continue
        if not key:
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
    raise ValueError(f"Unknown AI provider: {provider!r}")


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
            logged to that profile's ai_cost_ledger table.
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
    """
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown AI provider: {provider!r}. "
                         f"Supported: {', '.join(PROVIDERS.keys())}")

    if not api_key:
        raise ValueError(f"API key is required for provider {provider!r}")

    if model is None:
        model = _DEFAULT_MODELS.get(provider)

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
        # Fire-and-forget cost logging — never raise from here
        if db_path:
            try:
                from ai_cost_ledger import log_ai_call
                log_ai_call(db_path, attempt_provider, attempt_model or "?",
                            in_tok, out_tok, purpose or "")
            except Exception as exc:
                logger.debug("cost ledger skipped: %s", exc)
        return _strip_markdown_fences(response_text)

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
        except Exception:
            pass

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


def _call_google(prompt, model, api_key, max_tokens):
    """Call Google Gemini API. Returns (text, input_tokens, output_tokens)."""
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError(
            "The 'google-generativeai' package is required for the Google provider. "
            "Install it with: pip install google-generativeai"
        )

    genai.configure(api_key=api_key)
    model_obj = genai.GenerativeModel(model)
    response = model_obj.generate_content(
        prompt,
        generation_config={"max_output_tokens": max_tokens},
    )
    meta = getattr(response, "usage_metadata", None)
    in_tok = getattr(meta, "prompt_token_count", 0) if meta else 0
    out_tok = getattr(meta, "candidates_token_count", 0) if meta else 0
    return response.text, in_tok, out_tok
