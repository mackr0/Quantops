"""P3.6 of LONG_SHORT_PLAN.md — factor_data module tests."""
from __future__ import annotations

import os
import sys
import sqlite3
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_factor_db(tmp_path, monkeypatch):
    """Point factor_data at a temp DB and reset module state per test."""
    db = str(tmp_path / "factor.db")
    import factor_data
    monkeypatch.setattr(factor_data, "_DB_PATH", db)
    factor_data._table_ensured = False
    yield db


def test_book_to_market_classifier():
    from factor_data import classify_book_to_market
    assert classify_book_to_market(None) == "unknown"
    assert classify_book_to_market(1.5) == "value"
    assert classify_book_to_market(0.5) == "mid"
    assert classify_book_to_market(0.1) == "growth"
    # Boundary cases
    assert classify_book_to_market(1.0) == "value"
    assert classify_book_to_market(0.3) == "mid"
    assert classify_book_to_market(0.299) == "growth"


def test_beta_classifier():
    from factor_data import classify_beta
    assert classify_beta(None) == "unknown"
    assert classify_beta(0.4) == "defensive"
    assert classify_beta(1.0) == "market"
    assert classify_beta(1.8) == "levered"
    # Boundaries
    assert classify_beta(0.7) == "market"
    assert classify_beta(1.3) == "market"
    assert classify_beta(1.31) == "levered"


def test_momentum_classifier():
    from factor_data import classify_momentum
    assert classify_momentum(None) == "unknown"
    assert classify_momentum(0.20) == "winner"
    assert classify_momentum(0.05) == "neutral"
    assert classify_momentum(-0.20) == "loser"
    assert classify_momentum(0.10) == "neutral"  # exactly at boundary
    assert classify_momentum(0.11) == "winner"
    assert classify_momentum(-0.10) == "neutral"
    assert classify_momentum(-0.11) == "loser"


def test_get_book_to_market_caches_result(tmp_factor_db):
    """Second call should hit cache, not yfinance."""
    from factor_data import get_book_to_market
    fake_ticker = MagicMock()
    fake_ticker.info = {
        "bookValue": 50.0, "sharesOutstanding": 1_000_000_000,
        "marketCap": 100_000_000_000,
    }
    fake_yf = MagicMock()
    fake_yf.Ticker.return_value = fake_ticker
    with patch.dict("sys.modules", {"yfinance": fake_yf}):
        first = get_book_to_market("AAPL")
        # 50 * 1e9 / 100e9 = 0.5
        assert first == pytest.approx(0.5, abs=1e-6)
        # Second call: yfinance is NOT called
        fake_yf.Ticker.reset_mock()
        second = get_book_to_market("AAPL")
        assert second == pytest.approx(0.5, abs=1e-6)
        fake_yf.Ticker.assert_not_called()


def test_get_book_to_market_returns_none_on_missing_data(tmp_factor_db):
    from factor_data import get_book_to_market
    fake_ticker = MagicMock()
    fake_ticker.info = {}  # no fundamentals
    fake_yf = MagicMock()
    fake_yf.Ticker.return_value = fake_ticker
    with patch.dict("sys.modules", {"yfinance": fake_yf}):
        assert get_book_to_market("UNKNOWN") is None


def test_get_beta_uses_yfinance_info_beta(tmp_factor_db):
    from factor_data import get_beta
    fake_ticker = MagicMock()
    fake_ticker.info = {"beta": 1.42}
    fake_yf = MagicMock()
    fake_yf.Ticker.return_value = fake_ticker
    with patch.dict("sys.modules", {"yfinance": fake_yf}):
        assert get_beta("TSLA") == pytest.approx(1.42)


def test_get_momentum_12_1_skips_recent_month(tmp_factor_db):
    """Verify the 12-1m formula uses price 252 trading days ago vs
    21 trading days ago (NOT vs the most recent close)."""
    import pandas as pd
    from factor_data import get_momentum_12_1
    # Build 270 daily bars: linear ramp 50→100 (so 252-day ago = 50,
    # 21-day ago ≈ 96.something). Then a -50% crash in the last 21 days
    # which the 12-1m formula should IGNORE.
    n = 270
    prices = [50 + (50 * i / 248) for i in range(249)]  # 0..248 → 50..100
    # Last 21 days drop to 50 (-50%)
    prices += [100 - (50 * i / 20) for i in range(21)]
    bars = pd.DataFrame({
        "open": prices, "high": prices, "low": prices,
        "close": prices, "volume": [1_000_000] * len(prices),
    })
    with patch("market_data.get_bars", return_value=bars):
        mom = get_momentum_12_1("CRASH")
    # price[-252] = prices[18] ≈ 53.6
    # price[-21] ≈ 100
    # mom ≈ (100 - 53.6) / 53.6 ≈ +0.87
    assert mom is not None and mom > 0.5, (
        f"momentum should reflect 12-month rally, not the last-month "
        f"crash. Got {mom}"
    )


def test_momentum_returns_none_when_insufficient_history(tmp_factor_db):
    import pandas as pd
    from factor_data import get_momentum_12_1
    bars = pd.DataFrame({
        "close": [100] * 50, "volume": [1_000_000] * 50,
        "open": [100] * 50, "high": [100] * 50, "low": [100] * 50,
    })
    with patch("market_data.get_bars", return_value=bars):
        assert get_momentum_12_1("YOUNG") is None


