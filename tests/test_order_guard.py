"""Tests for `order_guard.check_can_submit` (2026-04-15).

Every order submission must pass through this guard. The bug:
a scan cycle starts at 3:50 PM ET (within market_hours), but
the pipeline takes 80+ minutes and the actual order submission
lands at 5:10 PM ET — after hours. Alpaca paper trading fills
it, producing an accidental after-hours trade.

The guard checks `is_within_schedule` at order time (not cycle
start time) and blocks if outside the window.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_ET = ZoneInfo("America/New_York")


def _ctx(schedule_type="market_hours"):
    from user_context import UserContext
    return UserContext(
        user_id=1, segment="stocks", display_name="Test",
        alpaca_api_key="k", alpaca_secret_key="s",
        ai_provider="anthropic", ai_model="claude-haiku-4-5-20251001",
        ai_api_key="k", db_path=":memory:",
        schedule_type=schedule_type,
    )


class TestMarketHoursProfile:
    def test_allows_order_during_market_hours(self):
        from order_guard import check_can_submit
        # Wednesday 10:30 AM ET
        fake_now = datetime(2026, 4, 15, 10, 30, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "AAPL", "buy") is True

    def test_blocks_order_after_market_close(self):
        from order_guard import check_can_submit
        # Wednesday 5:10 PM ET — the ALM bug
        fake_now = datetime(2026, 4, 15, 17, 10, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "ALM", "buy") is False

    def test_blocks_order_before_market_open(self):
        from order_guard import check_can_submit
        fake_now = datetime(2026, 4, 15, 8, 0, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "AAPL", "buy") is False

    def test_blocks_on_weekend(self):
        from order_guard import check_can_submit
        # Saturday 11 AM ET
        fake_now = datetime(2026, 4, 18, 11, 0, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "AAPL", "sell") is False


class TestExtendedHoursProfile:
    def test_allows_order_at_5pm_et(self):
        """Extended hours: 4 AM - 8 PM ET. 5:10 PM is fine."""
        from order_guard import check_can_submit
        fake_now = datetime(2026, 4, 15, 17, 10, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx("extended_hours"), "AAPL", "buy") is True

    def test_blocks_order_at_9pm_et(self):
        from order_guard import check_can_submit
        fake_now = datetime(2026, 4, 15, 21, 0, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx("extended_hours"), "AAPL", "buy") is False


class TestTwentyFourSevenProfile:
    def test_allows_order_anytime(self):
        from order_guard import check_can_submit
        # Saturday 3 AM
        fake_now = datetime(2026, 4, 18, 3, 0, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx("24_7"), "BTC/USD", "buy") is True


class TestNoContext:
    def test_allows_order_when_no_ctx(self):
        """Legacy code paths that don't pass ctx should not crash."""
        from order_guard import check_can_submit
        assert check_can_submit(None, "AAPL", "buy") is True


