"""Guardrail: every meta-model feature must have a user-visible
UI / prompt / API surface.

The recurring failure mode this catches: a new feature gets added to
`meta_model.NUMERIC_FEATURES` (so it influences trade decisions) but
doesn't get rendered anywhere a human can see it. The user has no
way to know the feature exists, what its current value is, or
whether it's contributing signal vs noise.

The contract this enforces: for each feature key in NUMERIC_FEATURES,
at least ONE of the following references the key by string:
  - a Jinja template under templates/
  - a Python view / API handler in views.py / app.py
  - the AI prompt assembler (ai_analyst.py)
  - the candidate-data builder (trade_pipeline.py) — at minimum the
    prompt path

Or it must be on the explicit `INTERNAL_FEATURES` allowlist (purely
internal — no user value in surfacing).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Set

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# Files we scan for feature-name references. Order doesn't matter.
SURFACES = [
    "templates/ai.html",
    "templates/dashboard.html",
    "templates/performance.html",
    "templates/settings.html",
    "templates/trades.html",
    "templates/ai_awareness.html",
    "views.py",
    "ai_analyst.py",
    "trade_pipeline.py",
]


# Features that legitimately don't surface (internal scaffolding,
# regime-conditioning fields the AI doesn't need to see directly,
# etc). Each entry MUST have a written rationale.
INTERNAL_FEATURES = {
    # Total count of bullish strategies firing — not a per-symbol
    # signal the user reasons about. Used by the meta-model as a
    # feature; surfaced as ensemble vote counts elsewhere on the AI
    # page.
    "_market_signal_count": "Aggregate scaffolding count, not user-facing",
    # Macro features — surfaced as macro_context blocks under
    # MARKET CONTEXT in the prompt, not by these internal keys.
    "_yield_spread_10y2y":   "Surfaced via macro_context.yield_curve",
    "_cboe_skew":            "Surfaced via macro_context.cboe_skew",
    "_unemployment_rate":    "Surfaced via macro_context.fred_macro",
    "_cpi_yoy":              "Surfaced via macro_context.fred_macro",
}


def _load_surface_text() -> str:
    """Concatenate every surface file's text into one big string for
    grep-style substring matches. Cheap; runs in ~10ms."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    chunks = []
    for rel in SURFACES:
        path = os.path.join(repo_root, rel)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                chunks.append(f.read())
        except Exception:
            continue
    return "\n".join(chunks)


def _feature_appears(feature: str, blob: str) -> bool:
    """True if the feature key appears in the surface blob, either
    quoted or as a bare identifier with word boundaries. Catches
    `entry["foo"]`, `f.get("foo")`, `'foo'`, `name="foo"`, etc."""
    pattern = r'["\']' + re.escape(feature) + r'["\']'
    if re.search(pattern, blob):
        return True
    # Also accept bare-word matches when the feature is "wide enough"
    # to be unambiguous (≥ 8 chars) — protects against false negatives
    # when the feature shows up in a label string the templates write
    # without quotes.
    if len(feature) >= 8:
        if re.search(r'\b' + re.escape(feature) + r'\b', blob):
            return True
    return False


class TestEveryMetaFeatureHasUiSurface:
    def test_every_numeric_feature_has_ui_surface(self):
        from meta_model import NUMERIC_FEATURES
        blob = _load_surface_text()

        missing = []
        for feature in NUMERIC_FEATURES:
            if feature in INTERNAL_FEATURES:
                continue
            if not _feature_appears(feature, blob):
                missing.append(feature)

        if missing:
            pytest.fail(
                "The following meta-model NUMERIC_FEATURES are not "
                "referenced by ANY user-visible surface (template, "
                "API view, AI prompt). The feature contributes to "
                "trade decisions but the user can't see what it is, "
                "what its value is, or whether it's working.\n\n"
                "Fix one of:\n"
                "  1. Render it in the AI prompt (ai_analyst.py).\n"
                "  2. Surface it in the candidate dict that the AI "
                "sees (trade_pipeline.py _build_candidates_data).\n"
                "  3. Add a dashboard panel + API endpoint.\n"
                "  4. If the feature is purely internal scaffolding, "
                "add it to INTERNAL_FEATURES with a rationale.\n\n"
                f"Missing: {sorted(missing)}"
            )

    def test_no_stale_internal_entries(self):
        """Stale INTERNAL_FEATURES entries hide drift — fail when an
        allowlisted feature is no longer in NUMERIC_FEATURES."""
        from meta_model import NUMERIC_FEATURES
        stale = [k for k in INTERNAL_FEATURES if k not in NUMERIC_FEATURES]
        assert not stale, (
            f"INTERNAL_FEATURES entries no longer in NUMERIC_FEATURES: "
            f"{sorted(stale)}. Remove them from INTERNAL_FEATURES."
        )
