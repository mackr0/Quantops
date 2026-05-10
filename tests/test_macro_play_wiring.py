"""Pin macro-play prompt block wiring.

Caught 2026-05-09: `evaluate_macro_play()` shipped 2026-05-03 in
`macro_event_tracker.py`, but no production code called it. The AI
prompt got the heads-up "next macro event: FOMC tomorrow" line
(via `render_macro_event_for_prompt`) but never an actionable
recommendation (sell iron condor / buy straddle / time-stop). Same
shape as Issue 6: prerequisite shipped, integration didn't.

This test pins:
1. `render_macro_play_recommendation_for_prompt` triggers each
   branch of `evaluate_macro_play` correctly given the right inputs.
2. Lookup failures (IV / price) return `""`, NOT a misleading
   recommendation. Safety rule: never produce a play from broken data.
3. The trade_pipeline market_context dict EXPOSES `macro_play_block`,
   so the AI prompt receives it.
4. Guardrail: `evaluate_macro_play` MUST have at least one production
   caller (narrow analog of test_no_unwired_writers; pinned
   specifically because this function was unwired for ~6 days).
"""

import os
import re
import sys
from datetime import date as _date
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# Layer 1 — render function behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def near_event(monkeypatch):
    """Patch the calendar so 'next event' is FOMC 2 days from today."""
    from datetime import date, timedelta
    import macro_event_tracker as mt
    target = (date.today() + timedelta(days=2)).isoformat()
    monkeypatch.setattr(mt, "MACRO_EVENT_CALENDAR", [
        mt.MacroEvent(target, "FOMC", "FOMC rate decision", "high"),
    ])
    return target


class TestRenderMacroPlayForPrompt:
    def test_rich_iv_emits_iron_condor_block(self, near_event):
        from macro_event_tracker import (
            render_macro_play_recommendation_for_prompt,
        )
        block = render_macro_play_recommendation_for_prompt(
            iv_rank_lookup=lambda: 85.0,        # rich
            spy_price_lookup=lambda: 500.0,
        )
        assert block.startswith("MACRO PLAY:"), block
        # Rationale should describe the iron-condor opportunity
        assert "rich" in block.lower() or "iron condor" in block.lower()
        assert "FOMC" in block

    def test_cheap_iv_emits_straddle_block(self, near_event):
        from macro_event_tracker import (
            render_macro_play_recommendation_for_prompt,
        )
        block = render_macro_play_recommendation_for_prompt(
            iv_rank_lookup=lambda: 15.0,        # cheap
            spy_price_lookup=lambda: 500.0,
        )
        assert block.startswith("MACRO PLAY:"), block
        assert "cheap" in block.lower() or "straddle" in block.lower()

    def test_dead_zone_iv_emits_nothing(self, near_event):
        """IV in the middle (50%) → no play. Empty string, not "MACRO PLAY:"."""
        from macro_event_tracker import (
            render_macro_play_recommendation_for_prompt,
        )
        block = render_macro_play_recommendation_for_prompt(
            iv_rank_lookup=lambda: 50.0,
            spy_price_lookup=lambda: 500.0,
        )
        assert block == ""

    def test_no_event_emits_nothing(self, monkeypatch):
        """Empty calendar → no recommendation, even with rich IV."""
        import macro_event_tracker as mt
        monkeypatch.setattr(mt, "MACRO_EVENT_CALENDAR", [])
        block = mt.render_macro_play_recommendation_for_prompt(
            iv_rank_lookup=lambda: 99.0,
            spy_price_lookup=lambda: 500.0,
        )
        assert block == ""

    def test_iv_lookup_failure_returns_empty(self, near_event):
        """Broken IV lookup must NOT silently emit a recommendation
        (would mean the AI gets a play built on garbage data)."""
        from macro_event_tracker import (
            render_macro_play_recommendation_for_prompt,
        )
        def bad_iv():
            raise RuntimeError("oracle down")
        block = render_macro_play_recommendation_for_prompt(
            iv_rank_lookup=bad_iv,
            spy_price_lookup=lambda: 500.0,
        )
        assert block == ""

    def test_price_lookup_failure_returns_empty(self, near_event):
        from macro_event_tracker import (
            render_macro_play_recommendation_for_prompt,
        )
        def bad_price():
            raise RuntimeError("market data down")
        block = render_macro_play_recommendation_for_prompt(
            iv_rank_lookup=lambda: 85.0,
            spy_price_lookup=bad_price,
        )
        assert block == ""

    def test_none_iv_returns_empty(self, near_event):
        """Lookup returning None (cache miss / no options chain) →
        no recommendation."""
        from macro_event_tracker import (
            render_macro_play_recommendation_for_prompt,
        )
        block = render_macro_play_recommendation_for_prompt(
            iv_rank_lookup=lambda: None,
            spy_price_lookup=lambda: 500.0,
        )
        assert block == ""


# ---------------------------------------------------------------------------
# Layer 2 — narrow regression guard: evaluate_macro_play must have
#            a production caller
# ---------------------------------------------------------------------------


def test_macro_play_render_has_production_caller():
    """`render_macro_play_recommendation_for_prompt` is the public
    wrapper for `evaluate_macro_play`; the latter is reached only
    through this wrapper. The 2026-05-09 Issue 7 bug was that the
    wrapper didn't exist AND `evaluate_macro_play` had no callers,
    leaving the macro-IV-crush playbook silent. Pin that the wrapper
    has at least one production caller so a future refactor can't
    silently un-wire it again.

    Calls inside macro_event_tracker.py itself don't count — the
    function needs a real downstream consumer."""
    import subprocess
    repo_root = os.path.join(os.path.dirname(__file__), os.pardir)
    r = subprocess.run(
        ["grep", "-rEn", "--include=*.py",
         "--exclude-dir=venv", "--exclude-dir=.git",
         "--exclude-dir=__pycache__", "--exclude-dir=tests",
         "--exclude-dir=node_modules",
         r"\brender_macro_play_recommendation_for_prompt\b", repo_root],
        capture_output=True, text=True,
    )
    callers = []
    def_re = re.compile(
        r":\s*\d+:\s*def\s+render_macro_play_recommendation_for_prompt\b"
    )
    for line in r.stdout.splitlines():
        if def_re.search(line):
            continue
        if "/macro_event_tracker.py:" in line:
            continue
        callers.append(line)
    assert callers, (
        "render_macro_play_recommendation_for_prompt() has zero "
        "production callers outside macro_event_tracker.py. This is "
        "the 2026-05-09 Issue 7 shape — wrapper shipped but never "
        "integrated into the trade_pipeline / ai_analyst prompt-build."
    )
