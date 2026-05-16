"""VIX-fallback regression tests (2026-05-16 zero-error audit).

Pre-fix `market_regime.detect_regime()` silently substituted
`vix=20.0` whenever `_vix_from_spy_options()` returned None. That
fake "moderate" VIX got fed into every downstream consumer (AI
prompt, regime classification, trade pipeline context) on the
3+/day cadence that the Alpaca SPY-options chain was empty.

Post-fix:
  - Tier-2 fallback to yfinance ^VIX before giving up.
  - When BOTH sources fail: vix=None, vix_level='unknown',
    vix_source='unknown' — never silently substitute a fake.
  - Downstream consumers (summary, regime classification) tolerate
    vix=None and degrade gracefully.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture(autouse=True)
def _clear_regime_cache():
    """Each test starts with a cold regime cache."""
    import market_regime
    market_regime._cache["regime"] = None
    market_regime._cache["regime_ts"] = 0
    yield


def _spy_hist():
    """Minimal SPY history dataframe shape `detect_regime` reads."""
    import pandas as pd
    # 60 rows so the SMA50 / 10-day-ago logic runs cleanly.
    closes = [400.0 + i * 0.1 for i in range(60)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    return pd.DataFrame({"close": closes, "high": highs, "low": lows})


def _patch_spy_hist(monkeypatch):
    """Stub out the SPY history call so we focus on the VIX branch."""
    import market_regime
    # detect_regime fetches SPY via `client._get_history` (or similar);
    # easier route is to patch the bar-fetcher used by the module.
    # Inspect: detect_regime calls `_get_history("SPY", ...)`. Patch it.
    monkeypatch.setattr(
        market_regime, "_get_history", lambda *a, **kw: _spy_hist(),
        raising=False,
    )


class TestVixFallback:

    def test_alpaca_succeeds_uses_alpaca_source(self, monkeypatch):
        _patch_spy_hist(monkeypatch)
        with patch("market_regime._vix_from_spy_options", return_value=18.5):
            from market_regime import detect_regime
            r = detect_regime()
        assert r["vix"] == 18.5
        assert r["vix_source"] == "alpaca_spy_options"
        assert r["vix_level"] == "moderate"

    def test_alpaca_fails_falls_back_to_yfinance(self, monkeypatch):
        _patch_spy_hist(monkeypatch)
        with patch("market_regime._vix_from_spy_options", return_value=None), \
             patch("market_regime._vix_from_yfinance", return_value=22.3):
            from market_regime import detect_regime
            r = detect_regime()
        assert r["vix"] == 22.3
        assert r["vix_source"] == "yfinance_vix"

    def test_both_sources_fail_marks_unknown_never_substitutes_20(
        self, monkeypatch,
    ):
        """The whole point of the fix: NEVER silently default to 20.
        Pre-fix a missing Alpaca chain produced vix=20.0,
        vix_level='moderate' — a fake "normal market" classification
        fed straight to the AI prompt."""
        _patch_spy_hist(monkeypatch)
        with patch("market_regime._vix_from_spy_options", return_value=None), \
             patch("market_regime._vix_from_yfinance", return_value=None):
            from market_regime import detect_regime
            r = detect_regime()
        assert r["vix"] is None, (
            "vix must be None when both sources fail; got "
            f"{r['vix']!r} which is the silent-default bug"
        )
        assert r["vix"] != 20.0
        assert r["vix_level"] == "unknown"
        assert r["vix_source"] == "unknown"
        # Summary must not contain a fake VIX number.
        assert "VIX unavailable" in r["summary"], (
            f"summary must surface unavailability; got: {r['summary']!r}"
        )

    def test_unknown_vix_does_not_break_regime_classification(
        self, monkeypatch,
    ):
        """Volatile classification needs a real VIX. With unknown VIX,
        the trend-based classification still produces a valid regime
        (bull/bear/sideways) rather than crashing."""
        _patch_spy_hist(monkeypatch)
        with patch("market_regime._vix_from_spy_options", return_value=None), \
             patch("market_regime._vix_from_yfinance", return_value=None):
            from market_regime import detect_regime
            r = detect_regime()
        assert r["regime"] in ("bull", "bear", "sideways", "volatile")
        assert r["regime"] != "volatile", (
            "Without a real VIX value, the volatile classification "
            "must not fire (would silently mislabel the market)"
        )