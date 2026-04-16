"""Tests for `scaling_projection.project_scaling` (2026-04-15).

The model shows what the strategy would look like at each capital level
*if you migrated to the appropriate profile type for that scale*. A
$10K small-cap user shouldn't see fictional "$10M with the same small-
cap universe" projections — at $10M they'd be on a Large Cap profile.
"""

from __future__ import annotations

import math

import pytest


def _trades(n, slippage_pct=0.05, pnl=10.0):
    return [
        {
            "symbol": "TEST",
            "side": "buy",
            "qty": 10,
            "price": 100.0,
            "decision_price": 100.0,
            "fill_price": 100.0 + slippage_pct,
            "slippage_pct": slippage_pct,
            "pnl": pnl,
        }
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Square-root impact within a tier
# ---------------------------------------------------------------------------

class TestSquareRootImpactWithinTier:
    def test_4x_capital_within_same_tier_gives_2x_slippage(self):
        """Within the same tier, slippage scales as sqrt(capital ratio)
        for BOTH execution styles."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small",
                              ladder=[(10_000, "1×"), (40_000, "4×")])
        # Market path
        s_1x_m = out["rows"][0]["slippage_market_pct"]
        s_4x_m = out["rows"][1]["slippage_market_pct"]
        assert 1.9 <= (s_4x_m / s_1x_m) <= 2.1
        # Limit path scales the same way
        s_1x_l = out["rows"][0]["slippage_limit_pct"]
        s_4x_l = out["rows"][1]["slippage_limit_pct"]
        assert 1.9 <= (s_4x_l / s_1x_l) <= 2.1

    def test_current_capital_returns_observed_slippage_market(self):
        """User on market orders — baseline IS the market projection."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.123), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small",
                              use_limit_orders_now=False,
                              ladder=[(10_000, "current")])
        assert abs(out["rows"][0]["slippage_market_pct"] - 0.123) < 1e-9
        # Limit projection at same scale should be ~0.4× lower
        assert out["rows"][0]["slippage_limit_pct"] == pytest.approx(0.123 * 0.4, rel=1e-3)

    def test_current_capital_returns_observed_slippage_limit(self):
        """User on limit orders — baseline IS the limit projection."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.123), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small",
                              use_limit_orders_now=True,
                              ladder=[(10_000, "current")])
        assert abs(out["rows"][0]["slippage_limit_pct"] - 0.123) < 1e-9
        # Market projection at same scale should be ~2.5× higher
        assert out["rows"][0]["slippage_market_pct"] == pytest.approx(0.123 / 0.4, rel=1e-3)


# ---------------------------------------------------------------------------
# Tier migration
# ---------------------------------------------------------------------------

class TestTierMigration:
    def test_small_cap_migrates_to_mid_cap_above_250k(self):
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small",
                              ladder=[(100_000, "$100K"), (1_000_000, "$1M")])
        assert out["rows"][0]["recommended_tier"] == "small"
        assert out["rows"][0]["migrated"] is False
        assert out["rows"][1]["recommended_tier"] == "mid"
        assert out["rows"][1]["migrated"] is True

    def test_migration_offsets_capital_growth_market_path(self):
        """100× capital with profile migration (no execution change):
        slippage growth = sqrt(100/10) ≈ 3.16×."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small",
                              ladder=[(10_000, "1×"), (1_000_000, "100×")])
        baseline = out["rows"][0]["slippage_market_pct"]
        at_1m = out["rows"][1]["slippage_market_pct"]
        ratio = at_1m / baseline
        # sqrt(100 capital / 10 ADV improvement) = sqrt(10) ≈ 3.16
        assert 2.8 <= ratio <= 3.5, (
            f"Migration alone should give ~3.16× growth; got {ratio:.2f}×"
        )

    def test_recommended_tier_label_is_human_readable(self):
        """No 'small' / 'mid' jargon in the UI — must be 'Small Cap' etc."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small")
        labels = {r["recommended_tier_label"] for r in out["rows"]}
        # All labels should be human-readable, not internal codes
        assert "small" not in labels
        assert "mid" not in labels
        assert any("Cap" in lbl for lbl in labels)

    def test_migration_note_uses_human_language_no_internal_doc_refs(self):
        """Notes shown to users must not reference internal docs like
        SCALING_PLAN.md."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small",
                              ladder=[(1_000_000, "$1M")])
        notes = out["rows"][0]["notes"]
        assert notes, "Migration row should have a note"
        joined = " ".join(notes).lower()
        assert ".md" not in joined, "Internal doc reference leaked to UI"
        assert "scaling_plan" not in joined
        # Should mention the new profile + the liquidity benefit
        assert "mid cap" in joined.lower()


# ---------------------------------------------------------------------------
# Crypto stays crypto
# ---------------------------------------------------------------------------

class TestCryptoTier:
    def test_crypto_universe_stays_crypto_at_all_scales(self):
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="crypto",
                              ladder=[(10_000, "1x"), (1_000_000, "100x")])
        for row in out["rows"]:
            assert row["recommended_tier"] == "crypto"
            assert row["migrated"] is False


# ---------------------------------------------------------------------------
# Confidence intervals scale with sample size
# ---------------------------------------------------------------------------

