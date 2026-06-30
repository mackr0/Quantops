"""No cross-profile concentration judgment anywhere in the decision path.

LOAD-BEARING isolation invariant: the 13 EXP-A* profiles are INDEPENDENT
virtual accounts ("different people"). One profile must NEVER be denied a
trade, sized, or steered because ANOTHER profile holds the same name. The 3
Alpaca accounts are execution conduits only; there is no shared book.

The `book_concentration` module (which globbed every quantopsai_profile_*.db,
summed a symbol's exposure ACROSS ALL PROFILES, and rejected entries at a 25%
aggregate cap — action BOOK_CONCENTRATION_CAP) was REMOVED 2026-06-30 along
with the "across sibling profiles" line it injected into the AI prompt. This
pins that it stays gone and can't be reintroduced.

Per-profile concentration is handled entirely by own-book mechanisms:
max_position_pct, the own-book specialist veto, and the own-book correlation
signal (book_fit, which reads only ctx.db_path).
"""
from __future__ import annotations

import importlib
import os

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _noncomment_hits(path, token):
    """Lines containing `token` outside of a comment."""
    hits = []
    for i, line in enumerate(open(path, encoding="utf-8").read().splitlines(), 1):
        code = line.split("#", 1)[0]
        if token in code:
            hits.append("%d: %s" % (i, line.strip()))
    return hits


def test_book_concentration_module_stays_deleted():
    try:
        importlib.import_module("book_concentration")
    except ModuleNotFoundError:
        return
    raise AssertionError(
        "book_concentration (the cross-profile concentration cap) must stay "
        "DELETED — it violated profile isolation (aggregated across all "
        "profile DBs to block one profile's trade based on others')")


def test_decision_path_has_no_cross_profile_aggregation():
    """trade_pipeline / ai_analyst must not import the cap, call would_breach,
    or glob other profiles' DBs (in code — tombstone comments are fine)."""
    for fn in ("trade_pipeline.py", "ai_analyst.py"):
        path = os.path.join(REPO, fn)
        for token in ("book_concentration", "would_breach",
                      "quantopsai_profile_*"):
            hits = _noncomment_hits(path, token)
            assert not hits, (
                "%s references cross-profile token %r in CODE (isolation "
                "violation): %s" % (fn, token, hits))


def test_book_concentration_cap_is_not_a_live_action():
    """No code path should still PRODUCE the BOOK_CONCENTRATION_CAP action
    (it's retained only as a display label for any historical rows)."""
    src = open(os.path.join(REPO, "trade_pipeline.py"), encoding="utf-8").read()
    for line in src.splitlines():
        code = line.split("#", 1)[0]
        assert '"action": "BOOK_CONCENTRATION_CAP"' not in code, (
            "trade_pipeline must not emit BOOK_CONCENTRATION_CAP: %s" % line.strip())
