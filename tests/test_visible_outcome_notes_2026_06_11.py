"""Visible outcome notes on AI Brain trade badges (2026-06-11).

Operator feedback: "I'm not a fan of invisible tooltips." Every
execution-outcome badge (REJECTED / EXECUTED AS … / BLOCKED /
CANCELED / GATED) carried its explanation only in a `title=` hover
tooltip — effectively invisible. The operator hit this live: the
FRMI "EXECUTED AS LONG CLOSE" badge gave no visible reason, and the
hover text was wrong anyway (hardcoded "F" instead of the symbol —
an f-string that never got its prefix).

Pins:
  1. views.py converted_to_close display interpolates the actual
     symbol; the literal "F was already held long" string is dead.
  2. Every execution_outcome badge branch in dashboard.html sets
     `outcomeNote`, and the trade-line assembly renders it as a
     visible element (not only a title attribute).
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _views():
    return (REPO / "views.py").read_text()


def _dashboard():
    return (REPO / "templates/dashboard.html").read_text()


def test_converted_to_close_names_the_actual_symbol():
    src = _views()
    # The exact pre-fix DISPLAY string (a comment at the top of the
    # enrichment block legitimately mentions the original Ford
    # incident, so we pin the string literal, not the phrase).
    assert "F was already held long, can't open" not in src, (
        "The converted_to_close explanation regressed to the "
        "hardcoded-'F' string — every conversion will claim Ford "
        "was the held position."
    )
    start = src.index('"converted_to_close"')
    block = src[start:start + 800]
    assert "{sym}" in block, (
        "converted_to_close display must interpolate the actual "
        "symbol so the operator knows WHICH long blocked the short."
    )


def test_every_outcome_badge_sets_a_visible_note():
    src = _dashboard()
    for outcome in ("'rejected'", "'converted_to_close'",
                    "'no_fill'", "'canceled'", "'gated'"):
        idx = src.index("t.execution_outcome === " + outcome)
        # The branch body runs until the closing brace of its if —
        # approximate with a forward window; each branch is < 1500
        # chars including comments.
        branch = src[idx:idx + 1500]
        assert "outcomeNote = " in branch, (
            f"execution_outcome {outcome} badge no longer sets "
            "outcomeNote — its explanation is hover-only again "
            "(the invisible-tooltip class the operator rejected)."
        )


def test_outcome_note_rendered_visibly_not_just_title():
    src = _dashboard()
    idx = src.index("outcomeNote\n")  # assembly usage
    assembly = src[idx:idx + 400]
    assert "<small" in assembly and "outcomeColor" in assembly, (
        "outcomeNote must render as a visible <small> element under "
        "the trade line, not only inside a title= attribute."
    )
