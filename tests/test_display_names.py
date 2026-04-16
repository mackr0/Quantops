"""Tests for display_names — the Jinja filter that converts internal
snake_case identifiers into human-readable labels for the UI."""

from __future__ import annotations

import pytest


class TestDisplayName:
    def test_known_strategy(self):
        from display_names import display_name
        assert display_name("max_pain_pinning") == "Max Pain Pinning"
        assert display_name("market_engine") == "Market Structure Engine"
        assert display_name("insider_cluster") == "Insider Buying Cluster"

    def test_known_specialist(self):
        from display_names import display_name
        assert display_name("risk_assessor") == "Risk Assessor"
        assert display_name("sentiment_narrative") == "Sentiment & Narrative"

    def test_known_event_type(self):
        from display_names import display_name
        assert display_name("sec_filing_detected") == "SEC Filing Detected"
        assert display_name("price_shock") == "Price Shock"

    def test_known_crisis_signal(self):
        from display_names import display_name
        assert display_name("vix_inversion") == "VIX Term Inversion"
        assert display_name("bond_stock_divergence") == "Bond/Stock Divergence"

    def test_crisis_level_titlecased(self):
        from display_names import display_name
        assert display_name("severe") == "Severe"
        assert display_name("crisis") == "Crisis"

    def test_unknown_falls_back_to_title_case(self):
        """An auto-generated strategy like `auto_oversold_vol_confirm`
        should pretty-print without a code change."""
        from display_names import display_name
        assert display_name("auto_oversold_vol_confirm") == "Auto Oversold Vol Confirm"
        assert display_name("something_new") == "Something New"

    def test_empty_and_none_are_safe(self):
        from display_names import display_name
        assert display_name("") == ""
        assert display_name(None) == ""

    def test_jinja_filter_registers(self):
        """The filter must be registered on the app via register()."""
        from flask import Flask
        from display_names import register
        app = Flask(__name__)
        register(app)
        assert "display_name" in app.jinja_env.filters
        rendered = app.jinja_env.from_string(
            "{{ 'max_pain_pinning' | display_name }}"
        ).render()
        assert rendered == "Max Pain Pinning"


class TestNoSnakeCaseLeaksAnywhere:
    """Catch any source of snake_case that could leak into the UI.

    The previous test was scoped only to STRATEGY_MODULES; that's why
    `purpose` tags like `political_context` and `ensemble:risk_assessor`
    leaked to the AI Cost panel without anyone noticing. This sweep
    grep-discovers every `purpose=` value in the codebase and asserts
    each one renders human-readably (no underscores in the label, first
    letter uppercased)."""

    def _grep_purpose_values(self):
        """Find every purpose= literal in the project."""
        import os, re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rx = re.compile(r'purpose\s*=\s*"([^"]+)"|purpose\s*=\s*f"([^"{]+)"')
        rx_fstring = re.compile(r'purpose\s*=\s*f"([^"]+)"')
        seen = set()
        for fname in os.listdir(root):
            if not fname.endswith(".py"):
                continue
            try:
                with open(os.path.join(root, fname)) as f:
                    src = f.read()
            except OSError:
                continue
            for m in rx.finditer(src):
                v = m.group(1) or m.group(2)
                if v and "{" not in v:
                    seen.add(v)
            # Special-case: ensemble's f"ensemble:{name}" — generate the
            # 4 known specialist permutations explicitly
            if "purpose=f\"ensemble:{name}\"" in src:
                for spec in ("earnings_analyst", "pattern_recognizer",
                             "sentiment_narrative", "risk_assessor"):
                    seen.add(f"ensemble:{spec}")
        return seen

    def test_every_purpose_tag_has_human_label(self):
        from display_names import display_name
        purposes = self._grep_purpose_values()
        # Sanity: we should find at least the known set
        assert len(purposes) >= 7, f"too few purpose tags discovered: {purposes}"

        offenders = []
        for p in purposes:
            label = display_name(p)
            # No raw snake_case — every word must be properly separated
            if "_" in label:
                offenders.append(f"{p!r} → {label!r} (contains underscore)")
            # First alphabetic char must be uppercase
            first_alpha = next((c for c in label if c.isalpha()), None)
            if first_alpha and not first_alpha.isupper():
                offenders.append(f"{p!r} → {label!r} (lowercase start)")

        assert not offenders, (
            "These purpose tags will leak as raw strings to the AI Cost "
            "panel — add them to display_names.py:\n  " +
            "\n  ".join(offenders)
        )

    def test_known_purpose_labels(self):
        """Exact assertions for the labels users will actually see."""
        from display_names import display_name
        assert display_name("political_context") == "Political / Macro Context"
        assert display_name("batch_select") == "Trade Selection (Batch)"
        assert display_name("ensemble:risk_assessor") == "Ensemble — Risk Assessor"
        assert display_name("ensemble:earnings_analyst") == "Ensemble — Earnings Analyst"
        assert display_name("sec_diff") == "SEC Filing Diff"
        assert display_name("strategy_proposal") == "Strategy Proposal (Auto-Gen)"

    def test_namespaced_fallback_for_unknown_specialist(self):
        """A new ensemble specialist added later must auto-pretty-print
        even before someone updates display_names.py — the colon-namespace
        fallback handles this."""
        from display_names import display_name
        out = display_name("ensemble:macro_econometrician")
        # Must not contain raw snake_case
        assert "_" not in out
        # Must have the namespace prefix
        assert "Ensemble" in out
        assert "Macro" in out and "Econometrician" in out
