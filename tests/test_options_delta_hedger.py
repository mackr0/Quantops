"""Phase D1 of OPTIONS_PROGRAM_PLAN.md — dynamic delta hedging.

Verifies:
  - Hedgeable filter: only long_call/long_put with side=buy qualify
  - compute_hedge_target groups legs by underlying, computes drift
  - Threshold gate: skip when drift below max(5, 5%)
  - rebalance_hedges submits opposite-side stock orders to neutralize
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_db():
    from journal import init_db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _seed_long_call(db_path, sym="AAPL", qty=1, strike=150,
                      expiry_days_out=30):
    from journal import log_trade
    from datetime import date as _d, timedelta as _td
    expiry = (_d.today() + _td(days=expiry_days_out)).isoformat()
    occ = f"{sym:<6}"[:6] + "990115C" + f"{int(strike*1000):08d}"
    occ = occ[:21]
    log_trade(
        symbol=sym, side="buy", qty=qty, price=2.50,
        signal_type="OPTIONS", strategy="long_call",
        decision_price=2.50, occ_symbol=occ,
        option_strategy="long_call", expiry=expiry, strike=strike,
        db_path=db_path,
    )


def _seed_covered_call(db_path, sym="AAPL", qty=1, strike=160,
                          expiry_days_out=30):
    from journal import log_trade
    from datetime import date as _d, timedelta as _td
    expiry = (_d.today() + _td(days=expiry_days_out)).isoformat()
    occ = f"{sym:<6}"[:6] + "990115C" + f"{int(strike*1000):08d}"
    occ = occ[:21]
    log_trade(
        symbol=sym, side="sell", qty=qty, price=1.50,
        signal_type="OPTIONS", strategy="covered_call",
        decision_price=1.50, occ_symbol=occ,
        option_strategy="covered_call", expiry=expiry, strike=strike,
        db_path=db_path,
    )


class TestIsHedgeableOption:
    def test_long_call_is_hedgeable(self):
        from options_delta_hedger import _is_hedgeable_option
        assert _is_hedgeable_option({
            "option_strategy": "long_call", "side": "buy",
        }) is True

    def test_long_put_is_hedgeable(self):
        from options_delta_hedger import _is_hedgeable_option
        assert _is_hedgeable_option({
            "option_strategy": "long_put", "side": "buy",
        }) is True

    def test_covered_call_not_hedgeable(self):
        """Stock is the hedge already; don't double up."""
        from options_delta_hedger import _is_hedgeable_option
        assert _is_hedgeable_option({
            "option_strategy": "covered_call", "side": "sell",
        }) is False

    def test_protective_put_not_hedgeable(self):
        from options_delta_hedger import _is_hedgeable_option
        assert _is_hedgeable_option({
            "option_strategy": "protective_put", "side": "buy",
        }) is False

    def test_csp_not_hedgeable(self):
        from options_delta_hedger import _is_hedgeable_option
        assert _is_hedgeable_option({
            "option_strategy": "cash_secured_put", "side": "sell",
        }) is False


class TestComputeHedgeTarget:
    def test_no_hedgeable_options_returns_empty(self, tmp_db):
        from options_delta_hedger import compute_hedge_target
        # Only a covered call (excluded)
        _seed_covered_call(tmp_db)
        targets = compute_hedge_target(
            positions=[], db_path=tmp_db,
            price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25,
        )
        assert targets == {}

    def test_long_call_produces_negative_target_stock(self, tmp_db):
        """Long ATM call has positive delta. To neutralize, target
        stock = -options_delta < 0 (short stock). Drift = target -
        current = negative if no current stock."""
        from options_delta_hedger import compute_hedge_target
        _seed_long_call(tmp_db, qty=1, strike=150)
        targets = compute_hedge_target(
            positions=[], db_path=tmp_db,
            price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25,
        )
        assert "AAPL" in targets
        info = targets["AAPL"]
        # ATM call delta ~0.5 * qty=1 * 100 = ~50 share-equivalents
        assert info["options_delta"] > 0
        # Target stock to neutralize = -options_delta < 0
        assert info["target_stock_qty"] < 0
        # No current stock → drift = target_stock - 0 = target
        assert info["drift_shares"] == info["target_stock_qty"]
        # Drift > 5 shares → rebalance needed
        assert info["rebalance_needed"] is True

    def test_already_hedged_no_rebalance(self, tmp_db):
        """Long call (delta ~50) + short stock (-50) → already neutral.
        Drift below threshold → no rebalance."""
        from options_delta_hedger import compute_hedge_target
        _seed_long_call(tmp_db, qty=1, strike=150)
        positions = [{"symbol": "AAPL", "qty": -50}]  # short 50 shares
        targets = compute_hedge_target(
            positions, tmp_db,
            price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25,
        )
        info = targets["AAPL"]
        # Target ~-50, current -50 → drift ~0
        assert abs(info["drift_shares"]) <= info["threshold_shares"]
        assert info["rebalance_needed"] is False


class TestRebalanceHedges:
    def test_submits_short_stock_order_for_long_call(self, tmp_db):
        from options_delta_hedger import rebalance_hedges
        _seed_long_call(tmp_db, qty=1, strike=150)
        api = MagicMock()
        api.submit_order.return_value = MagicMock(id="hedge-1")
        result = rebalance_hedges(
            api, tmp_db, positions=[],
            price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25, log=False,
        )
        assert result["rebalanced"] == 1
        kwargs = api.submit_order.call_args.kwargs
        # Long call → need short stock to neutralize → SELL
        assert kwargs["side"] == "sell"
        assert kwargs["symbol"] == "AAPL"
        # Quantity should be roughly the delta (~50 shares for 1 ATM call)
        assert 30 <= kwargs["qty"] <= 70

    def test_skips_when_within_threshold(self, tmp_db):
        from options_delta_hedger import rebalance_hedges
        _seed_long_call(tmp_db, qty=1, strike=150)
        # Already short ~50 shares — close to perfect neutral
        positions = [{"symbol": "AAPL", "qty": -50}]
        api = MagicMock()
        result = rebalance_hedges(
            api, tmp_db, positions,
            price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25, log=False,
        )
        assert result["rebalanced"] == 0
        api.submit_order.assert_not_called()

    def test_excludes_covered_call_no_double_hedge(self, tmp_db):
        """Covered call is intentionally hedged via stock; the hedger
        must not try to rebalance it."""
        from options_delta_hedger import rebalance_hedges
        _seed_covered_call(tmp_db)
        positions = [{"symbol": "AAPL", "qty": 100}]
        api = MagicMock()
        result = rebalance_hedges(
            api, tmp_db, positions,
            price_lookup=lambda s: 150.0,
            iv_lookup=lambda s: 0.25, log=False,
        )
        assert result["evaluated"] == 0
        api.submit_order.assert_not_called()

    def test_per_underlying_aggregation(self, tmp_db):
        """Two long calls on different symbols rebalance independently."""
        from options_delta_hedger import rebalance_hedges
        _seed_long_call(tmp_db, sym="AAPL", qty=1, strike=150)
        _seed_long_call(tmp_db, sym="MSFT", qty=1, strike=200)
        api = MagicMock()
        api.submit_order.side_effect = [
            MagicMock(id="h1"), MagicMock(id="h2"),
        ]
        result = rebalance_hedges(
            api, tmp_db, positions=[],
            price_lookup=lambda s: 150.0 if s == "AAPL" else 200.0,
            iv_lookup=lambda s: 0.25, log=False,
        )
        assert result["evaluated"] == 2
        assert result["rebalanced"] == 2
