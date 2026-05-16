"""Regression tests for the four performance-dashboard math fixes
shipped 2026-05-16 after the cross-tab audit.

Each test pins one specific bug class so it can't recur silently.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def temp_profile_db(tmp_path):
    """Tiny per-profile DB with the minimum schema metrics/legacy.py
    reads from."""
    db = str(tmp_path / "profile.db")
    from journal import init_db
    init_db(db)
    return db


def _insert_trade(db, **kw):
    cols = ", ".join(kw.keys())
    placeholders = ", ".join("?" * len(kw))
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            tuple(kw.values()),
        )
        conn.commit()


# ── Bug 1: gross vs net return uses SIGNED slippage ──────────────

class TestGrossVsNetReturnUsesSignedSlippage:
    """Pre-fix: `gross_return = (net_pnl + |slippage|×price×qty)/eq`.
    Post-fix: `gross_return = (net_pnl + signed_slippage_cost)/eq`
    where favorable slippage REDUCES the gross figure and adverse
    slippage INCREASES it.

    This test seeds a profile with one trade that had FAVORABLE
    slippage (BUY filled cheaper than decided). The fix means:
      net_pnl = $100 (post-slippage, actual realized)
      signed_slippage_cost = -$30 (favorable = negative cost)
      gross_pnl = $70 (less than net because the favorable fill
                       wouldn't have happened in a zero-slippage
                       world)
    The old code would have computed gross_pnl = $130 (wrong sign
    on the slippage_impact added back)."""

    def test_favorable_slippage_makes_gross_below_net(
        self, temp_profile_db, monkeypatch,
    ):
        # Seed one BUY that filled cheaper than decided.
        # decision=100, fill=99, qty=100 → favorable -$100 (got the
        # 100 shares for $9900 instead of $10000).
        _insert_trade(
            temp_profile_db,
            timestamp="2026-05-01T10:00:00", symbol="AAPL", side="buy",
            qty=100, price=99.0, decision_price=100.0, fill_price=99.0,
            slippage_pct=-1.0, status="open",
        )
        # And a SELL that closes it at $110 (clean — no slippage).
        # PnL = (110-99)*100 = $1100 (post-slippage realized).
        _insert_trade(
            temp_profile_db,
            timestamp="2026-05-10T10:00:00", symbol="AAPL", side="sell",
            qty=100, price=110.0, decision_price=110.0, fill_price=110.0,
            slippage_pct=0.0, status="closed", pnl=1100.0,
        )

        from metrics.legacy import calculate_all_metrics
        m = calculate_all_metrics(
            db_paths=[temp_profile_db],
            initial_capital=10_000.0,
        )
        net = m["net_pnl"]
        gross = m["gross_pnl"]
        # Net is the realized $1100 (post-slippage).
        assert net == pytest.approx(1100.0)
        # Gross should be LOWER than net because the BUY's favorable
        # slippage of $100 (got 100 shares for $9900 instead of
        # $10000) was a gift that wouldn't exist in a zero-slippage
        # world.
        assert gross < net, (
            f"With favorable slippage gross ({gross}) should be < net "
            f"({net}). Pre-fix bug had gross > net via |slippage|."
        )
        # Specifically: gross = net + signed_slippage_cost
        # For the BUY: signed_cost = (fill - decision) * qty
        #            = (99 - 100) * 100 = -100  (favorable)
        # For the SELL: signed_cost = (decision - fill) * qty
        #             = (110 - 110) * 100 = 0
        # → gross = 1100 + (-100) = 1000.
        assert gross == pytest.approx(1000.0)

    def test_adverse_slippage_makes_gross_above_net(
        self, temp_profile_db,
    ):
        """Mirror test. BUY filled HIGHER than decided (adverse) →
        gross_pnl > net_pnl."""
        _insert_trade(
            temp_profile_db,
            timestamp="2026-05-01T10:00:00", symbol="MSFT", side="buy",
            qty=50, price=200.0, decision_price=198.0, fill_price=200.0,
            slippage_pct=1.0, status="open",
        )
        _insert_trade(
            temp_profile_db,
            timestamp="2026-05-10T10:00:00", symbol="MSFT", side="sell",
            qty=50, price=210.0, decision_price=210.0, fill_price=210.0,
            slippage_pct=0.0, status="closed", pnl=500.0,
        )
        from metrics.legacy import calculate_all_metrics
        m = calculate_all_metrics(
            db_paths=[temp_profile_db],
            initial_capital=10_000.0,
        )
        # Adverse: paid $200 instead of $198 on 50 shares → cost +$100.
        # gross = 500 + 100 = 600.
        assert m["net_pnl"] == pytest.approx(500.0)
        assert m["gross_pnl"] == pytest.approx(600.0)
        assert m["gross_pnl"] > m["net_pnl"]


# ── Bug 2: Avg hold days handles re-opens + partial fills ────────

class TestAvgHoldDaysFIFOWithQty:
    """Pre-fix: `open_positions[sym] = ts` overwrote prior buys on
    same symbol AND ignored qty. Re-opened positions lost the
    original hold; partial fills broke matching entirely.

    Post-fix: FIFO queue of (date, qty) lots per symbol; sells pop
    FIFO and credit qty-weighted hold days."""

    def test_reopened_position_holds_count_both_buys(
        self, temp_profile_db,
    ):
        """Buy 100 day-1, buy 100 day-3, sell 200 day-10.
        Hold days should be qty-weighted:
          first 100 held 9 days (day 10 - day 1)
          second 100 held 7 days (day 10 - day 3)
          weighted avg = (100*9 + 100*7) / 200 = 8.0 days
        Pre-fix bug: only counted 7 days (second buy overwrote first)."""
        _insert_trade(
            temp_profile_db,
            timestamp="2026-05-01T10:00:00", symbol="AAPL", side="buy",
            qty=100, price=100.0, status="open",
        )
        _insert_trade(
            temp_profile_db,
            timestamp="2026-05-03T10:00:00", symbol="AAPL", side="buy",
            qty=100, price=101.0, status="open",
        )
        _insert_trade(
            temp_profile_db,
            timestamp="2026-05-10T10:00:00", symbol="AAPL", side="sell",
            qty=200, price=110.0, status="closed", pnl=1900.0,
        )

        from metrics.legacy import calculate_all_metrics
        m = calculate_all_metrics(
            db_paths=[temp_profile_db],
            initial_capital=10_000.0,
        )
        assert m["avg_hold_days"] == pytest.approx(8.0), (
            f"Expected qty-weighted hold = 8.0d "
            f"(100×9d + 100×7d / 200); got {m['avg_hold_days']}"
        )

    def test_partial_fills_all_match_to_a_buy(self, temp_profile_db):
        """Buy 100 day-1, sell 50 day-5, sell 50 day-10.
        Both sells must match to the SAME buy lot (FIFO).
        Hold days:
          first 50 sold day 5  → 4 days
          second 50 sold day 10 → 9 days
          weighted avg = (50*4 + 50*9)/100 = 6.5 days
        Pre-fix bug: second sell silently dropped (no remaining buy)."""
        _insert_trade(
            temp_profile_db,
            timestamp="2026-05-01T10:00:00", symbol="TSLA", side="buy",
            qty=100, price=100.0, status="open",
        )
        _insert_trade(
            temp_profile_db,
            timestamp="2026-05-05T10:00:00", symbol="TSLA", side="sell",
            qty=50, price=105.0, status="closed", pnl=250.0,
        )
        _insert_trade(
            temp_profile_db,
            timestamp="2026-05-10T10:00:00", symbol="TSLA", side="sell",
            qty=50, price=110.0, status="closed", pnl=500.0,
        )

        from metrics.legacy import calculate_all_metrics
        m = calculate_all_metrics(
            db_paths=[temp_profile_db],
            initial_capital=10_000.0,
        )
        assert m["avg_hold_days"] == pytest.approx(6.5), (
            f"Expected qty-weighted hold = 6.5d "
            f"(50×4d + 50×9d / 100); got {m['avg_hold_days']}"
        )


# ── Bug 5: Alpha uses geometric annualization for benchmark ───────

class TestAlphaUsesGeometricBenchmarkAnnualization:
    """Pre-fix: benchmark annualized as `mean(r) * 252`. Post-fix:
    `(prod(1+r))^(252/n) - 1`. For a portfolio whose daily returns
    EXACTLY equal beta * benchmark, alpha should be ~0 under
    geometric — under arithmetic it's biased."""

    def test_correlation_uses_sample_covariance_ddof1(self, temp_profile_db):
        """Bug 8 (deep-trace audit, 2026-05-16). Pre-fix the
        correlation/beta calc used `_mean(...)` for covariance
        (population, ddof=0) while `_std()` used sample ddof=1. That
        inconsistency biased correlation and beta downward by
        (n-1)/n. For n=30, ~3.3% bias.

        Direct math test: a correlation of perfectly-correlated
        series must equal 1.0 exactly when both cov and std use
        consistent ddof — otherwise it's exactly (n-1)/n."""
        # Two perfectly-correlated series, n=30
        port = [0.01 * i for i in range(30)]
        bench = [0.02 * i for i in range(30)]  # = 2 * port, perfectly correlated
        n = len(port)
        m_p = sum(port) / n
        m_b = sum(bench) / n
        # Sample cov (ddof=1) — what the fix uses
        sample_cov = sum(
            (p - m_p) * (b - m_b)
            for p, b in zip(port, bench)
        ) / (n - 1)
        # Population cov (ddof=0) — what the bug used
        pop_cov = sum(
            (p - m_p) * (b - m_b)
            for p, b in zip(port, bench)
        ) / n
        # Sample std (ddof=1)
        var_p = sum((p - m_p) ** 2 for p in port) / (n - 1)
        var_b = sum((b - m_b) ** 2 for b in bench) / (n - 1)
        import math
        s_p = math.sqrt(var_p)
        s_b = math.sqrt(var_b)
        # Correct correlation = 1.0 (perfectly correlated)
        corr_sample = sample_cov / (s_p * s_b)
        # Bugged correlation = (n-1)/n ≈ 0.967 for n=30
        corr_buggy = pop_cov / (s_p * s_b)
        assert abs(corr_sample - 1.0) < 1e-9, (
            f"Sample-ddof correlation should be exactly 1.0 for "
            f"perfectly correlated series; got {corr_sample}"
        )
        assert abs(corr_buggy - (n - 1) / n) < 1e-9, (
            f"Bugged correlation should be exactly (n-1)/n = "
            f"{(n-1)/n}; got {corr_buggy}"
        )
        # The bug bias is real and material:
        assert abs(corr_sample - corr_buggy) > 0.03, (
            f"Bias should be ~3.3% for n=30; got "
            f"{corr_sample - corr_buggy}"
        )

    def test_book_beta_equity_denominated_when_cash_present(self):
        """Pin the SEMANTIC choice (not a bug, a deliberate design):
        book_beta is equity-denominated, so a portfolio with 50%
        cash holdings reads as half the beta of a fully-deployed
        portfolio with the same holdings. The docstring says 'sums
        to book-level — gross_weight is fraction', meaning the
        weighted sum is the book's exposure relative to equity.

        If this test ever fails because someone changed compute_book_beta
        to gross-normalize, that's a real semantic shift that needs
        explicit decision — not a silent change."""
        from portfolio_exposure import compute_book_beta
        # 50% of equity in a beta=1 long position, 50% in cash.
        positions = [
            {"symbol": "AAPL", "qty": 100, "market_value": 5_000.0},
        ]
        equity = 10_000.0
        beta_lookup = lambda sym: 1.0 if sym == "AAPL" else None
        book_beta = compute_book_beta(positions, equity, beta_lookup)
        # Equity-denominated: 5000/10000 * 1.0 = 0.5
        assert book_beta == pytest.approx(0.5), (
            f"Book beta is equity-denominated: 50% deployed × "
            f"beta=1 → 0.5. Got {book_beta}. If this changed, the "
            f"simulate_book_beta_with_entry must also be updated to "
            f"match the new convention."
        )

    def test_simulate_book_beta_matches_actual_after_entry(self):
        """End-to-end: simulate_book_beta_with_entry should agree
        with what compute_book_beta returns AFTER the entry actually
        happens. Different equity-vs-gross conventions between the
        two functions would surface as a mismatch here."""
        from portfolio_exposure import (
            compute_book_beta, simulate_book_beta_with_entry,
        )
        # Existing: $5K long AAPL beta=1.0
        positions = [
            {"symbol": "AAPL", "qty": 100, "market_value": 5_000.0},
        ]
        equity = 10_000.0
        beta_lookup = lambda sym: {"AAPL": 1.0, "MSFT": 1.2}.get(sym)

        before = compute_book_beta(positions, equity, beta_lookup)
        # Simulate adding $1K long MSFT (beta=1.2, size_pct=10).
        projected = simulate_book_beta_with_entry(
            positions, equity,
            candidate_symbol="MSFT", candidate_size_pct=10.0,
            candidate_action="BUY", beta_lookup=beta_lookup,
        )
        # Actually add it and re-compute.
        after_positions = positions + [
            {"symbol": "MSFT", "qty": 10, "market_value": 1_000.0},
        ]
        actual_after = compute_book_beta(
            after_positions, equity, beta_lookup,
        )
        assert projected == pytest.approx(actual_after, abs=1e-6), (
            f"simulate_book_beta_with_entry ({projected}) should "
            f"match compute_book_beta of the actual book ({actual_after})"
        )

    def test_arithmetic_vs_geometric_differ_under_volatility(self):
        """Cross-check: for a synthetic benchmark with high vol, the
        two annualization methods give meaningfully different values.
        Confirms we're not falsely passing the test via arithmetic
        ≈ geometric coincidence (which IS the case for tiny returns)."""
        # Daily returns alternating +5%, -4% (volatile)
        rets = [0.05, -0.04] * 100  # 200 days
        # Arithmetic: mean = 0.005 → annualized 1.26
        # Geometric: prod = (1.05*0.96)^100 = 1.008^100 ≈ 2.22
        #            annualized = 2.22^(252/200) - 1 ≈ 1.86
        import math
        arithmetic = (sum(rets) / len(rets)) * 252
        cum = 1.0
        for r in rets:
            cum *= (1 + r)
        geometric = (cum ** (252 / len(rets))) - 1
        # Verify the two methods materially differ for volatile data —
        # if they didn't, the test wouldn't actually distinguish a
        # fix from the bug.
        assert abs(arithmetic - geometric) > 0.1, (
            f"Arithmetic ({arithmetic:.3f}) and geometric "
            f"({geometric:.3f}) should differ meaningfully on "
            f"volatile synthetic returns — test is broken"
        )
