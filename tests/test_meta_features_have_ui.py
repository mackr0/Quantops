"""Guardrail: every meta-model feature AND tunable signal must have
a user-visible UI / prompt / API surface.

The recurring failure mode this catches: shipping a new feature or
lever that influences trade decisions but doesn't render anywhere a
human can see it. The user has no way to know it exists, see its
current value, or judge whether it's contributing signal vs noise.

The contract this enforces — for each key in:
  - meta_model.NUMERIC_FEATURES
  - meta_model.CATEGORICAL_FEATURES
  - signal_weights.WEIGHTABLE_SIGNALS

at least ONE of the following references the key by string:
  - a Jinja template under templates/
  - a Python view / API handler in views.py / app.py
  - the AI prompt assembler (ai_analyst.py)
  - the candidate-data builder (trade_pipeline.py)
  - the self-tuning module (self_tuning.py)

Or it's on the explicit allowlist for that feature class with a
written rationale.
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
# NOTE: signal_weights.py / alternative_data.py / self_tuning.py
# are NOT in SURFACES even though they reference every feature
# they define. Including them would make the test tautological —
# a feature defined in alternative_data.py would always "pass"
# regardless of whether the user can see it in the UI.


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

# Categorical features that are surfaced by their VALUE, not their
# key — e.g. the prompt shows "Regime: bull" not the literal "_regime"
# key. Allowlist with the surface where the value appears.
INTERNAL_CATEGORICAL = {
    "_regime":             "Surfaced as 'Regime: <value>' in MARKET CONTEXT",
    "_curve_status":       "Surfaced via macro_context.yield_curve.curve_status",
    "_rotation_phase":     "Surfaced via sector_rotation block",
    "_market_gex_regime":  "Surfaced via macro_context options regime",
}

# Weightable signals surfaced dynamically (not as literal strings in
# the templates / views). The /api/weightable-signals endpoint
# iterates WEIGHTABLE_SIGNALS at request time, so the user sees the
# full list on the AI Operations tab even though the names don't
# appear as static substrings in the source. Rationale per entry.
INTERNAL_WEIGHTABLE = {
    "vote_momentum_breakout":
        "Listed dynamically on /api/weightable-signals panel",
    "vote_volume_spike":
        "Listed dynamically on /api/weightable-signals panel",
    "vote_mean_reversion":
        "Listed dynamically on /api/weightable-signals panel",
    "vote_gap_and_go":
        "Listed dynamically on /api/weightable-signals panel",
    "vote_short_squeeze_setup":
        "Listed dynamically on /api/weightable-signals panel",
    "vote_news_sentiment_spike":
        "Listed dynamically on /api/weightable-signals panel",
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

    def test_every_categorical_feature_has_ui_surface(self):
        """Same contract as numeric, but for CATEGORICAL_FEATURES."""
        from meta_model import CATEGORICAL_FEATURES
        blob = _load_surface_text()

        missing = []
        for feature in CATEGORICAL_FEATURES.keys():
            if feature in INTERNAL_CATEGORICAL:
                continue
            if not _feature_appears(feature, blob):
                missing.append(feature)

        assert not missing, (
            f"CATEGORICAL_FEATURES not referenced by any UI surface: "
            f"{sorted(missing)}. Add a UI / prompt render or add to "
            f"INTERNAL_CATEGORICAL with a rationale."
        )

    def test_every_weightable_signal_has_ui_surface(self):
        """Every WEIGHTABLE_SIGNALS entry should be visible. This is
        what fails when a new tunable-knob lever ships without a
        place for the user to see / configure it. The `vote_*`
        prefix variants count as covered when the underlying
        strategy name appears.
        """
        from signal_weights import WEIGHTABLE_SIGNALS
        blob = _load_surface_text()

        missing = []
        for entry in WEIGHTABLE_SIGNALS:
            name = entry[0] if isinstance(entry, tuple) else str(entry)
            if name in INTERNAL_WEIGHTABLE:
                continue
            # Direct match
            if _feature_appears(name, blob):
                continue
            # `vote_<strategy>` aliases — accept the base strategy name
            if name.startswith("vote_"):
                base = name[len("vote_"):]
                if _feature_appears(base, blob):
                    continue
            missing.append(name)

        assert not missing, (
            f"WEIGHTABLE_SIGNALS not referenced by any UI surface: "
            f"{sorted(missing)}. Either add a UI render, or — for "
            f"`vote_*` strategy weights — make sure the underlying "
            f"strategy name appears somewhere visible."
        )

    def test_no_stale_internal_entries(self):
        """Stale entries in INTERNAL_* allowlists hide drift."""
        from meta_model import NUMERIC_FEATURES, CATEGORICAL_FEATURES
        from signal_weights import WEIGHTABLE_SIGNALS
        weightable_names = {
            (e[0] if isinstance(e, tuple) else str(e))
            for e in WEIGHTABLE_SIGNALS
        }

        stale = [k for k in INTERNAL_FEATURES if k not in NUMERIC_FEATURES]
        assert not stale, (
            f"INTERNAL_FEATURES entries no longer in NUMERIC_FEATURES: "
            f"{sorted(stale)}."
        )
        stale_cat = [k for k in INTERNAL_CATEGORICAL
                     if k not in CATEGORICAL_FEATURES]
        assert not stale_cat, (
            f"INTERNAL_CATEGORICAL entries no longer in "
            f"CATEGORICAL_FEATURES: {sorted(stale_cat)}."
        )
        stale_w = [k for k in INTERNAL_WEIGHTABLE
                   if k not in weightable_names]
        assert not stale_w, (
            f"INTERNAL_WEIGHTABLE entries no longer in "
            f"WEIGHTABLE_SIGNALS: {sorted(stale_w)}."
        )
