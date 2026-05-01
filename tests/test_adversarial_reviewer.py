"""Tests for the adversarial_reviewer specialist (Item 5b).

The adversarial reviewer is a 5th specialist with VETO authority.
Different framing from risk_assessor — hunts for failure modes
("what would have to be true for this to lose money fast?") rather
than risk factors ("what risks exist?"). Tests verify:

  - Module exposes the standard specialist contract
  - HAS_VETO_AUTHORITY is True
  - build_prompt includes portfolio + regime context
  - parse_response correctly handles BUY/SELL/HOLD/VETO verdicts
  - Registered in SPECIALIST_MODULES
  - Wired with VETO authority in ensemble.VETO_AUTHORIZED
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestModuleContract:
    def test_module_imports(self):
        from specialists import adversarial_reviewer
        assert adversarial_reviewer.NAME == "adversarial_reviewer"
        assert isinstance(adversarial_reviewer.DESCRIPTION, str)
        assert adversarial_reviewer.HAS_VETO_AUTHORITY is True

    def test_build_prompt_callable(self):
        from specialists import adversarial_reviewer
        assert callable(adversarial_reviewer.build_prompt)
        assert callable(adversarial_reviewer.parse_response)


class TestBuildPrompt:
    def test_prompt_includes_regime(self):
        from specialists import adversarial_reviewer
        ctx = MagicMock()
        ctx.market_regime = "DEFENSIVE"
        ctx.target_short_pct = 0.5
        ctx.target_book_beta = 0
        with patch("specialists.adversarial_reviewer._portfolio_summary",
                   return_value="(no positions)"):
            prompt = adversarial_reviewer.build_prompt(
                [{"symbol": "AAPL", "signal": "BUY", "price": 150,
                  "reason": "breakout"}], ctx,
            )
        assert "DEFENSIVE" in prompt
        assert "target_short_pct=0.5" in prompt
        assert "target_book_beta=0" in prompt

    def test_prompt_includes_failure_mode_framing(self):
        from specialists import adversarial_reviewer
        ctx = MagicMock()
        ctx.market_regime = "neutral"
        with patch("specialists.adversarial_reviewer._portfolio_summary",
                   return_value="(no positions)"):
            prompt = adversarial_reviewer.build_prompt(
                [{"symbol": "AAPL", "signal": "BUY", "price": 150}], ctx,
            )
        # The whole point: framing is failure-mode hunting, not generic risk
        assert "FAILURE MODE" in prompt or "failure mode" in prompt.lower()
        assert "ADVERSARIAL" in prompt or "adversarial" in prompt.lower()
        # The 6-point checklist anchors should be in the prompt
        assert "CORRELATION" in prompt
        assert "CONCENTRATION" in prompt
        assert "REGIME MISMATCH" in prompt
        assert "EARNINGS" in prompt
        assert "CROWDED" in prompt
        assert "FACTOR" in prompt

    def test_prompt_demands_one_entry_per_candidate(self):
        from specialists import adversarial_reviewer
        ctx = MagicMock(market_regime="neutral")
        with patch("specialists.adversarial_reviewer._portfolio_summary",
                   return_value="(no positions)"):
            prompt = adversarial_reviewer.build_prompt(
                [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}], ctx,
            )
        assert "exactly 3 entries" in prompt

    def test_prompt_constrains_veto_discipline(self):
        """The prompt must spell out invalid VETO reasons. Without this
        the specialist over-vetoes on uncertainty."""
        from specialists import adversarial_reviewer
        ctx = MagicMock(market_regime="neutral")
        with patch("specialists.adversarial_reviewer._portfolio_summary",
                   return_value="(no positions)"):
            prompt = adversarial_reviewer.build_prompt(
                [{"symbol": "AAPL"}], ctx,
            )
        # Must explicitly call out invalid reasons
        assert "INVALID VETO" in prompt or "INVALID" in prompt.upper()
        # Must call out the over-vetoing failure mode
        assert "over-vetoing" in prompt or "re-examine" in prompt


class TestParseResponse:
    def test_parses_array_of_verdicts(self):
        from specialists.adversarial_reviewer import parse_response
        raw = """[
            {"symbol": "AAPL", "verdict": "VETO", "confidence": 80,
             "reasoning": "earnings tomorrow"},
            {"symbol": "MSFT", "verdict": "HOLD", "confidence": 50,
             "reasoning": "no specific failure mode"}
        ]"""
        result = parse_response(raw)
        assert len(result) == 2
        assert result[0]["symbol"] == "AAPL"
        assert result[0]["verdict"] == "VETO"
        assert result[1]["verdict"] == "HOLD"

    def test_parses_buy_verdict(self):
        """Adversarial review can support an entry too — when it can't
        find a failure mode, that's signal."""
        from specialists.adversarial_reviewer import parse_response
        raw = '[{"symbol": "AAPL", "verdict": "BUY", "confidence": 70, "reasoning": "robust"}]'
        result = parse_response(raw)
        assert result[0]["verdict"] == "BUY"


