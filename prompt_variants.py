"""Prompt-variant shadow arms — hold the MODEL constant, vary the PROMPT.

Shadow eval (shadow_eval.py) fires candidate models at the same prompt
the primary just answered, which measures MODEL delta. It cannot answer
the operator's question of 2026-07-30: "can better instructions get the
cheap primary to reason like the expensive shadow?" Answering that needs
the mirror experiment — same model as the primary, different
instructions, same candidate, same book, same second.

A shadow_models entry may therefore carry a variant tag:

    "google:gemini-3.1-flash-lite@adversarial_v2"

`shadow_eval` splits the tag off, looks it up here, and sends the
REWRITTEN prompt to the shadow call. Everything downstream is unchanged:
the row lands in ai_shadow_calls with the primary's response beside it
and `agreement` computed the usual way, so /shadow reads a prompt A/B
with exactly the machinery it already uses for a model A/B.

Two design rules, both load-bearing:

1. SCOPED BY PURPOSE. A variant declares which purposes it rewrites and
   is SKIPPED (no call, no cost, no row) everywhere else. Without this
   a variant would triple the call volume of all ~18k monthly shadow
   calls to test a prompt used by one specialist.

2. FAIL CLOSED, LOUDLY. A transform that cannot find what it means to
   replace returns None and logs at ERROR — the call is skipped. It
   must NEVER fall through to sending the unmodified prompt: that would
   run the primary model against itself with identical instructions and
   report "no measurable difference", the most expensive possible way
   to learn nothing.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# adversarial_v2 — evidence-gated reviewer
# ---------------------------------------------------------------------------
#
# Derived from the 2026-07-30 measurement of 705 live reviewer calls
# (gemini-3.1-flash-lite primary vs claude-haiku-4-5 shadow). Three
# findings, none of which depend on outcome data:
#
#   * The primary NEVER cites a concrete figure — 0% of its 204 HOLD
#     rationales contain a dollar amount or percentage, against 13% for
#     the shadow. It writes "provides energy sector diversification"
#     where the shadow writes "Book already carries WMB (midstream
#     energy) LONG $12.4k".
#   * The primary argues FOR the trade in 41% of HOLD rationales
#     ("provides useful diversification", "the book lacks energy
#     exposure") versus 21% for the shadow. That is a role inversion:
#     the reviewer is not the trade's advocate, and another specialist
#     already made the case for entry.
#   * The base prompt's soft anti-over-veto rule ("more than 2 VETOs
#     out of 5 and you are over-vetoing") is ignored by BOTH models —
#     the primary vetoes 63% of candidates, the shadow 85%.
#
# The rewrite replaces exhortation with structure: a VETO must carry a
# checklist trigger and a value COPIED from the context. That is a much
# harder bar to clear on a vague hunch than a word-count rule, and it
# does not force a wrong HOLD when four of five candidates genuinely
# fail — which an arbitrary quota would.
#
# `confidence` is deliberately RETAINED even though the same measurement
# showed it carries no information (the primary puts 95 or 85 on 80% of
# verdicts). ensemble.py requires the field and feeds it to weighted
# voting and Platt calibration, so dropping it would make the variant
# un-promotable without a second change to the ensemble.

_REVIEWER_ANCHOR = "Return a STRICT JSON ARRAY"
_REVIEWER_COUNT_RE = re.compile(r"exactly\s+(\d+)\s+entries")

_REVIEWER_V2_TAIL = """Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. Each entry:
  {
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "trigger": <the checklist number 1-7 that fired, or null>,
    "evidence": "<a value COPIED from the book / mandate / candidate
                 data above, or null>",
    "reasoning": "one sentence naming the failure mode"
  }

EVIDENCE GATE — this is the binding rule:
  A VETO is valid ONLY if you can fill BOTH `trigger` and `evidence`.
  `evidence` must be a value copied from the context above, not a
  paraphrase and not a generality. Valid evidence looks like:
      "WMB LONG $12.4k"                  (a book position)
      "8-K on 2026-08-04, inside 5-15d"  (a dated catalyst)
      "target_book_beta=1.0"             (a mandate number)
      "STX long $24k / short -$35.4k"    (a concentration)
  If you cannot name the trigger and copy a value, you have not found
  a specific failure mode — return HOLD. Do not veto on a feeling.

YOU ARE NOT THIS TRADE'S ADVOCATE:
  Never write a rationale that argues FOR the trade. "Provides
  diversification", "the book lacks this sector", "adds useful
  exposure", "no identifiable risk" are ADVOCACY or filler. Another
  specialist already argued for entry; you are not reviewing their
  case, you are hunting for what kills it.
  HOLD means "I hunted and found no evidenced failure mode." It does
  NOT mean "this looks like a good trade." If your HOLD reasoning
  names a BENEFIT of the trade, rewrite it to state what you checked
  and what you did not find.

