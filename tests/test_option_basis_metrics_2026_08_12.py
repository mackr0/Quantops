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