class TestPortfolioSummary:
    def test_empty_book_summary(self):
        from specialists.adversarial_reviewer import _portfolio_summary
        ctx = MagicMock()
        with patch("client.get_positions", return_value=[]):
            summary = _portfolio_summary(ctx)
        assert "empty" in summary.lower()

    def test_renders_positions(self):
        from specialists.adversarial_reviewer import _portfolio_summary
        ctx = MagicMock()
        positions = [
            {"symbol": "AAPL", "qty": 100, "market_value": 15000,
             "unrealized_plpc": 0.05},
            {"symbol": "TSLA", "qty": -50, "market_value": -10000,
             "unrealized_plpc": -0.02},
        ]
        with patch("client.get_positions", return_value=positions):
            summary = _portfolio_summary(ctx)
        assert "AAPL" in summary
        assert "LONG" in summary
        assert "TSLA" in summary
        assert "SHORT" in summary

    def test_handles_get_positions_failure(self):
        """Best-effort: if positions can't be read, return empty string,
        don't crash the specialist."""
        from specialists.adversarial_reviewer import _portfolio_summary
        ctx = MagicMock()
        with patch("client.get_positions", side_effect=Exception("oops")):
            summary = _portfolio_summary(ctx)
        # empty book treatment is fine — better than no output
        assert "empty" in summary.lower()


