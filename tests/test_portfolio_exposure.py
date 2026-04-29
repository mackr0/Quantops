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
