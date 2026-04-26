"""Adaptive AI prompt structure — Layer 6 of autonomous tuning.

The structure of the AI's prompt — what sections appear, at what
verbosity — becomes a tunable surface. The tuner rotates section
verbosity across cycles, tracks which variants correlate with higher
win rates, and reinforces the variants that work.

The default behavior (no overrides) is what the prompt builder did
before this layer: every section at its built-in verbosity. As the
tuner gathers data on which variants work for a profile, it sets
overrides that the prompt builder consults.

**Why discrete verbosity levels:** "normal" is the built-in behavior
(no change). "brief" omits non-essential lines from a section to test
whether the AI was getting noise. "detailed" adds extra context to
test whether more detail improves decisions. The 3-step ladder makes
A/B testing tractable — easy to attribute outcomes to variant choice.

**Cost gate:** "detailed" verbosity → more tokens → higher API spend.
Every move toward "detailed" is checked against the cost guard. If
the projected spend would breach the daily ceiling, the rotation is
queued as a recommendation, not auto-applied.
"""

from __future__ import annotations

import json
import logging
import random
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


VERBOSITIES: Tuple[str, ...] = ("brief", "normal", "detailed")


# Sections of the AI prompt that can have their verbosity tuned. Each
# entry maps a `name` (used in storage keys) to a human label. Adding
# a new section: register it here, then teach the prompt builder to
# consult `get_verbosity(profile, name)`.
TUNABLE_SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("alt_data",          "Alternative Data Section"),
    ("political_context", "Political Context Section"),
    ("learned_patterns",  "Learned Patterns Section"),
    ("portfolio_state",   "Portfolio State Section"),
)


def section_names() -> List[str]:
    return [name for name, _ in TUNABLE_SECTIONS]


def display_label(section_name: str) -> str:
    for name, label in TUNABLE_SECTIONS:
        if name == section_name:
            return label
    return " ".join(w.capitalize() for w in section_name.split("_"))


def parse_layout(raw_json: Optional[str]) -> Dict[str, str]:
    """Parse the raw `prompt_layout` column. Returns {} on missing or
    malformed data; filters to known sections and verbosities."""
    if not raw_json:
        return {}
    try:
        d = json.loads(raw_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(d, dict):
        return {}
    valid_sections = set(section_names())
    out = {}
    for name, verbosity in d.items():
        if name not in valid_sections:
            continue
        if verbosity not in VERBOSITIES:
            continue
        if verbosity == "normal":
            # Don't store the default — keep the dict sparse
            continue
        out[name] = verbosity
    return out


def get_verbosity(profile_or_dict: Any, section_name: str) -> str:
    """Return the current verbosity for `section_name` on this profile.
    Defaults to 'normal' if no override is set."""
    if isinstance(profile_or_dict, dict):
        raw = profile_or_dict.get("prompt_layout")
    else:
        raw = getattr(profile_or_dict, "prompt_layout", None)
    layout = parse_layout(raw) if isinstance(raw, str) else (raw or {})
    if not isinstance(layout, dict):
        layout = {}
    v = layout.get(section_name, "normal")
    return v if v in VERBOSITIES else "normal"


def set_verbosity(profile_id: int, section_name: str,
                   verbosity: str) -> None:
    """Persist a verbosity override for one section."""
    if section_name not in section_names():
        return
    if verbosity not in VERBOSITIES:
        return
    from models import _get_conn, get_trading_profile
    profile = get_trading_profile(profile_id)
    if not profile:
        return
    raw = profile.get("prompt_layout") or "{}"
    layout = parse_layout(raw)
    if verbosity == "normal":
        layout.pop(section_name, None)
    else:
        layout[section_name] = verbosity
    conn = _get_conn()
    conn.execute(
        "UPDATE trading_profiles SET prompt_layout = ? WHERE id = ?",
        (json.dumps(layout), profile_id),
    )
    conn.commit()
    conn.close()


def all_verbosities(profile_or_dict: Any) -> Dict[str, str]:
    """Return the full {section: verbosity} for a profile. Includes
    every section name with its current value (normal by default)."""
    out = {}
    for name in section_names():
        out[name] = get_verbosity(profile_or_dict, name)
    return out


# ─────────────────────────────────────────────────────────────────────
# Cost estimation for verbosity moves
# ─────────────────────────────────────────────────────────────────────

# Rough token-cost delta per cycle for a verbosity change. These are
# additive so the cost guard can multiply by the daily cycle count.
# Values intentionally conservative; cost guard will block borderline
# moves.
_BRIEF_VS_NORMAL_TOKENS = -150       # Saves tokens
_DETAILED_VS_NORMAL_TOKENS = +400    # Costs tokens
_TYPICAL_PRICE_PER_1K_TOKENS = 0.001  # ~Haiku-class pricing
_CYCLES_PER_DAY = 26                  # ~4/hour × 6.5 market hours


def estimate_daily_cost_delta(from_verbosity: str, to_verbosity: str) -> float:
    """Estimate the per-day USD cost change from rotating a section's
    verbosity. Negative means SAVES money (e.g., normal → brief)."""
    def _tokens_for(v: str) -> int:
        return {"brief": _BRIEF_VS_NORMAL_TOKENS,
                "normal": 0,
                "detailed": _DETAILED_VS_NORMAL_TOKENS}.get(v, 0)
    delta_tokens = _tokens_for(to_verbosity) - _tokens_for(from_verbosity)
    return (delta_tokens / 1000.0) * _TYPICAL_PRICE_PER_1K_TOKENS * _CYCLES_PER_DAY


# ─────────────────────────────────────────────────────────────────────
# Variant rotation (deterministic seed for testability)
# ─────────────────────────────────────────────────────────────────────

def pick_rotation(profile_or_dict: Any,
                   rng: Optional[random.Random] = None) -> Tuple[str, str, str]:
    """Pick one section to rotate and return (section_name, current,
    new_verbosity). The rotation is biased toward exploring less-tried
    states for this profile (epsilon-greedy)."""
    rng = rng or random.Random()
    current = all_verbosities(profile_or_dict)
    # Pick a random section
    section_name = rng.choice(section_names())
    cur_v = current[section_name]
    # Cycle through other verbosities — not the current one
    other = [v for v in VERBOSITIES if v != cur_v]
    new_v = rng.choice(other)
    return section_name, cur_v, new_v
