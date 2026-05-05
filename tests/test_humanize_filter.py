"""humanize() filter tests.

Catches the failure mode that bit prod 2026-05-05: dynamic content
(LLM reasoning, Alpaca order types) flows through templates as
strings and `STRONG_BUY` / `bull_put_spread` / `TRAILING_STOP` reach
the user. The static template guardrail can't catch dynamic content;
applying `| humanize` to all dynamic strings is the answer.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_lower_snake_to_title_case():
    from display_names import humanize
    assert humanize("bull_put_spread") == "Bull Put Spread"
    assert humanize("trailing_stop") == "Trailing Stop"


def test_upper_snake_normalized():
    from display_names import humanize
    assert humanize("STRONG_BUY") == "Strong Buy"
    assert humanize("TRAILING_STOP") == "Trailing Stop"


def test_known_mappings_take_precedence():
    """If display_names has an explicit mapping for the lower form,
    use it — even when called with the upper form."""
    from display_names import humanize, _DISPLAY_NAMES
    if "sector_momentum_rotation" in _DISPLAY_NAMES:
        expected = _DISPLAY_NAMES["sector_momentum_rotation"]
        assert humanize("sector_momentum_rotation") == expected
        assert humanize("SECTOR_MOMENTUM_ROTATION") == expected


def test_freeform_text_with_embedded_tokens():
    """Real-world LLM reasoning: paragraph containing snake_case mid-sentence."""
    from display_names import humanize
    text = (
        "SMMT is attractive on technicals (STRONG_BUY, RSI 38). "
        "Proposed bull_put_spread at $16.00P / $15.00P."
    )
    out = humanize(text)
    assert "STRONG_BUY" not in out
    assert "bull_put_spread" not in out
    assert "Strong Buy" in out
    assert "Bull Put Spread" in out


def test_no_snake_case_passes_through():
    from display_names import humanize
    s = "Just a normal sentence with no internal tokens."
    assert humanize(s) == s


def test_idempotent():
    from display_names import humanize
    s = "STRONG_BUY signal"
    assert humanize(humanize(s)) == humanize(s)


def test_preserves_numbers_and_punctuation():
    from display_names import humanize
    s = "Position closed at $123.45 (-2.5%). stop_loss at $120."
    out = humanize(s)
    assert "$123.45" in out
    assert "-2.5%" in out
    assert "Stop Loss" in out


def test_handles_none_and_empty():
    from display_names import humanize
    assert humanize(None) == ""
    assert humanize("") == ""


def test_jinja_filter_registered():
    """Verify the humanize filter is wired into the Flask app."""
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    from app import create_app
    app = create_app()
    assert "humanize" in app.jinja_env.filters
