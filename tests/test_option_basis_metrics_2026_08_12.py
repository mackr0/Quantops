"""2026-08-12 — the -4360% CVaR on /performance (p211).

Per-trade returns divided pnl by price x qty with NO option contract
multiplier, so a 1-lot PM put closed for -$315 against a $5.10 premium
scored -6,176% instead of -61.8% and the worst-5% tail averaged to
-4360%. The 2026-05-12 fix for the identical symptom only filtered
data_quality-corrupted rows; legitimate option closes hit the same
formula the moment real option pairs landed. `_trade_notional` is now
the single denominator source (100x when occ_symbol is set) for
VaR/CVaR, avg win/loss %, and position sizes.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402
from metrics.legacy import _trade_notional  # noqa: E402


class TestTradeNotional:
    def test_stock_unchanged(self):
        assert _trade_notional(
            {"price": 50.0, "qty": 10, "occ_symbol": None}) == 500.0

    def test_option_uses_contract_multiplier(self):
        assert _trade_notional(
            {"price": 5.10, "qty": 1,
             "occ_symbol": "PM260821P00190000"}) == pytest.approx(510.0)

    def test_junk_basis_is_zero(self):
        assert _trade_notional({"price": 0, "qty": 5}) == 0.0
        assert _trade_notional({"price": 2.0, "qty": 0}) == 0.0


class TestOptionReturnsSane:
    def _db(self, tmp_path, rows):
        db = str(tmp_path / "quantopsai_profile_701.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, "
            "timestamp TEXT, symbol TEXT, side TEXT, qty REAL, "
            "price REAL, pnl REAL, strategy TEXT, decision_price REAL, "
            "fill_price REAL, slippage_pct REAL, status TEXT, "
            "occ_symbol TEXT, data_quality TEXT)")
        for r in rows:
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, pnl, status, occ_symbol) "
                "VALUES (?,?,?,?,?,?,?,?)", r)
        conn.commit()
        conn.close()
        return db

    def test_cvar_not_poisoned_by_option_basis(self, tmp_path):
        from metrics.legacy import _gather_trades
        rows = [
            # five modest stock closes to clear MIN_TRADES_FOR_VAR
            ("2026-08-01", "AAA", "sell", 10, 50.0, -25.0, "closed", None),
            ("2026-08-02", "BBB", "sell", 10, 50.0, 15.0, "closed", None),
            ("2026-08-03", "CCC", "sell", 10, 50.0, 10.0, "closed", None),
            ("2026-08-04", "DDD", "sell", 10, 50.0, -10.0, "closed", None),
            ("2026-08-05", "EEE", "sell", 10, 50.0, 5.0, "closed", None),
            # the PM-shape option close: pnl -315 on 1 lot @ 5.10
            ("2026-08-06", "PM", "buy", 1, 5.10, -315.0, "closed",
             "PM260821P00190000"),
        ]
        db = self._db(tmp_path, rows)
        trades = _gather_trades([db])
        pm = [t for t in trades if t.get("occ_symbol")]
        assert pm
        assert _trade_notional(pm[0]) == pytest.approx(510.0)
        ret = pm[0]["pnl"] / _trade_notional(pm[0]) * 100
        assert -70 < ret < -55, (
            f"option return must be -61.8%%, not thousands: {ret}")

    def test_gather_tolerates_legacy_schema_without_occ(self, tmp_path):
        from metrics.legacy import _gather_trades
        db = str(tmp_path / "quantopsai_profile_702.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, "
            "timestamp TEXT, symbol TEXT, side TEXT, qty REAL, "
            "price REAL, pnl REAL, strategy TEXT, decision_price REAL, "
            "fill_price REAL, slippage_pct REAL, status TEXT)")
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "pnl, status) VALUES ('2026-08-01','AAA','sell',10,50.0,"
            "-25.0,'closed')")
        conn.commit()
        conn.close()
        trades = _gather_trades([db])
        assert trades and trades[0].get("occ_symbol") is None


class TestScratchClassificationOptionBasis:
    def test_small_option_pnl_is_scratch_not_loss(self):
        """pnl -$2 on a $510 option basis is -0.39% — a scratch. With
        the bare qty x price basis it read -39% and classified as a
        loss, silently distorting win rates for every option-trading
        profile."""
        from metrics.legacy import _trade_notional
        t = {"pnl": -2.0, "price": 5.10, "qty": 1,
             "occ_symbol": "PM260821P00190000"}
        pnl_pct = t["pnl"] / _trade_notional(t) * 100.0
        assert -0.5 < pnl_pct < 0, pnl_pct


class TestFillTrueNotional20260813:
    def test_fill_price_beats_garbage_decision_price(self):
        """p212 GOOG covers: price column $2.13 (garbage), fill_price
        $328.03 (broker truth). The notional must be fill-true — the
        same expression the cash algebra uses."""
        from metrics.legacy import _trade_notional
        t = {"price": 2.13, "fill_price": 328.03, "qty": 5,
             "occ_symbol": None}
        assert _trade_notional(t) == pytest.approx(1640.15)

    def test_price_fallback_when_no_fill(self):
        from metrics.legacy import _trade_notional
        t = {"price": 50.0, "fill_price": None, "qty": 2,
             "occ_symbol": None}
        assert _trade_notional(t) == pytest.approx(100.0)


class TestEpisodeReturns20260813:
    def test_spread_legs_group_into_one_episode(self):
        """A GOOG bull call spread's legs scored +443% and -602%
        individually — the episode nets to one modest return over the
        legs' combined capital at risk."""
        from metrics.legacy import _episode_returns
        legs = [
            {"_db": "a.db", "symbol": "GOOG", "pnl": 2460.0,
             "price": 1.85, "fill_price": 1.85, "qty": 3,
             "occ_symbol": "GOOG260821C00200000",
             "option_strategy": "bull_call_spread",
             "expiry": "2026-08-21", "spread_max_loss": 456.0,
             "timestamp": "2026-07-22T15:00:00"},
            {"_db": "a.db", "symbol": "GOOG", "pnl": -2148.0,
             "price": 1.19, "fill_price": 1.19, "qty": 3,
             "occ_symbol": "GOOG260821C00210000",
             "option_strategy": "bull_call_spread",
             "expiry": "2026-08-21", "spread_max_loss": 456.0,
             "timestamp": "2026-07-22T15:00:01"},
        ]
        rets = _episode_returns(legs)
        assert len(rets) == 1
        assert rets[0] == pytest.approx((2460 - 2148) / 912 * 100, rel=1e-3)

    def test_stock_rows_stay_per_trade(self):
        from metrics.legacy import _episode_returns
        rows = [
            {"_db": "a.db", "symbol": "AAA", "pnl": 10.0,
             "price": 50.0, "fill_price": 50.0, "qty": 10,
             "occ_symbol": None, "timestamp": "2026-08-01"},
            {"_db": "a.db", "symbol": "BBB", "pnl": -20.0,
             "price": 50.0, "fill_price": 50.0, "qty": 10,
             "occ_symbol": None, "timestamp": "2026-08-01"},
        ]
        assert len(_episode_returns(rows)) == 2

    def test_lottery_single_leg_keeps_its_real_return(self):
        """A $0.04 call that made $350 is a genuine 17.5x — truth
        stays; only fabricated extremes die."""
        from metrics.legacy import _episode_returns
        rows = [{"_db": "a.db", "symbol": "FCX", "pnl": 350.0,
                 "price": 0.04, "fill_price": 0.04, "qty": 5,
                 "occ_symbol": "FCX260724C00070000",
                 "option_strategy": None, "expiry": "2026-07-24",
                 "spread_max_loss": None,
                 "timestamp": "2026-07-24T15:00:00"}]
        rets = _episode_returns(rows)
        assert rets[0] == pytest.approx(1750.0, rel=1e-3)

    def test_different_profiles_never_group(self):
        from metrics.legacy import _episode_returns
        leg = {"symbol": "GOOG", "pnl": 100.0, "price": 1.0,
               "fill_price": 1.0, "qty": 1,
               "occ_symbol": "GOOG260821C00200000",
               "option_strategy": "bull_call_spread",
               "expiry": "2026-08-21", "spread_max_loss": None,
               "timestamp": "2026-07-22T15:00:00"}
        a = dict(leg, _db="a.db")
        b = dict(leg, _db="b.db")
        assert len(_episode_returns([a, b])) == 2