class TestBothSidesGuarded:
    def test_buy_blocked(self):
        from order_guard import check_can_submit
        fake_now = datetime(2026, 4, 15, 17, 10, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "AAPL", "buy") is False

    def test_sell_blocked(self):
        from order_guard import check_can_submit
        fake_now = datetime(2026, 4, 15, 17, 10, tzinfo=_ET)
        with patch("order_guard.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert check_can_submit(_ctx(), "AAPL", "sell") is False


class TestOvershootGuard:
    """allowable_sell_qty pre-trade guard.

    2026-06-09 rewrite: prior to this date the guard checked the
    AGGREGATE broker pool and DOWNSIZED requested_qty to the aggregate
    if it was smaller — which is exactly how one profile consumed
    sibling profiles' shares (pid 42 asked for 2979 LXEH, aggregate
    pool had 2979 of which 1788 was pid 44's + 1191 was pid 45's;
    guard said "go" and pid 42 sold all 2979). The new policy:
    a profile may sell ONLY what its own journal says it holds; the
    aggregate broker pool is consulted as a drift sanity-check and
    NEVER as a downsize target. There is no path where this returns
    a positive qty less than requested.

    These tests cover the broker-only path (db_path=None). Per-profile
    behavior is covered in test_per_profile_sell_isolation_*.py.
    """

    def _api(self, positions=None, raise_on_list=False):
        api = MagicMock()
        if raise_on_list:
            api.list_positions.side_effect = RuntimeError("broker down")
        else:
            api.list_positions.return_value = positions or []
        return api

    def _pos(self, symbol, qty):
        p = MagicMock()
        p.symbol = symbol
        p.qty = qty
        return p

    def test_broker_has_enough_returns_full_qty(self):
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("AAPL", "100")])
        qty, reason = allowable_sell_qty(api, "AAPL", 50)
        assert qty == 50
        assert reason == "ok"

    def test_broker_has_zero_refuses_completely(self):
        """Broker has no long shares — refuse with allowed_qty=0."""
        from order_guard import allowable_sell_qty
        api = self._api([])
        qty, reason = allowable_sell_qty(api, "BBWI", 187)
        assert qty == 0
        # Post-rewrite the wording is "drift detected" since the
        # broker-only path doesn't know about siblings/journals.
        assert "drift" in reason.lower() or "refused" in reason.lower()

    def test_broker_has_less_than_requested_refuses(self):
        """REWRITTEN 2026-06-09. Pre-rewrite the guard downsized to
        the broker's qty when aggregate < requested — which is how
        one profile consumed sibling shares. Now: REFUSE, not
        downsize. There must be NO partial-fill behavior."""
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("MSFT", "5")])
        qty, reason = allowable_sell_qty(api, "MSFT", 17)
        assert qty == 0, (
            "Post-rewrite this path must refuse, not downsize. "
            "Downsize was the share-consumption bug class."
        )
        assert "drift" in reason.lower() or "refused" in reason.lower()

    def test_broker_short_position_refuses(self):
        """Broker is already net-short the symbol — refuse."""
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("BBWI", "-374")])
        qty, reason = allowable_sell_qty(api, "BBWI", 100)
        assert qty == 0
        # Either "drift" or the old "would create short" — the
        # invariant is qty == 0; the wording is implementation detail
        assert qty == 0

    def test_broker_api_failure_is_permissive(self):
        """If the broker API is down, default permissive — let the
        existing submit_order error handling surface real issues. We
        should never block trading because the GUARD couldn't query."""
        from order_guard import allowable_sell_qty
        api = self._api(raise_on_list=True)
        qty, reason = allowable_sell_qty(api, "AAPL", 10)
        assert qty == 10
        assert "permissive" in reason

    def test_other_symbols_dont_satisfy_check(self):
        """Broker has 100 AAPL but request is for MSFT — refuse for MSFT."""
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("AAPL", "100")])
        qty, reason = allowable_sell_qty(api, "MSFT", 10)
        assert qty == 0

    def test_zero_or_negative_qty_returns_zero(self):
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("AAPL", "100")])
        qty, _ = allowable_sell_qty(api, "AAPL", 0)
        assert qty == 0
        qty, _ = allowable_sell_qty(api, "AAPL", -5)
        assert qty == 0

    def test_options_contract_bypasses_guard(self):
        """Option short legs (covered calls, bull put spreads, iron
        condors) are intentional shorts. The guard would refuse them
        because broker has 0 long of the contract symbol; that's wrong.
        Bypass for OCC-formatted symbols."""
        from order_guard import allowable_sell_qty
        api = self._api([])
        qty, reason = allowable_sell_qty(api, "MSFT260612P00375000", 1)
        assert qty == 1
        assert "option" in reason.lower()

    def test_case_insensitive_symbol_match(self):
        from order_guard import allowable_sell_qty
        api = self._api([self._pos("aapl", "50")])
        qty, _ = allowable_sell_qty(api, "AAPL", 30)
        assert qty == 30


# --- 2026-05-16 audit: pre-submit buy-qty sanity guard ---


