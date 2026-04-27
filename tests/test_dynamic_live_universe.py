"""Guardrails for `segments.get_live_universe` — DYNAMIC_UNIVERSE_PLAN.md
Steps 3-5 (the live-universe half).

The function returns the live trading universe for a segment. Behavior
is feature-flagged on `USE_DYNAMIC_UNIVERSE`:
- Default (flag off): returns the hardcoded list (historic behavior).
- Flag on: returns hardcoded list ∩ Alpaca-active (delisted names
  silently dropped).

These tests prove:

1. Default path returns the hardcoded list verbatim.
2. Flag-on path filters by Alpaca-active set.
3. Flag-on path with empty Alpaca set falls back to hardcoded list
   (no crash, self-healing).
4. Crypto bypasses the dynamic path entirely (always hardcoded).
5. Unknown segment raises KeyError (matches `get_segment` contract).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _no_flag_env(monkeypatch):
    monkeypatch.delenv("USE_DYNAMIC_UNIVERSE", raising=False)


def _flag_on(monkeypatch):
    monkeypatch.setenv("USE_DYNAMIC_UNIVERSE", "true")


def test_default_returns_hardcoded_list(monkeypatch):
    """With the feature flag unset, behavior must equal the legacy
    `seg["universe"]` lookup."""
    _no_flag_env(monkeypatch)
    import segments
    expected = list(segments.SEGMENTS["small"]["universe"])
    result = segments.get_live_universe("small")
    assert result == expected


def test_flag_on_filters_by_alpaca_active(monkeypatch):
    """With the flag on, the result must be the intersection of the
    hardcoded list and Alpaca's active asset set."""
    _flag_on(monkeypatch)
    import segments
    hardcoded = set(segments.SEGMENTS["small"]["universe"])
    # Pretend only the first 5 hardcoded names are still active
    active_subset = set(list(hardcoded)[:5])
    with patch("screener.get_active_alpaca_symbols",
               return_value=active_subset):
        result = segments.get_live_universe("small")
    assert set(result) == active_subset
    # Crucially: dead names (the rest of the hardcoded list) are dropped
    dropped = hardcoded - active_subset
    assert not (dropped & set(result)), (
        "Dynamic-universe flag must drop hardcoded names that aren't "
        "in Alpaca's active set."
    )


def test_flag_on_empty_alpaca_falls_back_to_hardcoded(monkeypatch):
    """If Alpaca is unreachable and the cache is cold,
    get_active_alpaca_symbols returns an empty set. Must not crash;
    must fall back to the hardcoded list. Self-healing on next
    successful Alpaca call."""
    _flag_on(monkeypatch)
    import segments
    expected = list(segments.SEGMENTS["small"]["universe"])
    with patch("screener.get_active_alpaca_symbols", return_value=set()):
        result = segments.get_live_universe("small")
    assert result == expected


def test_flag_on_alpaca_exception_falls_back(monkeypatch):
    """If get_active_alpaca_symbols raises, function must fail open
    to the hardcoded list rather than break the caller."""
    _flag_on(monkeypatch)
    import segments
    expected = list(segments.SEGMENTS["small"]["universe"])
    with patch("screener.get_active_alpaca_symbols",
               side_effect=RuntimeError("alpaca down")):
        result = segments.get_live_universe("small")
    assert result == expected


def test_crypto_bypasses_dynamic_filter_even_with_flag_on(monkeypatch):
    """Crypto's universe is small and stable; Alpaca's crypto asset
    list semantics are different. The flag-on dynamic filtering must
    not apply to crypto."""
    _flag_on(monkeypatch)
    import segments
    expected = list(segments.SEGMENTS["crypto"]["universe"])
    # Pretend get_active_alpaca_symbols returns nothing relevant —
    # we must still get the full crypto list.
    with patch("screener.get_active_alpaca_symbols", return_value=set()):
        result = segments.get_live_universe("crypto")
    assert result == expected


def test_unknown_segment_raises():
    import segments
    with pytest.raises(KeyError):
        segments.get_live_universe("nonexistent_segment")
