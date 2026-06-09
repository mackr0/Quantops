"""2026-06-09 — ATR-derived stop/TP percentage clamps.

Pre-rewrite: both `stock_strategy_advisor.evaluate_candidate_for_
stock_action` and `trade_pipeline.execute_trade` used the raw
`(ATR × multiplier) / price` formula with no bounds. For low-
priced volatile small-caps the ATR-as-percent-of-price ratio is
huge — pid 42's actual entries reproduced:

  - RGNT $3.36 entry → ATR×3 / 3.36 = +84% TP target. Unreachable;
    median realized MFE was 2.7%. 0/45 closed trades hit their TP
    over a 30-day window.
  - NEXR $1.28 entry → ATR×2 / 1.28 = −63% SL. So wide that a real
    stop-out meant the entire position evaporated.
  - RGNT #128 entry $3.36, recorded stop $3.35 (0.3%). ATR fed in
    as ~0 produced a near-zero stop that fired on noise.

Post-rewrite: `risk_clamps.clamp_tp_pct` and `clamp_sl_pct` clamp
the ATR-derived fraction to:

  - TP: [4%, 12%] — 12% sits just above historical p75 MFE (6.9%)
  - SL: [3%, 7%] — kills both noise stops AND the 50%+ wide ones

Tests pin:

  1. The clamp values themselves at their documented bounds.
  2. Historical reproduction: RGNT 84% raw → 12% clamped.
  3. Historical reproduction: NEXR 63% raw → 7% clamped.
  4. Historical reproduction: RGNT 0.3% raw → 3% clamped.
  5. Non-pathological inputs (AMZN 5.6%/8.4%) pass through untouched.
  6. Both call sites (advisor + pipeline) wire through the clamp.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Layer 1 — clamp values at the documented bounds
# ---------------------------------------------------------------------------


class TestClampBounds:

    def test_tp_min_documented(self):
        from risk_clamps import ATR_TP_PCT_MIN
        assert ATR_TP_PCT_MIN == 0.04, (
            "TP min is anchored to 'doesn't fire immediately on first "
            "uptick' — changing it requires re-analyzing historical "
            "MFE distribution"
        )

    def test_tp_max_documented(self):
        from risk_clamps import ATR_TP_PCT_MAX
        assert ATR_TP_PCT_MAX == 0.12, (
            "TP max is anchored to p75 of historical MFE (6.9%, "
            "2026-06-09 measurement). Increasing it back-doors the "
            "84% unreachable targets bug."
        )

    def test_sl_min_documented(self):
        from risk_clamps import ATR_SL_PCT_MIN
        assert ATR_SL_PCT_MIN == 0.03

    def test_sl_max_documented(self):
        from risk_clamps import ATR_SL_PCT_MAX
        assert ATR_SL_PCT_MAX == 0.07


# ---------------------------------------------------------------------------
# Layer 2 — historical reproductions clamp to expected values
# ---------------------------------------------------------------------------


class TestHistoricalReproductions:

    def test_rgnt_84pct_tp_clamps_to_12pct(self):
        """RGNT entry $3.36, ATR $0.94. ATR×3/price = 0.84 (84%).
        Should clamp to 0.12 (12%)."""
        from risk_clamps import clamp_tp_pct
        atr = 0.94
        price = 3.36
        multiplier = 3.0
        raw = atr * multiplier / price
        assert raw > 0.80, "Test premise: raw RGNT TP is the 84% pathology"
        assert clamp_tp_pct(raw) == 0.12

    def test_nexr_63pct_sl_clamps_to_7pct(self):
        """NEXR entry $1.28, ATR $0.40. ATR×2/price = 0.625 (63%).
        Should clamp to 0.07 (7%)."""
        from risk_clamps import clamp_sl_pct
        atr = 0.40
        price = 1.28
        multiplier = 2.0
        raw = atr * multiplier / price
        assert raw > 0.55, "Test premise: raw NEXR SL is the 63% pathology"
        assert clamp_sl_pct(raw) == 0.07

    def test_rgnt_0pt3pct_stale_atr_stop_clamps_to_3pct(self):
        """RGNT #128 had a 0.3% stop because ATR was effectively
        zero. Clamp must floor at 3% so noise stops are impossible."""
        from risk_clamps import clamp_sl_pct
        # ATR fed as $0.005 on a $3.36 stock → 2x = $0.01, 0.3% of price
        raw = 0.003
        assert clamp_sl_pct(raw) == 0.03

    def test_amzn_normal_values_pass_through(self):
        """AMZN entry $246.56, ATR $6.91. ATR×3/price ≈ 0.084 (8.4%).
        That's inside [4%, 12%] → unchanged. ATR×2/price ≈ 0.056
        (5.6%) inside [3%, 7%] → unchanged. Verifies the clamp
        doesn't squash normal-volatility stocks."""
        from risk_clamps import clamp_tp_pct, clamp_sl_pct
        atr = 6.91
        price = 246.56
        raw_tp = atr * 3.0 / price
        raw_sl = atr * 2.0 / price
        assert abs(clamp_tp_pct(raw_tp) - raw_tp) < 0.001, (
            "AMZN-normal TP 8.4% should pass through unchanged"
        )
        assert abs(clamp_sl_pct(raw_sl) - raw_sl) < 0.001, (
            "AMZN-normal SL 5.6% should pass through unchanged"
        )

    def test_lxeh_39pct_tp_clamps_to_12pct(self):
        """LXEH entry $1.25, ATR $0.16. ATR×3/price = 0.384 (39%).
        Still way too far — clamp."""
        from risk_clamps import clamp_tp_pct
        raw = 0.16 * 3.0 / 1.25
        assert raw > 0.30
        assert clamp_tp_pct(raw) == 0.12


