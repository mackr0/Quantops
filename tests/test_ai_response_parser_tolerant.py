"""Structural guardrail: the AI batch-response parser must tolerate
common malformations that real LLM outputs ship with — markdown
fences, leading/trailing prose, trailing commas, and truncated
strings from max_tokens cutoffs. Strict json.loads is too brittle.

The bug class (2026-05-14 incident).
After the prompt grew (symmetric stock recs + multileg recs +
per-action notes), `max_tokens=1024` was no longer enough. AI
responses started getting truncated mid-string, producing
`Unterminated string starting at: line N column M` errors across
multiple profiles. The cycle dropped with no trades and no useful
diagnostic.

Two-part fix:
  1. max_tokens bumped 1024 → 4096 (the immediate cap)
  2. Tolerant parser with truncation-salvage so a partial response
     still extracts the trades the AI managed to write before the
     cutoff.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


class TestAiResponseParserTolerant:
    def test_clean_response_parses(self):
        from ai_analyst import _parse_ai_response_tolerant
        raw = '{"trades": [], "portfolio_reasoning": "ok", "pass_this_cycle": false}'
        assert _parse_ai_response_tolerant(raw)["trades"] == []

    def test_strips_markdown_json_fence(self):
        from ai_analyst import _parse_ai_response_tolerant
        raw = '```json\n{"trades": [{"symbol": "AAPL", "action": "BUY"}]}\n```'
        result = _parse_ai_response_tolerant(raw)
        assert result["trades"][0]["symbol"] == "AAPL"

    def test_strips_bare_markdown_fence(self):
        from ai_analyst import _parse_ai_response_tolerant
        raw = '```\n{"trades": []}\n```'
        assert _parse_ai_response_tolerant(raw)["trades"] == []

    def test_strips_leading_prose(self):
        from ai_analyst import _parse_ai_response_tolerant
        raw = 'Here is the analysis: {"trades": [], "pass_this_cycle": true}'
        assert _parse_ai_response_tolerant(raw)["pass_this_cycle"] is True

    def test_strips_trailing_prose(self):
        from ai_analyst import _parse_ai_response_tolerant
        raw = '{"trades": []}\nLet me know if you have questions.'
        assert _parse_ai_response_tolerant(raw)["trades"] == []

    def test_handles_trailing_commas(self):
        from ai_analyst import _parse_ai_response_tolerant
        raw = '{"trades": [{"symbol": "AAPL", "action": "BUY",},], "portfolio_reasoning": "ok",}'
        result = _parse_ai_response_tolerant(raw)
        assert result["trades"][0]["symbol"] == "AAPL"

    def test_salvages_truncated_response(self):
        """When max_tokens cuts off mid-string, the parser should
        return as many complete trades as it can salvage rather than
        dropping the entire cycle."""
        from ai_analyst import _parse_ai_response_tolerant
        # Two complete trades, then a third trade truncated mid-string.
        raw = (
            '{"trades": ['
            '{"symbol": "AAPL", "action": "BUY", "size_pct": 5},'
            '{"symbol": "MSFT", "action": "BUY", "size_pct": 5},'
            '{"symbol": "GOOG", "action": "BUY", "reasoning": "Long thesis bec'
        )
        result = _parse_ai_response_tolerant(raw)
        # Should salvage AAPL + MSFT (the two complete trades).
        # GOOG is truncated mid-string and may or may not be recovered.
        symbols = [t["symbol"] for t in result.get("trades", [])]
        assert "AAPL" in symbols, (
            f"Truncation salvage lost AAPL; got {symbols}"
        )
        assert "MSFT" in symbols, (
            f"Truncation salvage lost MSFT; got {symbols}"
        )

    def test_empty_response_raises(self):
        from ai_analyst import _parse_ai_response_tolerant
        with pytest.raises(json.JSONDecodeError):
            _parse_ai_response_tolerant("")
        with pytest.raises(json.JSONDecodeError):
            _parse_ai_response_tolerant("   \n\n  ")

    def test_unrecoverable_response_raises(self):
        """If the response is so malformed that no salvage works,
        re-raise the original error so the caller treats as cycle
        failure (not silently swallowing as empty trades)."""
        from ai_analyst import _parse_ai_response_tolerant
        with pytest.raises(json.JSONDecodeError):
            _parse_ai_response_tolerant("not valid json at all {")

    def test_max_tokens_is_at_least_4096(self):
        """Pin-test: max_tokens for the batch_select call must be at
        least 4096. Previous 1024 was too low and caused the
        truncation incident on 2026-05-14."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "ai_analyst.py",
        )
        with open(path) as f:
            src = f.read()
        # The batch_select call_ai must use max_tokens >= 4096.
        # We do a narrow regex match around the batch_select purpose.
        import re as _re
        m = _re.search(
            r"max_tokens\s*=\s*(\d+)[\s\S]{0,400}purpose=\"batch_select\"",
            src,
        )
        assert m, (
            "Could not find max_tokens setting near batch_select call. "
            "If the call has been refactored, update this test to "
            "match — but max_tokens for batch_select must remain "
            "≥4096 to prevent truncation."
        )
        n = int(m.group(1))
        assert n >= 4096, (
            f"max_tokens={n} for batch_select is below 4096. "
            f"Truncated AI responses caused the 2026-05-14 "
            f"AI-call-failed incident across multiple profiles."
        )
