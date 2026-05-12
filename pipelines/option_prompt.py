"""Option-aware AI prompt builder.

Phase 3 of the instrument-class pipeline refactor (2026-05-11).
See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md`.

Renders option-specific features (IV rank, Greeks, days-to-expiry,
strike, spread max-loss/max-gain, contract bid-ask) ALONGSIDE the
underlying's stock technicals. Closes audit finding #4: option
proposals previously saw only stock technicals; now they get the
fundamental option context the AI needs to make defined-risk
decisions.

Used by `pipelines.option.OptionPipeline.build_prompt()`.

This is the minimum-viable per-pipeline prompt. The legacy
`ai_analyst._build_batch_prompt` continues to handle the
production-running prompt for now; Phase 4+ will route the
dispatcher through these new builders. Until then, this builder
exists as a CAPABILITY ready to be wired up.
"""
from __future__ import annotations

import json
from typing import List


def build_prompt(ctx, candidates: List) -> str:
    """Render an option-aware AI prompt for the given candidates.

    Each candidate is a `pipelines.Candidate` instance. Option
    candidates' `extra` dict typically carries: iv_rank, dte,
    strike, spread_max_loss, spread_max_gain, delta, gamma, theta,
    plus the underlying's technicals (rsi, sector_momentum, etc.).

    Critically — UNLIKE the stock prompt, option features (IV,
    Greeks, DTE, strike, spread economics) ARE included. The AI
    can't propose a sensible defined-risk spread without seeing
    these.
    """
    if not candidates:
        return _empty_prompt(ctx)
    rendered_candidates = []
    for c in candidates:
        option_block = _option_features_first(c.extra or {})
        rendered_candidates.append(
            f"- **{c.symbol}** "
            f"({c.signal}, score {c.score:.2f}, "
            f"underlying ${c.price:.2f}): "
            f"{json.dumps(option_block)}"
        )
    return (
        f"You are evaluating OPTION candidates for "
        f"{getattr(ctx, 'segment', 'this profile')}.\n\n"
        f"For each candidate decide MULTILEG_OPEN with a strategy "
        f"name (bull_call_spread / bull_put_spread / bear_call_spread "
        f"/ bear_put_spread / iron_condor / etc.), strikes, expiry, "
        f"and contract count, OR HOLD. Cite the IV rank, DTE, and "
        f"the spread's max-loss/max-gain in the reasoning so the "
        f"defined-risk economics are explicit.\n\n"
        f"Candidates:\n"
        + "\n".join(rendered_candidates)
    )


def _empty_prompt(ctx) -> str:
    return (
        f"No option candidates this cycle for "
        f"{getattr(ctx, 'segment', 'this profile')}. Return an "
        f"empty trades list."
    )


# ---------------------------------------------------------------------------
# Option feature renderer — surfaces option-specific keys FIRST in
# the rendered dict (so the AI sees IV rank / Greeks / DTE before
# the underlying's technicals when scanning the prompt).
# ---------------------------------------------------------------------------

# Ordered for readability — the AI sees these top-to-bottom.
_OPTION_FEATURE_ORDER = (
    "iv_rank", "iv", "implied_vol",
    "delta", "gamma", "theta", "vega",
    "dte", "days_to_expiry",
    "strike", "spread_width",
    "spread_max_loss", "spread_max_gain",
    "option_strategy", "premium",
    "bid_ask_spread", "option_bid", "option_ask",
)


def _option_features_first(extra: dict) -> dict:
    """Surface option-specific keys first in the rendered dict.

    Returns a new dict with option features ordered first (in the
    order given by `_OPTION_FEATURE_ORDER`), then any remaining
    keys from the candidate's extras (typically the underlying's
    technicals like rsi, sector_momentum). Insertion order is
    preserved by Python 3.7+ dicts so the JSON renders in this
    deterministic order.
    """
    out = {}
    seen = set()
    for k in _OPTION_FEATURE_ORDER:
        if k in extra:
            out[k] = extra[k]
            seen.add(k)
    for k, v in extra.items():
        if k not in seen:
            out[k] = v
    return out
