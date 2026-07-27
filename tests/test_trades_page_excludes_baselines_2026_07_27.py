"""/trades excludes the baseline/benchmark control profiles
(2026-07-27, operator request).

Every other page and dropdown scopes the benchmark controls
(BuyHoldSPY / RandomA / RandomB) out of the book via
profile_classification.is_baseline_profile; /trades listed them —
inconsistent, and controls aren't part of "the book".
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_trades_route_filters_baselines():
    src = open(os.path.join(REPO, "views.py")).read()
    i = src.find('@views_bp.route("/trades")')
    assert i != -1
    block = src[i:i + 900]
    assert "is_baseline_profile" in block, (
        "/trades stopped filtering baseline profiles — the benchmark "
        "accounts leak back into the trade list and dropdown."
    )


def test_predicate_covers_the_benchmark_strategy_types():
    from profile_classification import is_baseline_profile
    assert is_baseline_profile({"strategy_type": "buy_hold"})
    assert is_baseline_profile({"strategy_type": "random"})
    # the one AI value, and the documented blank-defaults-to-AI rule
    assert not is_baseline_profile({"strategy_type": "ai"})
    assert not is_baseline_profile({"strategy_type": ""})