# ---------------------------------------------------------------------------
# Layer 3 — call-site integration
# ---------------------------------------------------------------------------


class TestStockStrategyAdvisorClamps:

    def test_advisor_clamps_high_atr_tp(self):
        """The pre-AI recommendation that goes into the prompt block
        is what the AI sees and copies. Without the clamp at this
        site, the AI proposes the 84% TP."""
        from stock_strategy_advisor import evaluate_candidate_for_stock_action
        candidate = {
            "symbol": "RGNT",
            "signal": "BUY",
            "price": 3.36,
            "atr": 0.94,
            "score": 1.5,
            "rsi": 60, "adx": 25, "volume_ratio": 2.0,
        }
        ctx = SimpleNamespace(
            max_position_pct=0.10,
            atr_multiplier_sl=2.0,
            atr_multiplier_tp=3.0,
        )
        recs = evaluate_candidate_for_stock_action(candidate, ctx=ctx)
        assert len(recs) == 1
        # 12% clamp expressed as percentage (12.0)
        assert recs[0]["take_profit_pct"] == 12.0, (
            f"RGNT TP must clamp to 12% (was raw 84%). Got "
            f"{recs[0]['take_profit_pct']}"
        )
        # 7% SL clamp
        assert recs[0]["stop_loss_pct"] == 7.0, (
            f"RGNT SL must clamp to 7% (was raw 56%). Got "
            f"{recs[0]['stop_loss_pct']}"
        )

    def test_advisor_clamps_near_zero_atr_sl(self):
        """If the screener feeds a near-zero ATR, the SL floor
        prevents 0.3% stops."""
        from stock_strategy_advisor import evaluate_candidate_for_stock_action
        candidate = {
            "symbol": "RGNT",
            "signal": "BUY",
            "price": 3.36,
            "atr": 0.005,  # stale / mis-fed
            "score": 1.5,
            "rsi": 60, "adx": 25, "volume_ratio": 2.0,
        }
        ctx = SimpleNamespace(
            max_position_pct=0.10,
            atr_multiplier_sl=2.0,
            atr_multiplier_tp=3.0,
        )
        recs = evaluate_candidate_for_stock_action(candidate, ctx=ctx)
        # Raw would be 0.3% SL; clamp floors at 3%
        assert recs[0]["stop_loss_pct"] == 3.0
        # Raw would be 0.45% TP; clamp floors at 4%
        assert recs[0]["take_profit_pct"] == 4.0

    def test_advisor_passes_through_amzn_normal(self):
        from stock_strategy_advisor import evaluate_candidate_for_stock_action
        candidate = {
            "symbol": "AMZN",
            "signal": "BUY",
            "price": 246.56,
            "atr": 6.91,
            "score": 1.5,
            "rsi": 55, "adx": 22, "volume_ratio": 1.2,
        }
        ctx = SimpleNamespace(
            max_position_pct=0.10,
            atr_multiplier_sl=2.0,
            atr_multiplier_tp=3.0,
        )
        recs = evaluate_candidate_for_stock_action(candidate, ctx=ctx)
        # ATR×3/price ≈ 8.4% — inside the band, passes through
        assert 8.0 <= recs[0]["take_profit_pct"] <= 9.0
        # ATR×2/price ≈ 5.6% — inside the band
        assert 5.0 <= recs[0]["stop_loss_pct"] <= 6.0


def test_trade_pipeline_imports_risk_clamps():
    """Source-code pin on trade_pipeline.execute_trade: the ATR-
    stops path must import and use risk_clamps. Without this the
    pipeline silently bypasses the clamp and stamps unbounded values
    onto the BUY row's stop_loss / take_profit columns."""
    src = (REPO_ROOT / "trade_pipeline.py").read_text()
    # Find the ATR-stops block in execute_trade
    anchor = src.find("ATR-based stops: calculate volatility-adapted")
    assert anchor > 0, "ATR-stops anchor comment moved or removed"
    window = src[anchor:anchor + 2000]
    assert "from risk_clamps import" in window, (
        "trade_pipeline.execute_trade must import clamp_tp_pct/"
        "clamp_sl_pct from risk_clamps. Without it the pipeline's "
        "ATR formula re-introduces the 84% TPs / 0.3% stops bugs."
    )
    assert "clamp_tp_pct(raw_tp_frac)" in window
    assert "clamp_sl_pct(raw_sl_frac)" in window


def test_stock_advisor_imports_risk_clamps():
    """Source pin for the advisor too."""
    src = (REPO_ROOT / "stock_strategy_advisor.py").read_text()
    anchor = src.find("ATR-based stop / take-profit")
    assert anchor > 0
    window = src[anchor:anchor + 2000]
    assert "from risk_clamps import" in window
    assert "clamp_tp_pct" in window
    assert "clamp_sl_pct" in window
