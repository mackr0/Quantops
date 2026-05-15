"""Specialist AI ensemble — Phase 8 of the Quant Fund Evolution roadmap.

A single generalist AI making decisions has systematic blind spots. Real
quant funds run teams of specialists (earnings analysts, technicians,
macroeconomists, risk managers) and combine their views. We do the same
with focused AI prompts — each specialist is a small module with its own
system prompt tuned for one lens on the market. The meta-coordinator
(`ensemble.run_ensemble`) collects their verdicts and synthesizes a final
decision.

Each specialist module exposes:
    NAME: str                     — stable identifier
    DESCRIPTION: str              — one-line role description
    build_prompt(candidates, ctx) — returns the user-facing prompt string
    parse_response(raw)           — parses the AI response into per-symbol
                                    verdicts: [{"symbol", "verdict",
                                                "confidence", "reasoning"}, ...]

The specialist module does NOT call the AI itself. `ensemble.run_ensemble`
handles the provider call; specialists only own prompt engineering and
response parsing. This keeps cost and retry logic in one place.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, List

logger = logging.getLogger(__name__)


SPECIALIST_MODULES = [
    "specialists.earnings_analyst",
    "specialists.pattern_recognizer",
    "specialists.sentiment_narrative",
    "specialists.risk_assessor",
    "specialists.adversarial_reviewer",
    # Option-only — Phase 4 of pipeline refactor. Auto-skipped on stock
    # pipelines via APPLIES_TO_PIPELINES = ("option",) tag.
    "specialists.option_spread_risk",
    # 2026-05-12 — additional option-specialists for ensemble depth.
    # iv_skew judges put_iv vs call_iv premium edge; gamma_pin
    # judges pinning regime via GEX + max_pain. Neither holds veto
    # (option_spread_risk remains the structural gate).
    "specialists.iv_skew_specialist",
    "specialists.gamma_pin_specialist",
]


def discover_specialists() -> List[Any]:
    """Import every specialist module and return the live ones."""
    out: List[Any] = []
    for mod_path in SPECIALIST_MODULES:
        try:
            mod = importlib.import_module(mod_path)
        except (ImportError, AttributeError, SyntaxError) as _imp_exc:
            # Per-module specialist import loop; one bad specialist
            # shouldn't kill registry load. Surface for follow-up so
            # a broken specialist file doesn't quietly disappear.
            logger.warning(
                "specialist module failed to import: %s: %s",
                type(_imp_exc).__name__, _imp_exc,
            )
            continue
        if callable(getattr(mod, "build_prompt", None)) and callable(
            getattr(mod, "parse_response", None)
        ):
            out.append(mod)
    return out