def test_get_factor_classification_returns_all_three(tmp_factor_db):
    """Single-call helper that returns btm/beta/momentum buckets."""
    from factor_data import get_factor_classification
    with patch("factor_data.get_book_to_market", return_value=0.5), \
         patch("factor_data.get_beta", return_value=1.5), \
         patch("factor_data.get_momentum_12_1", return_value=0.20):
        cls = get_factor_classification("TSLA")
    assert cls == {"btm": "mid", "beta": "levered", "momentum": "winner"}


def test_factor_data_handles_yfinance_exception_gracefully(tmp_factor_db):
    """yfinance can raise on rate limit / network — must return None
    instead of propagating the exception."""
    from factor_data import get_book_to_market
    fake_yf = MagicMock()
    fake_yf.Ticker.side_effect = RuntimeError("rate limit")
    with patch.dict("sys.modules", {"yfinance": fake_yf}):
        assert get_book_to_market("ANYTHING") is None


def test_crypto_symbols_skipped(tmp_factor_db):
    """Crypto pairs (with /) shouldn't be passed to yfinance."""
    from factor_data import get_book_to_market, get_beta, get_momentum_12_1
    assert get_book_to_market("BTC/USD") is None
    assert get_beta("ETH/USD") is None
    assert get_momentum_12_1("BTC/USD") is None


def test_compute_factor_exposure_includes_real_factors():
    """compute_factor_exposure now exposes book_to_market / beta /
    momentum buckets (in addition to the existing size_bands +
    direction)."""
    from portfolio_exposure import compute_factor_exposure
    positions = [
        {"symbol": "AAPL", "qty": 100, "market_value": 20_000},
        {"symbol": "TSLA", "qty": -50, "market_value": -10_000},
    ]
    fake_factors = {
        "AAPL": {"btm": "mid", "beta": "market", "momentum": "winner"},
        "TSLA": {"btm": "growth", "beta": "levered", "momentum": "loser"},
    }
    out = compute_factor_exposure(
        positions, equity=100_000,
        factor_lookup=lambda s: fake_factors.get(s, {}),
    )
    # AAPL contributes 20% gross to mid B/M, market beta, winner mom
    assert out["book_to_market"]["mid"] == 20.0
    assert out["beta"]["market"] == 20.0
    assert out["momentum"]["winner"] == 20.0
    # TSLA contributes 10% gross to growth B/M, levered beta, loser mom
    assert out["book_to_market"]["growth"] == 10.0
    assert out["beta"]["levered"] == 10.0
    assert out["momentum"]["loser"] == 10.0


def test_render_for_prompt_surfaces_real_factor_lines():
    """P3.6 — factor lines appear in the prompt rendering when
    book_to_market / beta / momentum buckets have real weight.
    Regresses a bug where the render path read exposure[<factor>]
    at top-level instead of exposure['factors'][<factor>]."""
    from portfolio_exposure import compute_exposure, render_for_prompt

    positions = [
        {"symbol": "AAPL", "qty": 100, "market_value": 20_000},
        {"symbol": "TSLA", "qty": 50, "market_value": 10_000},
    ]
    fake_factors = {
        "AAPL": {"btm": "growth", "beta": "market", "momentum": "winner"},
        "TSLA": {"btm": "growth", "beta": "levered", "momentum": "winner"},
    }
    exp = compute_exposure(
        positions, equity=100_000,
        sector_lookup=lambda s: "Technology",
        # compute_exposure forwards factor_lookup via the helper —
        # but currently it always reads from factor_data; we patch
        # via a wrapper to skip the yfinance hop.
    )
    # Manually populate the factor buckets to mimic real lookup output
    exp["factors"]["book_to_market"] = {"value": 0.0, "mid": 0.0,
                                          "growth": 30.0, "unknown": 0.0}
    exp["factors"]["beta"] = {"defensive": 0.0, "market": 20.0,
                                "levered": 10.0, "unknown": 0.0}
    exp["factors"]["momentum"] = {"winner": 30.0, "neutral": 0.0,
                                    "loser": 0.0, "unknown": 0.0}
    rendered = render_for_prompt(exp)
    assert "By value/growth" in rendered, (
        f"factor render path broken — book_to_market line missing. "
        f"Output: {rendered}"
    )
    assert "By beta vs SPY" in rendered
    assert "By 12-1m momentum" in rendered
    assert "growth 30.0%" in rendered


def test_factor_exposure_handles_missing_lookup_gracefully():
    """When factor_lookup raises for some symbols, those positions
    fall into 'unknown' buckets — they don't crash the run."""
    from portfolio_exposure import compute_factor_exposure
    positions = [
        {"symbol": "GOOD", "qty": 100, "market_value": 10_000},
        {"symbol": "BAD", "qty": 100, "market_value": 5_000},
    ]
    def picky_lookup(sym):
        if sym == "BAD":
            raise RuntimeError("yfinance down for this one")
        return {"btm": "value", "beta": "market", "momentum": "winner"}
    out = compute_factor_exposure(
        positions, equity=100_000, factor_lookup=picky_lookup,
    )
    # GOOD gets buckets; BAD lands in unknown
    assert out["book_to_market"]["value"] == 10.0
    assert out["book_to_market"]["unknown"] == 5.0
