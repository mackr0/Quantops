"""ai_analyst._build_batch_prompt handles dict-shaped news items.

2026-05-19. After the master-key kill (commit 369a7dc), Alpaca
credentials started flowing properly and `fetch_news_alpaca`
finally returned its real payload — a list of dicts with
{headline, summary, source, created_at}. The prompt builder at
`ai_analyst.py:1857` was written assuming `n` was a string and
did `n[:80]`, which raised `KeyError: slice(None, 80, None)` on
the dict.

This bug had been latent for weeks/months: the env-level master
key had been silently 401-ing news fetches, so `entry["news"]`
was never set, so the slicing code path was dead. The credential
fix exposed it and broke 4 AI-driven profile cycles at 17:45 UTC.

Tests pin the contract:
  - News items can be dicts → render headline up to 80 chars
  - News items can be strings (legacy / hypothetical) → slice
  - Mixed list → handle each item by type
  - Empty / missing news → no News line in the rendered prompt
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _make_candidate(news):
    """Minimum candidate dict for _build_batch_prompt to render news."""
    return {
        "symbol": "AAPL",
        "price": 150.0,
        "signal": "BUY",
        "score": 0.5,
        "rsi": 60,
        "volume_ratio": 1.2,
        "atr": 1.0,
        "adx": 25,
        "stoch_rsi": 50,
        "roc_10": 1.0,
        "pct_from_52w_high": 0.05,
        "mfi": 50,
        "cmf": 0,
        "squeeze": 0,
        "pct_from_vwap": 0,
        "nearest_fib_dist": 99,
        "gap_pct": 0,
        "news": news,
    }


class TestNewsDictHandling:
    """The 2026-05-19 regression class — dict items must not crash."""

    def test_dict_news_items_do_not_raise(self):
        """The exact shape fetch_news_alpaca returns."""
        from ai_analyst import _build_batch_prompt
        news = [
            {"headline": "Apple beats earnings", "summary": "Q3 revenue up",
             "source": "Benzinga", "created_at": "2026-05-19T13:00:00Z"},
            {"headline": "Apple announces new product launch",
             "summary": "iPhone Pro 16", "source": "Reuters",
             "created_at": "2026-05-19T14:00:00Z"},
        ]
        # Must not raise KeyError
        prompt = _build_batch_prompt(
            [_make_candidate(news)],
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=SimpleNamespace(
                ai_provider="google",
                segment="largecap",
                max_position_pct=0.05,
                max_total_positions=10,
                enable_short_selling=False,
                enable_options=True,
            ),
        )
        # The headline should appear in the prompt (truncated to 80 chars)
        assert "Apple beats earnings" in prompt
        assert "Apple announces new product" in prompt

    def test_dict_news_headline_truncated_to_80_chars(self):
        from ai_analyst import _build_batch_prompt
        long_headline = "A" * 100
        news = [{"headline": long_headline, "summary": "",
                 "source": "test", "created_at": ""}]
        prompt = _build_batch_prompt(
            [_make_candidate(news)],
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=SimpleNamespace(
                ai_provider="google",
                segment="largecap",
                max_position_pct=0.05,
                max_total_positions=10,
                enable_short_selling=False,
                enable_options=True,
            ),
        )
        # 80 A's must appear; longer string must NOT (truncation applied)
        assert ("A" * 80) in prompt
        assert ("A" * 81) not in prompt

    def test_string_news_items_still_work_back_compat(self):
        """If any caller (legacy / test fixture) passes strings, the
        renderer must still handle them — defensive fallback."""
        from ai_analyst import _build_batch_prompt
        news = ["Plain string headline 1", "Plain string headline 2"]
        # Must not raise
        prompt = _build_batch_prompt(
            [_make_candidate(news)],
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=SimpleNamespace(
                ai_provider="google",
                segment="largecap",
                max_position_pct=0.05,
                max_total_positions=10,
                enable_short_selling=False,
                enable_options=True,
            ),
        )
        assert "Plain string headline 1" in prompt

    def test_mixed_news_list_handles_each_by_type(self):
        from ai_analyst import _build_batch_prompt
        news = [
            {"headline": "Dict item one"},
            "String item two",
        ]
        prompt = _build_batch_prompt(
            [_make_candidate(news)],
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=SimpleNamespace(
                ai_provider="google",
                segment="largecap",
                max_position_pct=0.05,
                max_total_positions=10,
                enable_short_selling=False,
                enable_options=True,
            ),
        )
        assert "Dict item one" in prompt
        assert "String item two" in prompt

    def test_empty_news_omits_news_line(self):
        from ai_analyst import _build_batch_prompt
        prompt = _build_batch_prompt(
            [_make_candidate([])],
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=SimpleNamespace(
                ai_provider="google",
                segment="largecap",
                max_position_pct=0.05,
                max_total_positions=10,
                enable_short_selling=False,
                enable_options=True,
            ),
        )
        assert "News:" not in prompt

    def test_missing_news_key_omits_news_line(self):
        from ai_analyst import _build_batch_prompt
        cand = _make_candidate(None)
        cand.pop("news")
        prompt = _build_batch_prompt(
            [cand],
            portfolio_state={"positions": [], "drawdown_pct": 0.0,
                              "account": {"equity": 100000}},
            market_context={"regime": "neutral"},
            ctx=SimpleNamespace(
                ai_provider="google",
                segment="largecap",
                max_position_pct=0.05,
                max_total_positions=10,
                enable_short_selling=False,
                enable_options=True,
            ),
        )
        assert "News:" not in prompt
