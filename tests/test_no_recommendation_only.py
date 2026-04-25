"""Guardrail: every "Recommendation:" string in the self-tuner must be on
the explicit allowlist, with a written rationale. New "Recommendation:"
strings fail this test until the author either (a) wires a real action
that auto-applies the change, or (b) adds an entry to ALLOWED with a
rationale explaining why human review is required.

Background — why this test exists:

The self-tuner exists to make smart choices and learn from them. When
the tuner identifies a problem but only logs a "Recommendation:" string
without acting, the system isn't doing its job — the user sees text and
has to act manually. Most "Recommendation: ..." paths can and should be
auto-actioned via either the profile-toggle pipeline (for parameters
with a profile column) or via alpha_decay.deprecate_strategy (for
modular strategies). The few cases that legitimately need human review
(auto-flipping a high-risk feature ON without supervision) belong on
the allowlist below.
"""

import ast
from pathlib import Path

import pytest


# Allowlist — each entry is a substring of an intentional, reviewed
# recommendation-only string. New entries require:
#   - A clear rationale comment explaining why this CANNOT be auto-actioned
#   - The substring must be specific enough that other recommendation-only
#     fall-throughs can't accidentally match
ALLOWED = {
    # Auto-ENABLING shorts is high-risk: it flips on a feature with
    # uncapped downside, requires margin, and changes the entire risk
    # profile. The defensive opposite (auto-DISABLE shorts on losses) IS
    # auto-actioned. Asymmetric on purpose: easy to turn dangerous things
    # off, hard to turn them on without a human in the loop.
    "Recommendation: enable short selling": "auto-enabling shorts is high-risk",
}


def _self_tuning_source() -> str:
    return (Path(__file__).resolve().parent.parent / "self_tuning.py").read_text()


def _walk_string_constants(tree: ast.AST):
    """Yield every string literal in the tree (including JoinedStr / f-string
    parts), with the source line number."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value, getattr(node, "lineno", -1)
        elif isinstance(node, ast.JoinedStr):
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    yield piece.value, getattr(piece, "lineno", -1)


class TestNoNewRecommendationOnlyPaths:
    def test_every_recommendation_string_is_allowlisted(self):
        src = _self_tuning_source()
        tree = ast.parse(src)

        unallowed = []
        for value, lineno in _walk_string_constants(tree):
            if "Recommendation:" not in value:
                continue
            if not any(allowed in value for allowed in ALLOWED):
                unallowed.append((lineno, value.strip()))

        if unallowed:
            details = "\n".join(
                f"  self_tuning.py:{ln}: {text!r}" for ln, text in unallowed
            )
            pytest.fail(
                "New 'Recommendation:'-only path(s) found in self_tuning.py.\n\n"
                "The self-tuner is supposed to ACT on what it identifies, not\n"
                "just emit text the user has to read and respond to. Either:\n\n"
                "  1. Wire a real auto-action — e.g., update_trading_profile\n"
                "     for tunable parameters, or alpha_decay.deprecate_strategy\n"
                "     for modular strategies. This is almost always the right\n"
                "     answer.\n\n"
                "  2. If the action is genuinely too risky to auto-apply (e.g.,\n"
                "     auto-enabling a feature with uncapped downside), add an\n"
                "     entry to ALLOWED in tests/test_no_recommendation_only.py\n"
                "     with a written rationale.\n\n"
                f"Offending strings:\n{details}"
            )

    def test_allowlist_entries_actually_appear_in_source(self):
        """Stale allowlist entries hide newly-introduced rec-only paths.
        Fail if any allowlist key no longer appears in self_tuning.py so
        the list stays honest."""
        src = _self_tuning_source()
        stale = [k for k in ALLOWED if k not in src]
        if stale:
            pytest.fail(
                "Allowlist contains entries no longer present in self_tuning.py:\n"
                + "\n".join(f"  - {k!r}" for k in stale)
                + "\n\nRemove them from ALLOWED to keep the guardrail tight."
            )