VERDICT DISCIPLINE:
  HOLD  = DEFAULT. No evidenced failure mode. Uncertainty is HOLD.
          "Probably fine" is HOLD.
  VETO  = a named failure mode that CLEARS THE EVIDENCE GATE above.
  BUY   = the failure-mode hunt actively supports this entry — you
          looked for the kill and the setup survives it.
  SELL  = the hunt supports closing an EXISTING position (not the
          candidate).

You MUST return exactly __N__ entries, one per candidate, in the same
order.
"""


def _adversarial_v2(prompt: str) -> Optional[str]:
    """Replace the reviewer prompt's output-contract tail with the
    evidence-gated version. Returns None (caller skips the call) when
    the anchor or the candidate count can't be found — the base prompt
    changed and this variant must be re-derived rather than silently
    degrade into a duplicate of the primary."""
    idx = prompt.find(_REVIEWER_ANCHOR)
    if idx == -1:
        logger.error(
            "prompt variant adversarial_v2: anchor %r not found in the "
            "reviewer prompt — specialists/adversarial_reviewer.py has "
            "changed. Variant SKIPPED (never sent unmodified); re-derive "
            "the transform before trusting further A/B rows.",
            _REVIEWER_ANCHOR,
        )
        return None
    m = _REVIEWER_COUNT_RE.search(prompt, idx)
    if not m:
        logger.error(
            "prompt variant adversarial_v2: candidate-count phrase "
            "('exactly N entries') not found after the anchor — the "
            "reviewer output contract changed. Variant SKIPPED.",
        )
        return None
    return prompt[:idx] + _REVIEWER_V2_TAIL.replace("__N__", m.group(1))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# tag -> {purpose: transform}. A purpose absent from a variant's map is
# not shadowed by that variant at all.
VARIANTS: Dict[str, Dict[str, Callable[[str], Optional[str]]]] = {
    "adversarial_v2": {
        "ensemble:adversarial_reviewer": _adversarial_v2,
    },
}

# Operator-facing copy for the settings page. A variant with no entry
# here still works; it just renders with its bare tag.
VARIANT_META: Dict[str, Dict[str, str]] = {
    "adversarial_v2": {
        "label": "Evidence-gated reviewer",
        "description": (
            "Red-team reviewer must name the checklist item that fired "
            "and copy a concrete value (a book position, a dated "
            "catalyst, a mandate number) to justify a VETO, and may "
            "not argue FOR the trade. Tests whether instructions alone "
            "close the gap to a more expensive model."
        ),
    },
}


def describe_variants() -> List[Dict[str, Any]]:
    """Catalogue of registered variants for the settings UI.

    The settings page needs this because prompt-variant arms are NOT in
    the provider/model catalogue that renders the ordinary shadow
    checkboxes. Without a checkbox of their own they would be invisible
    AND — because the save handler rebuilds `shadow_models` from the
    posted checkboxes — silently deleted the first time the operator
    saved the settings form.
    """
    out: List[Dict[str, Any]] = []
    for tag, purpose_map in sorted(VARIANTS.items()):
        meta = VARIANT_META.get(tag, {})
        out.append({
            "tag": tag,
            "label": meta.get("label", tag),
            "description": meta.get("description", ""),
            "purposes": sorted(purpose_map.keys()),
        })
    return out


def split_variant_tag(entry_model: str) -> tuple:
    """Split a shadow model string into (model, variant_tag_or_None).

    `"gemini-3.1-flash-lite@adversarial_v2"` -> `("gemini-3.1-flash-lite",
    "adversarial_v2")`; a plain model name yields a None tag.
    """
    model, sep, tag = entry_model.partition("@")
    if not sep or not tag.strip():
        return entry_model, None
    return model, tag.strip()


def apply_variant(tag: str, purpose: Optional[str],
                  prompt: str) -> Optional[str]:
    """Rewrite `prompt` for variant `tag` at this `purpose`.

    Returns the rewritten prompt, or None when the call should be
    SKIPPED — either the variant doesn't cover this purpose (normal,
    quiet) or its transform failed (logged at ERROR by the transform).
    Never returns the input unchanged: a variant that produced no
    edit is a failed variant, not a free duplicate of the primary.
    """
    purpose_map = VARIANTS.get(tag)
    if not purpose_map:
        logger.warning(
            "prompt variant %r is not registered — shadow call skipped. "
            "Check the profile's shadow_models JSON against "
            "prompt_variants.VARIANTS.", tag,
        )
        return None
    fn = purpose_map.get(purpose or "")
    if fn is None:
        # Normal: this variant only rewrites certain purposes.
        return None
    out = fn(prompt)
    if out is None:
        return None
    if out == prompt:
        logger.error(
            "prompt variant %r on purpose %r produced an IDENTICAL "
            "prompt — skipping. An unmodified variant would compare the "
            "primary model against itself and report a false null.",
            tag, purpose,
        )
        return None
    return out
