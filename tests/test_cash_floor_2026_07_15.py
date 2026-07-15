"""Virtual-cash floor class fix (2026-07-15).

The p217 shape: a market BUY sized to 100.0% of remaining cash at the
DECISION price ($21.70) filled 10 cents higher and took the virtual
account to -$5.05 — every cash check on the buy side compared against
the decision price with zero slippage headroom, and the order door
(GuardedAlpacaApi) had NO buy-side check at all (`side != 'sell'` →
early return). p216 sat $18.86 from the same fate; p218 bottomed at
$125.78 on a $250K book.

Pinned here:
  - journal.get_virtual_cash — the cash-only reader the door uses —
    stays penny-identical to get_virtual_account_info's cash (single
    source; two implementations of "cash" is how books drift), and
    screams (rate-limited ERROR) on negative cash.
  - order_guard.assert_buy_within_own_cash — the unbypassable door:
    refuses a stock BUY whose estimated cost (ref price × qty, padded
    by the market slippage reserve) exceeds own virtual cash;
    buy-to-cover (exit) always flows; OCC and non-virtual pass;
    unpriceable/unreadable → fail-closed.
  - portfolio_manager cash check budgets the reserve for market
    orders; limit orders compare exactly.
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import config


# ---------------------------------------------------------------------------
# get_virtual_cash — parity + alarm
# ---------------------------------------------------------------------------

@pytest.fixture
def profile_db(tmp_path):
    path = str(tmp_path / "quantopsai_profile_9.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, "
        "occ_symbol TEXT, side TEXT, qty REAL, price REAL, "
        "fill_price REAL, status TEXT, data_quality TEXT)")
    rows = [
        # open long: buy 10 @ decision 100, filled 101 → cash -1010
        ("AAPL", None, "buy", 10, 100.0, 101.0, "open", None),
        # closed round trip: sell 5 @ 110 fill → +550
        ("AAPL", None, "sell", 5, 110.0, 110.0, "closed", None),
        # canceled protective keeps its trigger price — must NOT count
        ("AAPL", None, "sell", 5, 95.0, 0, "canceled", None),
        # phantom-close — broker never filled: no money moved
        ("MSFT", None, "buy", 3, 200.0, 0, "auto_reconciled_phantom_close",
         None),
        # externally-closed option leg: cash booked by activities pass
        ("CVX", "CVX260717P00170000", "buy", 1, 1.22, 1.22,
         "auto_closed_external", None),
        # option leg: 100x multiplier — sell-to-open credit +122
        ("CVX", "CVX260717P00165000", "sell", 1, 1.22, 1.22, "open", None),
        # data_quality-tagged row excluded
        ("NOK", None, "buy", 100, 5.0, 5.0, "open", "phantom_2026"),
    ]
    conn.executemany(
        "INSERT INTO trades (symbol, occ_symbol, side, qty, price, "
        "fill_price, status, data_quality) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


class TestGetVirtualCash:
    def test_cash_matches_account_info_to_the_penny(self, profile_db):
        """ONE cash truth: the door's cheap reader and the full account
        snapshot must agree exactly, on a ledger exercising fill-price
        preference, dead statuses, the option multiplier, and
        data-quality exclusion."""
        from journal import get_virtual_cash, get_virtual_account_info
        cash = get_virtual_cash(profile_db, initial_capital=10_000.0)
        info = get_virtual_account_info(profile_db,
                                        initial_capital=10_000.0)
        assert cash == pytest.approx(
            10_000.0 - 1010.0 + 550.0 + 122.0)
        assert round(cash, 2) == info["cash"]

    def test_negative_cash_logs_error(self, profile_db, caplog):
        import logging
        import journal
        journal._NEG_CASH_ALARM_MEMO.clear()
        with caplog.at_level(logging.ERROR):
            cash = journal.get_virtual_cash(profile_db,
                                            initial_capital=300.0)
        assert cash < 0
        assert any("NEGATIVE VIRTUAL CASH" in r.message
                   for r in caplog.records), (
            "an overdrawn virtual book must scream — buying_power's "
            "max(cash, 0) clamp hid p217's -$5.05 from every consumer")

    def test_unreadable_table_returns_none(self, tmp_path):
        from journal import get_virtual_cash
        path = str(tmp_path / "empty.db")
        sqlite3.connect(path).close()  # no trades table
        assert get_virtual_cash(path, initial_capital=1000.0) is None


# ---------------------------------------------------------------------------
# The cash door
# ---------------------------------------------------------------------------

def _ctx(db_path, initial_capital=25_000.0, virtual=True):
    ctx = MagicMock()
    ctx.is_virtual = virtual
    ctx.db_path = db_path
    ctx.initial_capital = initial_capital
    ctx.display_name = "test-profile"
    return ctx


def _api(last_price=None, error=None):
    api = MagicMock()
    if error is not None:
        api.get_latest_trade.side_effect = error
    else:
        trade = MagicMock()
        trade.price = last_price
        api.get_latest_trade.return_value = trade
    return api


class TestBuyCashDoor:
    def test_overdrawing_market_buy_refused(self, profile_db, monkeypatch):
        """THE p217 shape: cash $1,782.55, market BUY of 82 × $21.70 =
        $1,779.40 'fits' at the decision price but not with the fill
        reserve — the door must refuse it."""
        import journal
        import order_guard
        monkeypatch.setattr(journal, "get_virtual_cash",
                            lambda *a, **k: 1782.55)
        monkeypatch.setattr(journal, "get_virtual_positions",
                            lambda *a, **k: [])
        kwargs = {"symbol": "FCEL", "side": "buy", "qty": 82,
                  "type": "market"}
        with pytest.raises(order_guard.CashFloorGuardError):
            order_guard.assert_buy_within_own_cash(
                _api(last_price=21.70), _ctx(profile_db), kwargs)

    def test_affordable_buy_passes(self, profile_db, monkeypatch):
        import journal
        import order_guard
        monkeypatch.setattr(journal, "get_virtual_cash",
                            lambda *a, **k: 5_000.0)
        monkeypatch.setattr(journal, "get_virtual_positions",
                            lambda *a, **k: [])
        kwargs = {"symbol": "FCEL", "side": "buy", "qty": 82}
        order_guard.assert_buy_within_own_cash(
            _api(last_price=21.70), _ctx(profile_db), kwargs)  # no raise

    def test_limit_buy_uses_limit_price_without_reserve(
            self, profile_db, monkeypatch):
        """A limit fill can't exceed the limit price — exact compare.
        qty*limit == cash passes; the market reserve would refuse it."""
        import journal
        import order_guard
        monkeypatch.setattr(journal, "get_virtual_cash",
                            lambda *a, **k: 2170.0)
        monkeypatch.setattr(journal, "get_virtual_positions",
                            lambda *a, **k: [])
        kwargs = {"symbol": "FCEL", "side": "buy", "qty": 100,
                  "type": "limit", "limit_price": "21.70"}
        order_guard.assert_buy_within_own_cash(
            _api(), _ctx(profile_db), kwargs)  # no raise, no trade fetch

    def test_buy_to_cover_always_flows(self, profile_db, monkeypatch):
        """SHORT protectives are side='buy'. A cover is an exit — it
        must NEVER be cash-gated, even on a broke book (a short loss
        can legitimately exceed the credit received)."""
        import journal
        import order_guard
        monkeypatch.setattr(journal, "get_virtual_cash",
                            lambda *a, **k: -5.05)
        monkeypatch.setattr(
            journal, "get_virtual_positions",
            lambda *a, **k: [{"symbol": "GOOG", "occ_symbol": None,
                              "qty": -40}])
        kwargs = {"symbol": "GOOG", "side": "buy", "qty": 40}
        order_guard.assert_buy_within_own_cash(
            _api(last_price=200.0), _ctx(profile_db), kwargs)  # no raise

    def test_partial_cover_gates_only_the_increment(
            self, profile_db, monkeypatch):
        """Short 40, buy 50: the 40-share cover flows, the 10-share new
        long must fit cash. Cash covers 10×$200×1.01=$2,020? Give
        $1,000 → refuse."""
        import journal
        import order_guard
        monkeypatch.setattr(journal, "get_virtual_cash",
                            lambda *a, **k: 1_000.0)
        monkeypatch.setattr(
            journal, "get_virtual_positions",
            lambda *a, **k: [{"symbol": "GOOG", "occ_symbol": None,
                              "qty": -40}])
        kwargs = {"symbol": "GOOG", "side": "buy", "qty": 50}
        with pytest.raises(order_guard.CashFloorGuardError):
            order_guard.assert_buy_within_own_cash(
                _api(last_price=200.0), _ctx(profile_db), kwargs)

    def test_occ_symbols_bypass(self, profile_db):
        """Options budget is enforced in the options pipeline; the
        stock door returns early on OCC symbols (same as the sell
        door)."""
        import order_guard
        kwargs = {"symbol": "CVX260717P00170000", "side": "buy", "qty": 1}
        order_guard.assert_buy_within_own_cash(
            _api(error=RuntimeError("no fetch expected")),
            _ctx(profile_db), kwargs)  # no raise

    def test_sells_bypass(self, profile_db):
        import order_guard
        kwargs = {"symbol": "AAPL", "side": "sell", "qty": 10}
        order_guard.assert_buy_within_own_cash(
            _api(error=RuntimeError("no fetch expected")),
            _ctx(profile_db), kwargs)  # no raise

    def test_non_virtual_account_bypasses(self, profile_db):
        import order_guard
        kwargs = {"symbol": "AAPL", "side": "buy", "qty": 10}
        order_guard.assert_buy_within_own_cash(
            _api(error=RuntimeError("no fetch expected")),
            _ctx(profile_db, virtual=False), kwargs)  # no raise

    def test_unpriceable_buy_fails_closed(self, profile_db, monkeypatch):
        """No limit price and the latest-trade fetch fails → the cost
        is unknowable → refuse. A skipped entry is recoverable next
        cycle; an overdraw is booked money."""
        import journal
        import order_guard
        monkeypatch.setattr(journal, "get_virtual_positions",
                            lambda *a, **k: [])
        kwargs = {"symbol": "AAPL", "side": "buy", "qty": 10}
        with pytest.raises(order_guard.CashFloorGuardError):
            order_guard.assert_buy_within_own_cash(
                _api(error=RuntimeError("data outage")),
                _ctx(profile_db), kwargs)

    def test_unreadable_cash_fails_closed(self, profile_db, monkeypatch):
        import journal
        import order_guard
        monkeypatch.setattr(journal, "get_virtual_cash",
                            lambda *a, **k: None)
        monkeypatch.setattr(journal, "get_virtual_positions",
                            lambda *a, **k: [])
        kwargs = {"symbol": "AAPL", "side": "buy", "qty": 10}
        with pytest.raises(order_guard.CashFloorGuardError):
            order_guard.assert_buy_within_own_cash(
                _api(last_price=100.0), _ctx(profile_db), kwargs)

    def test_door_is_wired_into_guarded_submit(self):
        """The check must sit in GuardedAlpacaApi.submit_order right
        after the oversell door — the single choke point every submit
        crosses. Sizing-layer checks alone are bypassable."""
        import order_guard
        src = inspect.getsource(order_guard.GuardedAlpacaApi.submit_order)
        assert "assert_buy_within_own_cash" in src
        assert (src.find("assert_sell_within_own_book")
                < src.find("assert_buy_within_own_cash")
                < src.find("api.submit_order("))


# ---------------------------------------------------------------------------
# Sizing-layer reserve
# ---------------------------------------------------------------------------

class TestSizingReserve:
    def test_portfolio_constraint_pads_market_orders(self):
        from portfolio_manager import check_portfolio_constraints
        account = {"equity": 25_000.0, "cash": 1_782.55}
        # 82 × 21.70 = 1,779.40 ≤ cash at decision price, but the
        # reserve pushes required past cash → refused.
        proposed = {"side": "buy", "qty": 82, "price": 21.70,
                    "order_type": "market"}
        allowed, reason = check_portfolio_constraints(
            "FCEL", proposed, {}, account,
            max_position_pct=0.16, max_total_positions=999)
        assert not allowed
        assert "slippage" in reason.lower() or "cash" in reason.lower()

    def test_portfolio_constraint_exact_for_limit_orders(self):
        from portfolio_manager import check_portfolio_constraints
        account = {"equity": 25_000.0, "cash": 1_779.40}
        proposed = {"side": "buy", "qty": 82, "price": 21.70,
                    "order_type": "limit"}
        allowed, _ = check_portfolio_constraints(
            "FCEL", proposed, {}, account,
            max_position_pct=0.16, max_total_positions=999)
        assert allowed

    def test_unknown_order_type_treated_as_market(self):
        from portfolio_manager import check_portfolio_constraints
        account = {"equity": 25_000.0, "cash": 1_779.40}
        proposed = {"side": "buy", "qty": 82, "price": 21.70}
        allowed, _ = check_portfolio_constraints(
            "FCEL", proposed, {}, account,
            max_position_pct=0.16, max_total_positions=999)
        assert not allowed, "unknown order type must budget conservatively"

    def test_execute_trade_budgets_cash_net_of_reserve(self):
        """Source pin: the sizing budget divides cash by (1 + reserve)
        for market orders, so a 100%-of-cash buy leaves fill headroom."""
        import trade_pipeline
        src = inspect.getsource(trade_pipeline)
        assert ("cash_budget = cash / "
                "(1 + config.MARKET_BUY_SLIPPAGE_RESERVE_PCT)") in src
        assert "dollars = min(max_dollars, cash_budget)" in src

    def test_cycle_cash_adjustment_is_conservative_both_ways(self):
        """In-cycle debits pad, credits haircut — a same-cycle BUY can
        never spend proceeds the sell's fill won't deliver."""
        from trade_pipeline import _adjust_cycle_cash
        r = 1 + config.MARKET_BUY_SLIPPAGE_RESERVE_PCT
        account = {"cash": 10_000.0}
        _adjust_cycle_cash(account, {"action": "BUY", "qty": 10,
                                     "price": 100.0})
        assert account["cash"] == pytest.approx(10_000.0 - 1000.0 * r)
        account = {"cash": 10_000.0}
        _adjust_cycle_cash(account, {"action": "SELL", "qty": 10,
                                     "price": 100.0})
        assert account["cash"] == pytest.approx(10_000.0 + 1000.0 / r)

    def test_reserve_constant_is_sane(self):
        """0 would re-open the p217 class; >5% would strand real money
        idle. Observed cohort slippage: 46-120bps."""
        assert 0.005 <= config.MARKET_BUY_SLIPPAGE_RESERVE_PCT <= 0.05


# ---------------------------------------------------------------------------
# Adversarial-review round (2026-07-15) pins
# ---------------------------------------------------------------------------

class TestReviewRoundCashDoor:
    def test_protective_cover_intent_bypasses_the_door(self, profile_db):
        """Review #9: a SHORT's buy-side protective is an exit by
        construction (broker-backing verified upstream) — the door must
        never gate it, immune to journal-read hiccups."""
        import order_guard
        kwargs = {"symbol": "GOOG", "side": "buy", "qty": 40}
        order_guard.assert_buy_within_own_cash(
            _api(error=RuntimeError("no fetch expected")),
            _ctx(profile_db),
            kwargs, intent=order_guard.INTENT_PROTECTIVE_COVER)  # no raise

    def test_bracket_orders_declares_the_cover_intent(self):
        import inspect
        import bracket_orders
        src = inspect.getsource(bracket_orders._submit_protective)
        assert "INTENT_PROTECTIVE_COVER" in src, (
            "buy-side protectives must declare intent so the cash door "
            "can never block a short's cover stop")

    def test_door_reserves_pending_protective_buy_commitments(
            self, profile_db, monkeypatch):
        """Review #6: money a resting cover stop will need is not
        spendable — the cash math excludes pending_protective rows, so
        a cover firing mid-cycle leaves cash stale-HIGH; the door
        subtracts the commitment."""
        import sqlite3 as _sq
        import journal
        import order_guard
        conn = _sq.connect(profile_db)
        conn.execute(
            "INSERT INTO trades (symbol, occ_symbol, side, qty, price, "
            "fill_price, status) VALUES ('MARA', NULL, 'buy', 1000, "
            "22.0, 0, 'pending_protective')")
        conn.commit()
        conn.close()
        assert journal.get_pending_protective_buy_commitment(
            profile_db) == pytest.approx(22000.0)
        monkeypatch.setattr(journal, "get_virtual_cash",
                            lambda *a, **k: 30_000.0)
        monkeypatch.setattr(journal, "get_virtual_positions",
                            lambda *a, **k: [])
        # 29.7K entry passes raw cash 30K but NOT 30K - 22K committed
        kwargs = {"symbol": "NVDA", "side": "buy", "qty": 300}
        with pytest.raises(order_guard.CashFloorGuardError):
            order_guard.assert_buy_within_own_cash(
                _api(last_price=99.0), _ctx(profile_db), kwargs)

    def test_multileg_debit_feeds_cycle_cash(self):
        """Review #5: a net-debit spread's estimated cost must debit the
        in-cycle snapshot (legs journal price=NULL until backfill)."""
        from trade_pipeline import _adjust_cycle_cash
        r = 1 + config.MARKET_BUY_SLIPPAGE_RESERVE_PCT
        acct = {"cash": 10_000.0}
        _adjust_cycle_cash(acct, {"action": "MULTILEG_OPEN",
                                  "estimated_cost": 1200.0})
        assert acct["cash"] == pytest.approx(10_000.0 - 1200.0 * r)
        # credit spreads carry no estimated_cost → no-op (an unfilled
        # credit must never fund a same-cycle buy)
        acct = {"cash": 10_000.0}
        assert _adjust_cycle_cash(
            acct, {"action": "MULTILEG_OPEN"}) == 0.0

    def test_multileg_executor_has_the_debit_cash_floor(self):
        import inspect
        import pipelines.option as po
        src = inspect.getsource(po.OptionPipeline._execute_multileg)
        assert "OPTION CASH FLOOR" in src
        assert "get_virtual_cash" in src
        assert "fail-closed" in src.lower() or "fail-CLOSED" in src

    def test_pair_leg_a_journaled_when_leg_b_fails(self):
        import inspect
        import stat_arb_pair_book
        src = inspect.getsource(stat_arb_pair_book)
        idx = src.find("but leg B failed")
        region = src[idx:idx + 2500]
        assert "log_trade(" in region, (
            "review #7: a live half-pair leg-A order must be journaled "
            "on the leg-B failure path, not left to the recovery ledger")
