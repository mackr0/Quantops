"""Test the `screener.is_alpaca_active` prefilter used at every
yfinance fallback site (8 in alternative_data + factor_data +
earnings_calendar + sector_classifier).

Pre-2026-05-16 every cron run hit yfinance for ~10 known-delisted
tickers (BRK.B, CS, CT, HN, NJ, OL, REV, SPYB, SQ, VA) producing
"possibly delisted" ERROR log spam. Per the Alpaca-first data-
source rule, yfinance is the last resort and shouldn't be queried
for symbols Alpaca can't trade anyway.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with a cold active-symbols cache."""
    import screener
    screener._active_symbols_cache = {"timestamp": 0.0, "symbols": set()}
    yield


class TestIsAlpacaActive:

    def test_symbol_in_active_set_returns_true(self):
        from screener import is_alpaca_active
        with patch("screener.get_active_alpaca_symbols",
                   return_value={"AAPL", "MSFT", "NVDA"}):
            assert is_alpaca_active("AAPL") is True
            assert is_alpaca_active("MSFT") is True

    def test_delisted_symbol_returns_false(self):
        """The whole point of the guard: known-delisted symbols
        (not in Alpaca's active list) get rejected so yfinance
        never sees them."""
        from screener import is_alpaca_active
        with patch("screener.get_active_alpaca_symbols",
                   return_value={"AAPL"}):
            assert is_alpaca_active("BRK.B") is False
            assert is_alpaca_active("CS") is False
            assert is_alpaca_active("REV") is False
            assert is_alpaca_active("SQ") is False

    def test_case_insensitive(self):
        from screener import is_alpaca_active
        with patch("screener.get_active_alpaca_symbols",
                   return_value={"AAPL"}):
            assert is_alpaca_active("aapl") is True
            assert is_alpaca_active("AaPl") is True

    def test_empty_string_returns_false(self):
        from screener import is_alpaca_active
        assert is_alpaca_active("") is False
        assert is_alpaca_active(None) is False

    def test_empty_cache_is_permissive(self):
        """Cold start / Alpaca outage → empty cache. Returning False
        for everything would block ALL yfinance lookups forever.
        Permissive on empty cache."""
        from screener import is_alpaca_active
        with patch("screener.get_active_alpaca_symbols",
                   return_value=set()):
            assert is_alpaca_active("AAPL") is True
            assert is_alpaca_active("BRK.B") is True


class TestSkipYfInAlternativeData:
    """Verify alternative_data._skip_yf wires through to
    is_alpaca_active correctly."""

    def test_skips_for_non_active(self):
        import alternative_data
        with patch("screener.get_active_alpaca_symbols",
                   return_value={"AAPL"}):
            assert alternative_data._skip_yf("BRK.B") is True
            assert alternative_data._skip_yf("AAPL") is False

    def test_permissive_when_screener_import_fails(self):
        """If for some reason the screener module isn't importable
        (test contexts, partial install), default to NOT skipping —
        the existing yfinance error handling becomes the safety net."""
        import alternative_data
        with patch(
            "screener.is_alpaca_active",
            side_effect=ImportError("simulated"),
        ):
            # ImportError inside is_alpaca_active means _skip_yf
            # returns False (permissive).
            assert alternative_data._skip_yf("AAPL") is False
