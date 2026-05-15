"""Structural guardrail: the catastrophic-single-trade gate's
rolling baseline must compare stock trades to other stock trades,
not to option premiums or polluted rows.

The bug class (2026-05-15 incident).
The single-trade gate (`single_trade_gate.is_catastrophic`) caps a
proposed trade at 5× the rolling-average $ value of recent trades.
If the rolling baseline pulls in option legs (per-leg premiums of
$1-$3) the average drags down to options-dollars and any normal
$5-10k stock BUY looks "5× recent average". On 2026-05-15 pid 11
had 3 stock BUYs (JPM, NOC, DHR) all blocked for this exact
reason — the baseline was poisoned by SBUX/LRCX/ANET/WMT multileg
legs from earlier the same day.

Per Mack's "real-world" rule the guard itself stays — a stock
trade truly 5× larger than typical IS a catastrophic-shape
candidate. The fix is to make the baseline ACCURATE (compare
stocks to stocks) rather than paper over the symptom.

This test pins the exclusion contract:
  - Option-leg rows (occ_symbol set OR signal_type in MULTILEG/
    OPTIONS) are excluded from the baseline
  - data_quality='polluted' rows are excluded
  - Cross-profile reconcile-adjustment rows are excluded
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_db(tmp_path):
    db = str(tmp_path / "quantopsai_profile_test.db")
    from journal import init_db
    init_db(db)
    return db


def _add_trade(db, symbol, qty, price, signal_type=None,
               occ_symbol=None, data_quality=None):
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO trades "
            "(timestamp, symbol, side, qty, price, signal_type, "
            " occ_symbol, data_quality, status) "
            "VALUES (datetime('now'), ?, 'buy', ?, ?, ?, ?, ?, 'open')",
            (symbol, qty, price, signal_type, occ_symbol, data_quality),
        )
        conn.commit()


class TestCatastrophicTradeBaselineExcludesOptions:
    def test_option_legs_excluded_from_baseline(self, tmp_path):
        """Adding cheap option legs alongside normal stock trades
        must NOT drag the rolling average down."""
        from single_trade_gate import recent_avg_position_value
        db = _make_db(tmp_path)
        # 5 stock trades around $5,000 each → avg = $5,000
        for i in range(5):
            _add_trade(db, "AAPL", 25, 200.0, signal_type="BUY")
        baseline_stocks_only = recent_avg_position_value(db)
        assert baseline_stocks_only is not None
        assert 4500 < baseline_stocks_only < 5500, (
            f"Expected baseline ~$5000 from 5 stock trades; got "
            f"${baseline_stocks_only:.2f}"
        )
        # Add 20 option legs at $1.50 premium × 1 contract = $1.50
        for i in range(20):
            _add_trade(
                db, "AAPL", 1, 1.50,
                signal_type="MULTILEG",
                occ_symbol="AAPL260618C00200000",
            )
        baseline_with_options = recent_avg_position_value(db)
        # Without the exclusion, baseline would be roughly
        # (5×$5000 + 20×$1.50) / 25 = $1000.12 — a HUGE drop.
        # With the exclusion, baseline stays near $5000 (option
        # legs dropped from the rolling sample).
        assert baseline_with_options is not None
        assert 4500 < baseline_with_options < 5500, (
            f"Option legs leaked into the baseline; got "
            f"${baseline_with_options:.2f} (expected ~$5000)"
        )

    def test_polluted_rows_excluded_from_baseline(self, tmp_path):
        """data_quality='polluted' rows (e.g. the 2026-05-11 phantom
        SELLs at $0.16 prices) must NOT contaminate the baseline."""
        from single_trade_gate import recent_avg_position_value
        db = _make_db(tmp_path)
        for i in range(5):
            _add_trade(db, "AAPL", 25, 200.0, signal_type="BUY")
        # Add 20 polluted rows at $0.16 (the 2026-05-11 incident shape)
        for i in range(20):
            _add_trade(
                db, "KO", 2, 0.16, signal_type="SELL",
                data_quality="polluted",
            )
        baseline = recent_avg_position_value(db)
        assert baseline is not None
        assert 4500 < baseline < 5500, (
            f"Polluted rows leaked into baseline; got ${baseline:.2f}"
        )

    def test_reconcile_xprof_rows_excluded(self, tmp_path):
        """The synthetic cross-profile reconcile adjustments (added
        2026-05-15 to handle shared-account drift) must not be
        treated as real trades for the baseline calculation."""
        from single_trade_gate import recent_avg_position_value
        db = _make_db(tmp_path)
        for i in range(5):
            _add_trade(db, "AAPL", 25, 200.0, signal_type="BUY")
        _add_trade(
            db, "KO", 26, 78.61,
            signal_type="reconcile_xprof",
            data_quality="reconcile_adjustment",
        )
        baseline = recent_avg_position_value(db)
        assert baseline is not None
        assert 4500 < baseline < 5500, (
            f"Reconcile rows leaked into baseline; got ${baseline:.2f}"
        )

    def test_baseline_returns_none_when_too_few_real_trades(self, tmp_path):
        """If the entire recent history is options/polluted/reconcile
        and there are <5 real stock trades, the baseline returns None
        and the guard falls open (no baseline → no catastrophic-block).
        That's the correct behavior — better to allow a trade through
        than to block on a meaningless baseline."""
        from single_trade_gate import recent_avg_position_value, is_catastrophic
        db = _make_db(tmp_path)
        # 30 multileg legs and 0 stock trades
        for i in range(30):
            _add_trade(
                db, "AAPL", 1, 1.50,
                signal_type="MULTILEG",
                occ_symbol="AAPL260618C00200000",
            )
        baseline = recent_avg_position_value(db)
        assert baseline is None, (
            f"Expected None baseline (insufficient stock trades); "
            f"got {baseline}"
        )
        # And is_catastrophic should fall open with no baseline
        cat, reason, detail = is_catastrophic(10000.0, db)
        assert cat is False
        assert "no baseline" in reason

    def test_realistic_jpm_block_does_not_recur(self, tmp_path):
        """Reproduces the 2026-05-15 incident shape: 5 stock trades
        averaging ~$3000, plus a flood of multileg legs averaging
        $1-3 each. JPM proposed at $6,953 should NOT be blocked
        after the fix (it's 2.3× stock-only baseline of $3000, well
        under the 5× cap)."""
        from single_trade_gate import is_catastrophic
        db = _make_db(tmp_path)
        # Normal stock trades from earlier today
        for sym, qty, price in [
            ("UNH", 3, 395.025),    # $1185
            ("CVS", 4, 97.445),     # $390
            ("ABT", 35, 85.99),     # $3010
            ("TSLA", 2, 428.87),    # $858
            ("LMT", 6, 480.0),      # $2880
        ]:
            _add_trade(db, sym, qty, price, signal_type="BUY")
        # Pre-fix bug: 30 multileg legs poisoning the baseline
        for i in range(30):
            _add_trade(
                db, "SBUX", 1, 2.16,
                signal_type="MULTILEG",
                occ_symbol="SBUX260618C00100000",
            )
        # Now propose JPM at $6,953 (the actual blocked trade)
        cat, reason, detail = is_catastrophic(6953.0, db)
        assert cat is False, (
            f"JPM at $6953 should NOT be flagged catastrophic when "
            f"the baseline correctly excludes options. The pre-fix "
            f"behavior was to block this trade. Got: {reason} "
            f"(detail: {detail})"
        )
