"""P2.1 of LONG_SHORT_PLAN.md — sector exposure tracking."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from portfolio_exposure import compute_exposure, render_for_prompt


def _stub_lookup(mapping):
    return lambda sym: mapping.get(sym.upper(), "Unknown")


def test_empty_positions_returns_zero_exposure():
    out = compute_exposure([], equity=100_000)
    assert out["net_pct"] == 0.0
    assert out["gross_pct"] == 0.0
    assert out["num_positions"] == 0
    assert out["by_sector"] == {}
    assert out["concentration_flags"] == []


def test_zero_equity_returns_zeros():
    out = compute_exposure(
        [{"symbol": "AAPL", "qty": 10, "market_value": 2000}],
        equity=0,
    )
    assert out["num_positions"] == 0  # bails out early


def test_long_only_sector_breakdown():
    positions = [
        {"symbol": "AAPL", "qty": 100, "market_value": 20_000},
        {"symbol": "MSFT", "qty": 50,  "market_value": 15_000},
        {"symbol": "JPM",  "qty": 100, "market_value": 10_000},
    ]
    lookup = _stub_lookup({"AAPL": "Technology", "MSFT": "Technology",
                            "JPM": "Financials"})
    out = compute_exposure(positions, equity=100_000, sector_lookup=lookup)
    assert out["net_pct"] == 45.0   # 45k long / 100k equity
    assert out["gross_pct"] == 45.0
    assert out["num_positions"] == 3
    tech = out["by_sector"]["Technology"]
    assert tech["long_pct"] == 35.0
    assert tech["short_pct"] == 0.0
    assert tech["net_pct"] == 35.0
    assert tech["n_long"] == 2
    assert tech["n_short"] == 0
    fin = out["by_sector"]["Financials"]
    assert fin["long_pct"] == 10.0
    assert fin["n_long"] == 1


def test_long_short_same_sector_nets_correctly():
    """Long AAPL + short MSFT in same sector → net is the difference."""
    positions = [
        {"symbol": "AAPL", "qty": 100,  "market_value": 30_000},
        {"symbol": "MSFT", "qty": -50,  "market_value": -15_000},
    ]
    lookup = _stub_lookup({"AAPL": "Technology", "MSFT": "Technology"})
    out = compute_exposure(positions, equity=100_000, sector_lookup=lookup)
    tech = out["by_sector"]["Technology"]
    assert tech["long_pct"] == 30.0
    assert tech["short_pct"] == 15.0
    assert tech["net_pct"] == 15.0
    assert tech["gross_pct"] == 45.0
    # Aggregate net = 30 long - 15 short = 15
    assert out["net_pct"] == 15.0
    assert out["gross_pct"] == 45.0


def test_concentration_flag_fires_at_threshold():
    """Sector >= 30% of gross book gets flagged."""
    positions = [
        {"symbol": "AAPL", "qty": 100, "market_value": 35_000},
        {"symbol": "JPM",  "qty": 100, "market_value": 10_000},
    ]
    lookup = _stub_lookup({"AAPL": "Technology", "JPM": "Financials"})
    out = compute_exposure(positions, equity=100_000, sector_lookup=lookup)
    assert "Technology" in out["concentration_flags"]
    assert "Financials" not in out["concentration_flags"]


def test_unknown_sector_grouped_separately():
    positions = [
        {"symbol": "WEIRDCO", "qty": 100, "market_value": 5000},
    ]
    lookup = _stub_lookup({})  # nothing maps
    out = compute_exposure(positions, equity=50_000, sector_lookup=lookup)
    assert "Unknown" in out["by_sector"]
    assert out["by_sector"]["Unknown"]["long_pct"] == 10.0


def test_sector_lookup_failure_falls_back_to_unknown():
    """If sector lookup raises, position still counts under Unknown."""
    def broken_lookup(sym):
        raise RuntimeError("yfinance down")
    positions = [
        {"symbol": "AAPL", "qty": 100, "market_value": 20_000},
    ]
    out = compute_exposure(positions, equity=100_000, sector_lookup=broken_lookup)
    assert out["num_positions"] == 1
    assert "Unknown" in out["by_sector"]


def test_render_for_prompt_includes_top_sectors():
    out = compute_exposure(
        [
            {"symbol": "AAPL", "qty": 100, "market_value": 30_000},
            {"symbol": "MSFT", "qty": -50, "market_value": -15_000},
            {"symbol": "JPM",  "qty": 100, "market_value": 10_000},
        ],
        equity=100_000,
        sector_lookup=_stub_lookup({
            "AAPL": "Technology", "MSFT": "Technology", "JPM": "Financials",
        }),
    )
    rendered = render_for_prompt(out)
    assert "Technology" in rendered
    assert "Financials" in rendered
    assert "Net:" in rendered or "net" in rendered.lower()


def test_render_for_prompt_handles_empty():
    rendered = render_for_prompt(
        {"net_pct": 0, "gross_pct": 0, "num_positions": 0,
         "by_sector": {}, "concentration_flags": []}
    )
    assert "No open positions" in rendered


def test_find_pairs_returns_empty_for_empty_candidates():
    from portfolio_exposure import find_pair_opportunities
    assert find_pair_opportunities([]) == []


def test_find_pairs_returns_empty_when_no_same_sector_pair():
    """Long Tech + short Energy → no pair (different sectors)."""
    from portfolio_exposure import find_pair_opportunities
    candidates = [
        {"symbol": "AAPL", "signal": "BUY", "score": 3, "reason": "tech rip"},
        {"symbol": "XOM", "signal": "SHORT", "score": 2, "reason": "energy weak"},
    ]
    lookup = _stub_lookup({"AAPL": "Technology", "XOM": "Energy"})
    assert find_pair_opportunities(candidates, sector_lookup=lookup) == []


def test_find_pairs_pairs_top_long_with_top_short_in_sector():
    from portfolio_exposure import find_pair_opportunities
    candidates = [
        {"symbol": "AAPL", "signal": "BUY", "score": 3,
         "reason": "tech leader"},
        {"symbol": "MSFT", "signal": "BUY", "score": 1,
         "reason": "tech mid"},
        {"symbol": "INTC", "signal": "SHORT", "score": 2,
         "reason": "tech laggard"},
    ]
    lookup = _stub_lookup({"AAPL": "Technology", "MSFT": "Technology",
                            "INTC": "Technology"})
    pairs = find_pair_opportunities(candidates, sector_lookup=lookup)
    assert len(pairs) == 1
    assert pairs[0]["sector"] == "Technology"
    # AAPL has higher score than MSFT — should be the long pick
    assert pairs[0]["long"]["symbol"] == "AAPL"
    assert pairs[0]["short"]["symbol"] == "INTC"
    assert pairs[0]["combined_score"] == 5


def test_find_pairs_returns_multiple_sectors_sorted():
    from portfolio_exposure import find_pair_opportunities
    candidates = [
        # Tech pair (combined score 4)
        {"symbol": "AAPL", "signal": "BUY", "score": 2},
        {"symbol": "INTC", "signal": "SHORT", "score": 2},
        # Energy pair (combined score 6)
        {"symbol": "CVX", "signal": "BUY", "score": 3},
        {"symbol": "XOM", "signal": "SHORT", "score": 3},
    ]
    lookup = _stub_lookup({"AAPL": "Technology", "INTC": "Technology",
                            "CVX": "Energy", "XOM": "Energy"})
    pairs = find_pair_opportunities(candidates, sector_lookup=lookup)
    assert len(pairs) == 2
    # Energy pair has higher combined score → should sort first
    assert pairs[0]["sector"] == "Energy"
    assert pairs[1]["sector"] == "Technology"


def test_find_pairs_respects_max_pairs():
    from portfolio_exposure import find_pair_opportunities
    candidates = []
    for prefix, sector in [("A", "S1"), ("B", "S2"), ("C", "S3"), ("D", "S4")]:
        candidates.append({"symbol": f"{prefix}L", "signal": "BUY", "score": 2})
        candidates.append({"symbol": f"{prefix}S", "signal": "SHORT", "score": 2})
    lookup = _stub_lookup({c["symbol"]: c["symbol"][0] for c in candidates})
    pairs = find_pair_opportunities(candidates, sector_lookup=lookup, max_pairs=2)
    assert len(pairs) == 2


def test_render_pairs_for_prompt_renders_each_pair():
    from portfolio_exposure import render_pairs_for_prompt
    pairs = [
        {"sector": "Technology",
         "long":  {"symbol": "AAPL", "signal": "BUY", "score": 3, "reason": "leader"},
         "short": {"symbol": "INTC", "signal": "SHORT", "score": 2, "reason": "laggard"},
         "combined_score": 5},
    ]
    rendered = render_pairs_for_prompt(pairs)
    assert "PAIR OPPORTUNITIES" in rendered
    assert "Technology" in rendered
    assert "AAPL" in rendered
    assert "INTC" in rendered


def test_render_pairs_for_prompt_empty_returns_empty_string():
    from portfolio_exposure import render_pairs_for_prompt
    assert render_pairs_for_prompt([]) == ""


# ---------------------------------------------------------------------------
# P2.5 — factor exposure
# ---------------------------------------------------------------------------

def test_factor_exposure_empty_positions():
    from portfolio_exposure import compute_factor_exposure
    out = compute_factor_exposure([], equity=100_000)
    for band in ("cheap", "mid", "expensive"):
        assert out["size_bands"][band]["long_pct"] == 0.0
        assert out["size_bands"][band]["short_pct"] == 0.0
    assert out["direction"]["long_share"] == 0.0
    assert not out["direction"]["single_direction_concentrated"]


def test_factor_exposure_buckets_by_price():
    from portfolio_exposure import compute_factor_exposure
    positions = [
        # cheap (price = 5000/1000 = $5)
        {"symbol": "AAA", "qty": 1000, "market_value": 5000},
        # mid (price = 30000/600 = $50)
        {"symbol": "BBB", "qty": 600, "market_value": 30000},
        # expensive (price = 200000/1000 = $200)
        {"symbol": "CCC", "qty": 1000, "market_value": 200000},
    ]
    out = compute_factor_exposure(positions, equity=1_000_000)
    assert out["size_bands"]["cheap"]["long_pct"] == 0.5
    assert out["size_bands"]["mid"]["long_pct"] == 3.0
    assert out["size_bands"]["expensive"]["long_pct"] == 20.0
    assert out["size_bands"]["cheap"]["n_long"] == 1
    assert out["size_bands"]["mid"]["n_long"] == 1
    assert out["size_bands"]["expensive"]["n_long"] == 1


def test_factor_exposure_short_breakdown():
    from portfolio_exposure import compute_factor_exposure
    # Long $20K cheap + short $10K cheap → both in cheap bucket
    positions = [
        {"symbol": "AAA", "qty": 4000, "market_value": 20_000},  # $5
        {"symbol": "BBB", "qty": -2000, "market_value": -10_000},  # $5
    ]
    out = compute_factor_exposure(positions, equity=100_000)
    cheap = out["size_bands"]["cheap"]
    assert cheap["long_pct"] == 20.0
    assert cheap["short_pct"] == 10.0
    assert cheap["n_long"] == 1
    assert cheap["n_short"] == 1


def test_factor_exposure_directional_concentration_flag():
    from portfolio_exposure import compute_factor_exposure
    # All long → 100% long share → flag fires
    positions = [
        {"symbol": f"S{i}", "qty": 100, "market_value": 5000}
        for i in range(3)
    ]
    out = compute_factor_exposure(positions, equity=100_000)
    assert out["direction"]["long_share"] == 1.0
    assert out["direction"]["short_share"] == 0.0
    assert out["direction"]["single_direction_concentrated"] is True


def test_factor_exposure_balanced_book_not_flagged():
    from portfolio_exposure import compute_factor_exposure
    positions = [
        {"symbol": "L1", "qty": 100, "market_value": 5000},
        {"symbol": "S1", "qty": -100, "market_value": -5000},
    ]
    out = compute_factor_exposure(positions, equity=100_000)
    assert out["direction"]["long_share"] == 0.5
    assert not out["direction"]["single_direction_concentrated"]


def test_factor_exposure_bundled_into_compute_exposure():
    """compute_exposure now includes 'factors' key wrapping the
    new size + direction breakdown."""
    from portfolio_exposure import compute_exposure
    positions = [
        {"symbol": "AAPL", "qty": 100, "market_value": 20_000},
    ]
    lookup = lambda sym: "Technology"
    out = compute_exposure(positions, equity=100_000, sector_lookup=lookup)
    assert "factors" in out
    assert "size_bands" in out["factors"]
    assert "direction" in out["factors"]


def test_render_for_prompt_warns_on_concentration():
    out = compute_exposure(
        [
            {"symbol": "AAPL", "qty": 100, "market_value": 50_000},
        ],
        equity=100_000,
        sector_lookup=_stub_lookup({"AAPL": "Technology"}),
    )
    rendered = render_for_prompt(out)
    assert "CONCENTRATION" in rendered.upper()