class TestConfidenceIntervals:
    def test_few_trades_wide_ci(self):
        """n<10: ±100% CI on the market path (and limit path)."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(5, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small",
                              ladder=[(10_000, "1x")])
        row = out["rows"][0]
        assert row["slippage_market_pct_low"] == 0.0
        assert row["slippage_market_pct_high"] == pytest.approx(0.10, rel=1e-3)

    def test_many_trades_narrow_ci(self):
        """n>=100: ±10% CI."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(150, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small",
                              ladder=[(10_000, "1x")])
        row = out["rows"][0]
        assert row["slippage_market_pct_low"] == pytest.approx(0.045, rel=1e-3)
        assert row["slippage_market_pct_high"] == pytest.approx(0.055, rel=1e-3)


# ---------------------------------------------------------------------------
# Insufficient data path
# ---------------------------------------------------------------------------

class TestInsufficientData:
    def test_no_fills_returns_insufficient_flag(self):
        from scaling_projection import project_scaling
        trades = [{"symbol": "X", "qty": 10, "price": 100, "pnl": 5}
                  for _ in range(20)]
        out = project_scaling(trades, current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small")
        assert out["data_quality"] == "insufficient"
        assert out["rows"] == []
        assert "message" in out

    def test_empty_trades_returns_insufficient(self):
        from scaling_projection import project_scaling
        out = project_scaling([], current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small")
        assert out["data_quality"] == "insufficient"

    def test_data_quality_modeled_with_small_sample(self):
        from scaling_projection import project_scaling
        out = project_scaling(_trades(10, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small")
        assert out["data_quality"] == "modeled"


# ---------------------------------------------------------------------------
# UI quality guards
# ---------------------------------------------------------------------------

class TestUIQuality:
    def test_no_internal_doc_references_anywhere(self):
        """Sweep every string field of the output for SCALING_PLAN.md."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small")
        def walk(obj):
            if isinstance(obj, str):
                return [obj]
            if isinstance(obj, dict):
                acc = []
                for v in obj.values():
                    acc.extend(walk(v))
                return acc
            if isinstance(obj, (list, tuple)):
                acc = []
                for v in obj:
                    acc.extend(walk(v))
                return acc
            return []
        all_strings = " ".join(walk(out)).lower()
        assert "scaling_plan" not in all_strings, (
            "Internal doc reference (SCALING_PLAN.md) is leaking to UI strings"
        )
        assert ".md" not in all_strings

    def test_market_type_aliases_normalize(self):
        from scaling_projection import project_scaling
        out_a = project_scaling(_trades(50, 0.05), 10_000, 10.0, "small")
        out_b = project_scaling(_trades(50, 0.05), 10_000, 10.0, "smallcap")
        assert out_a["market_type"] == out_b["market_type"] == "small"


# ---------------------------------------------------------------------------
# Regression: old broken model wouldn't pass these
# ---------------------------------------------------------------------------

class TestBothExecutionStylesAlwaysShown:
    def test_every_row_has_both_market_and_limit_columns(self):
        """User wanted to see both side-by-side, not have the system pick."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small")
        for row in out["rows"]:
            for key in ("slippage_market_pct", "slippage_limit_pct",
                        "return_market_pct", "return_limit_pct"):
                assert key in row, f"Missing {key} in row {row['label']}"

    def test_limit_slippage_always_lower_than_market(self):
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small")
        for row in out["rows"]:
            assert row["slippage_limit_pct"] < row["slippage_market_pct"], (
                f"At {row['label']}: limit {row['slippage_limit_pct']:.4f}% "
                f"should be less than market {row['slippage_market_pct']:.4f}%"
            )

    def test_limit_slippage_is_about_40pct_of_market(self):
        """The 0.40× ratio (60% reduction) is preserved through scaling."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), current_capital=10_000,
                              base_net_return_pct=10.0, market_type="small")
        for row in out["rows"]:
            ratio = row["slippage_limit_pct"] / row["slippage_market_pct"]
            assert 0.39 <= ratio <= 0.41

    def test_baseline_calibration_from_limit_user(self):
        """User on limits → baseline IS limit, market is implied higher."""
        from scaling_projection import project_scaling
        out = project_scaling(_trades(50, 0.05), 10_000, 10.0,
                              "small", use_limit_orders_now=True,
                              ladder=[(10_000, "current")])
        # At current capital, the LIMIT path should equal the observed baseline
        assert out["rows"][0]["slippage_limit_pct"] == pytest.approx(0.05, rel=1e-3)
        # And market should be ~2.5× higher (back-implied)
        assert out["rows"][0]["slippage_market_pct"] == pytest.approx(0.125, rel=1e-3)


class TestRegressionGuard:
    def test_1m_market_after_migration_below_naive_pure_sqrt(self):
        """At $1M with migration to mid (10× more liquid), market-order
        slippage = base × sqrt(100/10) = 0.158%. Without migration it
        would be 0.5% (sqrt(100))."""
        from scaling_projection import project_scaling
        with_mig = project_scaling(_trades(50, 0.05), current_capital=10_000,
                                    base_net_return_pct=10.0, market_type="small",
                                    ladder=[(1_000_000, "$1M")])
        slip_market = with_mig["rows"][0]["slippage_market_pct"]
        assert slip_market < 0.20, (
            f"Migration should pull $1M market slippage well under 0.20%; got {slip_market:.3f}"
        )