class TestPairBookSummary:
    """The reviewer needs visibility into the active stat-arb pair
    book so it can red-team pair-specific failure modes BEFORE the AI
    proposes a PAIR_TRADE downstream."""

    def _ctx_with_db(self, db_path):
        ctx = MagicMock()
        ctx.db_path = db_path
        return ctx

    @pytest.fixture
    def tmp_db(self):
        import tempfile
        from journal import init_db
        fd, path = tempfile.mkstemp(suffix=".db")
        import os as _os
        _os.close(fd)
        init_db(path)
        yield path
        try:
            _os.unlink(path)
        except OSError:
            pass

    def test_empty_book_returns_empty_string(self, tmp_db):
        from specialists.adversarial_reviewer import _pair_book_summary
        out = _pair_book_summary(self._ctx_with_db(tmp_db))
        assert out == ""

    def test_no_db_path_returns_empty(self):
        from specialists.adversarial_reviewer import _pair_book_summary
        ctx = MagicMock()
        ctx.db_path = None
        assert _pair_book_summary(ctx) == ""

    def test_active_pairs_listed(self, tmp_db):
        from specialists.adversarial_reviewer import _pair_book_summary
        from stat_arb_pair_book import Pair, upsert_pair
        upsert_pair(tmp_db, Pair(
            symbol_a="AAPL", symbol_b="MSFT",
            hedge_ratio=1.0, p_value=0.01,
            half_life_days=5.0, correlation=0.92,
        ))
        out = _pair_book_summary(self._ctx_with_db(tmp_db))
        assert "AAPL/MSFT" in out
        assert "Active stat-arb pairs" in out

    def test_slow_half_life_flagged(self, tmp_db):
        """Reviewer should see a [SLOW] flag on pairs with HL > 20d so
        it can veto PAIR_TRADE proposals that would tie up capital."""
        from specialists.adversarial_reviewer import _pair_book_summary
        from stat_arb_pair_book import Pair, upsert_pair
        upsert_pair(tmp_db, Pair(
            symbol_a="A", symbol_b="B",
            hedge_ratio=1.0, p_value=0.01,
            half_life_days=25.0, correlation=0.92,  # slow
        ))
        out = _pair_book_summary(self._ctx_with_db(tmp_db))
        assert "SLOW" in out

    def test_extreme_hedge_ratio_flagged(self, tmp_db):
        """When hedge ratio is far from 1.0, dollar-neutral sizing
        leaves residual beta — flag it."""
        from specialists.adversarial_reviewer import _pair_book_summary
        from stat_arb_pair_book import Pair, upsert_pair
        upsert_pair(tmp_db, Pair(
            symbol_a="A", symbol_b="B",
            hedge_ratio=2.5, p_value=0.01,
            half_life_days=5.0, correlation=0.92,
        ))
        out = _pair_book_summary(self._ctx_with_db(tmp_db))
        assert "HEDGE FAR FROM 1.0" in out

    def test_prompt_includes_pair_book_when_present(self, tmp_db):
        from specialists.adversarial_reviewer import build_prompt
        from stat_arb_pair_book import Pair, upsert_pair
        upsert_pair(tmp_db, Pair(
            symbol_a="A", symbol_b="B",
            hedge_ratio=1.0, p_value=0.01,
            half_life_days=5.0, correlation=0.92,
        ))
        ctx = self._ctx_with_db(tmp_db)
        ctx.market_regime = "neutral"
        with patch("specialists.adversarial_reviewer._portfolio_summary",
                   return_value="(no positions)"):
            prompt = build_prompt(
                [{"symbol": "X", "signal": "BUY", "price": 100,
                  "reason": "test"}], ctx,
            )
        assert "Active stat-arb pairs" in prompt
        assert "PAIR-BOOK INTERACTION" in prompt

    def test_prompt_omits_pair_book_when_empty(self, tmp_db):
        """No active pairs → no pair-book section in prompt (avoids
        token bloat when there's nothing to red-team)."""
        from specialists.adversarial_reviewer import build_prompt
        ctx = self._ctx_with_db(tmp_db)
        ctx.market_regime = "neutral"
        with patch("specialists.adversarial_reviewer._portfolio_summary",
                   return_value="(no positions)"):
            prompt = build_prompt(
                [{"symbol": "X", "signal": "BUY", "price": 100,
                  "reason": "test"}], ctx,
            )
        assert "Active stat-arb pairs" not in prompt
        # The PAIR-BOOK INTERACTION checklist item still appears (it's
        # static prompt text); only the dynamic pair list is gated.


class TestEnsembleIntegration:
    def test_registered_in_specialist_modules(self):
        from specialists import SPECIALIST_MODULES
        assert "specialists.adversarial_reviewer" in SPECIALIST_MODULES

    def test_discovered_by_discover_specialists(self):
        from specialists import discover_specialists
        names = [s.NAME for s in discover_specialists()]
        assert "adversarial_reviewer" in names

    def test_has_veto_authority_in_ensemble(self):
        from ensemble import VETO_AUTHORIZED
        assert "adversarial_reviewer" in VETO_AUTHORIZED
        assert "risk_assessor" in VETO_AUTHORIZED  # back-compat

    def test_has_weight_in_ensemble(self):
        from ensemble import SPECIALIST_WEIGHTS
        assert "adversarial_reviewer" in SPECIALIST_WEIGHTS
        # Weight should be sane (not zero, not absurd)
        assert 0.5 <= SPECIALIST_WEIGHTS["adversarial_reviewer"] <= 2.0
