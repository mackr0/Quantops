"""P3.5 of LONG_SHORT_PLAN.md — insider strategy score promotion."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_insider_cluster_emits_score_3():
    """P3.5 — insider cluster signals must carry score 3 so they
    reliably reach the AI's top-15 shortlist."""
    from strategies.insider_cluster import find_candidates

    insider_data = {
        "recent_buys": 5,
        "recent_sells": 1,
        "total_buy_value": 1_000_000,
    }
    with patch("alternative_data.get_insider_activity",
                return_value=insider_data):
        results = find_candidates(None, ["TSLA"])

    assert len(results) == 1
    assert results[0]["score"] == 3, (
        "insider_cluster score was reduced from 3. P3.5 of "
        "LONG_SHORT_PLAN.md requires score >= 3 so insider buys reach "
        "the top-15 shortlist reliably."
    )


def test_insider_selling_cluster_emits_score_3():
    """P3.5 — insider selling cluster must also carry score 3."""
    import pandas as pd
    from strategies.insider_selling_cluster import find_candidates

    insider_data = {
        "recent_buys": 1,
        "recent_sells": 5,
        "total_sell_value": 800_000,
    }
    bars = pd.DataFrame({
        "open": [100], "high": [101], "low": [99],
        "close": [100], "volume": [1_000_000],
    })
    with patch("alternative_data.get_insider_activity",
                return_value=insider_data), \
         patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["WFC"])

    assert len(results) == 1
    assert results[0]["score"] == 3, (
        "insider_selling_cluster score was reduced from 3. P3.5 of "
        "LONG_SHORT_PLAN.md requires score >= 3."
    )


def test_score_promotion_documented_in_source():
    """Source-level guard: the score=3 must be associated with the
    P3.5 comment so future refactors don't silently drop it back to 2."""
    import pathlib
    cluster = pathlib.Path("strategies/insider_cluster.py").read_text()
    selling = pathlib.Path("strategies/insider_selling_cluster.py").read_text()
    assert '"score": 3' in cluster, "insider_cluster missing score=3"
    assert '"score": 3' in selling, "insider_selling_cluster missing score=3"
    assert "P3.5" in cluster, "insider_cluster missing P3.5 promotion comment"
    assert "P3.5" in selling, "insider_selling_cluster missing P3.5 comment"