class TestAllowableBuyQty:
    """Caught 2026-05: NU 60×, KNX 28.5×, LEVI 129×, CSX 82× median —
    sizing-arithmetic bugs that the post-fact `position_runaway`
    detector flagged AFTER the trade had already filled. New
    pre-submit guard refuses qty > 20× profile-recent median.
    """

    def _seed_db(self, tmp_path, qtys):
        """Create a tmp DB with the trades schema + seeded BUY rows."""
        import sqlite3
        db = str(tmp_path / "trades.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                symbol TEXT, side TEXT, qty REAL, price REAL,
                status TEXT DEFAULT 'open'
            )
        """)
        for q in qtys:
            conn.execute(
                "INSERT INTO trades (symbol, side, qty, price) "
                "VALUES (?, 'buy', ?, 100.0)",
                ("AAPL", q),
            )
        conn.commit()
        conn.close()
        return db

    def test_normal_qty_passes(self, tmp_path):
        """Qty close to the median goes through cleanly."""
        from order_guard import allowable_buy_qty
        # Median 10, request 15 — well under 20× threshold.
        db = self._seed_db(tmp_path, [10] * 15)
        allowed, reason = allowable_buy_qty(db, "AAPL", 15)
        assert allowed == 15
        assert reason == "ok"

    def test_qty_just_under_threshold_passes(self, tmp_path):
        """19× median should still pass (threshold is >20×)."""
        from order_guard import allowable_buy_qty
        db = self._seed_db(tmp_path, [10] * 15)   # median = 10
        allowed, _ = allowable_buy_qty(db, "AAPL", 190)
        assert allowed == 190

    def test_qty_at_threshold_passes(self, tmp_path):
        """Exactly 20× median passes (strict >, not >=)."""
        from order_guard import allowable_buy_qty
        db = self._seed_db(tmp_path, [10] * 15)
        allowed, _ = allowable_buy_qty(db, "AAPL", 200)
        assert allowed == 200

    def test_excessive_qty_blocked(self, tmp_path):
        """The exact failure mode that motivated this guard:
        KNX-style 28.5× = sizing bug. Must block, not alert."""
        from order_guard import allowable_buy_qty
        db = self._seed_db(tmp_path, [10] * 15)
        allowed, reason = allowable_buy_qty(db, "AAPL", 285)
        assert allowed == 0
        assert "blocked" in reason.lower()
        assert "28.5x median" in reason

    def test_extreme_qty_blocked(self, tmp_path):
        """LEVI-style 129× — well into bug territory."""
        from order_guard import allowable_buy_qty
        db = self._seed_db(tmp_path, [10] * 15)
        allowed, _ = allowable_buy_qty(db, "AAPL", 1290)
        assert allowed == 0

    def test_insufficient_history_permissive(self, tmp_path):
        """Profiles with <10 BUY rows shouldn't get throttled — let
        them through and rely on the post-fact alert during ramp-up."""
        from order_guard import allowable_buy_qty
        db = self._seed_db(tmp_path, [10] * 3)   # only 3 rows
        allowed, reason = allowable_buy_qty(db, "AAPL", 10_000)
        assert allowed == 10_000
        assert "permissive" in reason.lower()

    def test_db_read_failure_permissive(self):
        """Bogus db_path = DB read fails = permissive (fall through to
        the post-fact alert)."""
        from order_guard import allowable_buy_qty
        allowed, reason = allowable_buy_qty(
            "/nonexistent/path.db", "AAPL", 100,
        )
        assert allowed == 100
        assert "permissive" in reason.lower()

    def test_no_db_path_permissive(self):
        from order_guard import allowable_buy_qty
        allowed, reason = allowable_buy_qty(None, "AAPL", 100)
        assert allowed == 100
        assert "no db_path" in reason.lower()

    def test_non_positive_qty_refused(self):
        from order_guard import allowable_buy_qty
        allowed, reason = allowable_buy_qty("/tmp/x.db", "AAPL", 0)
        assert allowed == 0
        assert "refused" in reason.lower()

    def test_options_contract_bypasses_buy_guard(self, tmp_path):
        """OCC-format symbols use a different qty convention (1
        contract = 100 shares). Skip the median comparison."""
        from order_guard import allowable_buy_qty
        db = self._seed_db(tmp_path, [10] * 15)
        allowed, reason = allowable_buy_qty(db, "MSFT260612P00375000", 1)
        assert allowed == 1
        assert "option" in reason.lower()


class TestStockMedianExcludesOptionContracts:
    """2026-05-21 regression: the buy-qty median must be computed from
    STOCK buys only. Pooling option-contract qtys (1-4) dragged the
    median to ~1.0 on options-heavy profiles, so legitimate stock
    BUYs (100s-1000s of shares) read as 100-1000× median and got
    blocked fleet-wide (ACHR 1134, GRAB 1899, SMR 301)."""

    def _seed_mixed(self, tmp_path, stock_qtys, option_qtys):
        """DB with the FULL schema (occ_symbol present), seeded with
        a mix of stock BUYs (occ_symbol NULL) and option BUYs
        (occ_symbol set)."""
        import sqlite3
        db = str(tmp_path / "mixed.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                symbol TEXT, side TEXT, qty REAL, price REAL,
                occ_symbol TEXT, status TEXT DEFAULT 'open'
            )
        """)
        for q in option_qtys:
            conn.execute(
                "INSERT INTO trades (symbol, side, qty, price, occ_symbol) "
                "VALUES ('QCOM', 'buy', ?, 5.0, 'QCOM260626C00225000')",
                (q,),
            )
        for q in stock_qtys:
            conn.execute(
                "INSERT INTO trades (symbol, side, qty, price, occ_symbol) "
                "VALUES ('AAPL', 'buy', ?, 200.0, NULL)",
                (q,),
            )
        conn.commit()
        conn.close()
        return db

    def test_stock_buy_not_blocked_by_option_polluted_median(self, tmp_path):
        """The exact prod scenario: profile has 40 option-contract
        buys (qty 1-4) and 12 stock buys (qty ~100). A new stock BUY
        of 1000 shares must NOT be blocked — the stock-only median is
        ~100, so 1000 is 10× (under the 20× threshold)."""
        from order_guard import allowable_buy_qty
        db = self._seed_mixed(
            tmp_path,
            stock_qtys=[100] * 12,
            option_qtys=[1, 2, 3, 4] * 10,  # 40 option buys, median ~2
        )
        allowed, reason = allowable_buy_qty(db, "AAPL", 1000)
        assert allowed == 1000, (
            f"Stock BUY of 1000 shares was blocked: {reason}. The "
            "median must be computed from STOCK buys only (~100), not "
            "the option-polluted pool (~2). 1000/100 = 10× is under "
            "the 20× threshold."
        )
        assert reason == "ok"

    def test_genuinely_excessive_stock_buy_still_blocked(self, tmp_path):
        """The guard must still catch a real sizing bug: 3000 shares
        against a stock median of 100 = 30× → blocked. (Confirms the
        fix didn't neuter the guard entirely.)"""
        from order_guard import allowable_buy_qty
        db = self._seed_mixed(
            tmp_path,
            stock_qtys=[100] * 12,
            option_qtys=[1, 2, 3, 4] * 10,
        )
        allowed, reason = allowable_buy_qty(db, "AAPL", 3000)
        assert allowed == 0
        assert "blocked" in reason.lower()

    def test_insufficient_stock_history_is_permissive(self, tmp_path):
        """A profile with only option buys (zero/few stock buys) can't
        produce a confident stock median → permissive, so a first
        stock BUY isn't artificially throttled."""
        from order_guard import allowable_buy_qty
        db = self._seed_mixed(
            tmp_path,
            stock_qtys=[100] * 3,   # only 3 stock buys (< 10 min)
            option_qtys=[1, 2, 3, 4] * 10,
        )
        allowed, reason = allowable_buy_qty(db, "AAPL", 1000)
        assert allowed == 1000
        assert "permissive" in reason.lower()
