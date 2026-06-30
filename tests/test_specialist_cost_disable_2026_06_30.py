"""Globally-disabled advisory specialists for cost control (2026-06-30).

sentiment_narrative + pattern_recognizer were ~22% of daily AI cost. They are
ADVISORY (non-veto) and redundant with the free 179-rule deterministic panel,
so disabling them cuts cost with no learning impact (specialist verdicts are
not a learning feature) and no protection loss. These pin the SAFETY of the
cut: we must never globally disable a VETO-authorized specialist, and the
ensemble must actually honor the config list (merged before the ≥2 floor).
"""
from __future__ import annotations

import os

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def test_disabled_list_is_the_two_advisory_specialists():
    import config
    assert config.GLOBALLY_DISABLED_SPECIALISTS == [
        "sentiment_narrative", "pattern_recognizer"]


def test_never_globally_disable_a_veto_specialist():
    """The protection layer (risk_assessor / adversarial_reviewer) must never
    be in the global disable list."""
    import config
    from ensemble import VETO_AUTHORIZED
    disabled = set(config.GLOBALLY_DISABLED_SPECIALISTS)
    assert not (disabled & VETO_AUTHORIZED), (
        "must not globally disable a veto-authorized specialist: %s"
        % (disabled & VETO_AUTHORIZED))


def test_ensemble_merges_global_disable_list():
    """The ensemble must fold config.GLOBALLY_DISABLED_SPECIALISTS into the
    per-run disabled set (so the disable is actually applied)."""
    src = open(os.path.join(REPO, "ensemble.py"), encoding="utf-8").read()
    assert "GLOBALLY_DISABLED_SPECIALISTS" in src, (
        "ensemble must read config.GLOBALLY_DISABLED_SPECIALISTS")
    # merged into `disabled` before the >=2 floor check
    assert "disabled |= set(getattr(_cfg, \"GLOBALLY_DISABLED_SPECIALISTS\"" in src
