"""Tests for pnl.py — FIFO lot matching + range-midpoint P&L estimator.

Uses mock price lookup functions so tests are fully hermetic. No
network calls, no filesystem.
"""

from __future__ import annotations

from congresstrades.pnl import (
    match_fifo_lots,
    Roundtrip,
    OpenPosition,
    MemberPerformance,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _price_mock(series: dict):
    """Build a (price_at_date, current_price) pair backed by a dict.

    series: {ticker: [(date_str, close_price), ...]} sorted by date.
    """
    def price_at_date(ticker, iso_date):
        rows = series.get(ticker) or []
        # First row on or after iso_date
        for d, p in rows:
            if d >= iso_date:
                return p
        return None

    def current_price(ticker):
        rows = series.get(ticker) or []
        return rows[-1][1] if rows else None

    return price_at_date, current_price


def _trade(member, symbol, tx_type, date, low, high):
    return {
        "member_name": member,
        "ticker": symbol,
        "transaction_type": tx_type,
        "transaction_date": date,
        "amount_low": low,
        "amount_high": high,
    }


# ---------------------------------------------------------------------------
# Basic FIFO matching
# ---------------------------------------------------------------------------

class TestBasicFifoMatching:
    def test_single_buy_sell_closes_roundtrip(self):
        trades = [
            _trade("Alice", "AAPL", "buy",  "2025-01-10", 15001, 50000),
            _trade("Alice", "AAPL", "sell", "2025-04-01", 15001, 50000),
        ]
        prices = {"AAPL": [("2025-01-10", 100.0), ("2025-04-01", 120.0)]}
        paod, cur = _price_mock(prices)
        perf = match_fifo_lots(trades, paod, cur)

        assert perf.member == "Alice"
        assert perf.n_buys == 1
        assert perf.n_sells == 1
        assert len(perf.closed_roundtrips) == 1
        assert len(perf.open_positions) == 0
        rt = perf.closed_roundtrips[0]
        assert abs(rt.return_pct - 0.20) < 1e-9  # 120/100 - 1 = 20%

    def test_unmatched_buy_becomes_open_position(self):
        trades = [
            _trade("Bob", "NVDA", "buy", "2025-02-01", 1001, 15000),
        ]
        prices = {"NVDA": [("2025-02-01", 50.0), ("2025-12-01", 80.0)]}
        paod, cur = _price_mock(prices)
        perf = match_fifo_lots(trades, paod, cur)

        assert len(perf.closed_roundtrips) == 0
        assert len(perf.open_positions) == 1
        op = perf.open_positions[0]
        assert abs(op.return_pct - 0.60) < 1e-9   # 80/50 - 1 = 60%

    def test_sell_without_prior_buy_is_dropped(self):
        # STOCK Act: a sell can happen without us seeing the buy (if
        # the buy was pre-2023 for example). We skip it silently and
        # don't create a phantom loss.
        trades = [
            _trade("Carol", "MSFT", "sell", "2025-03-01", 15001, 50000),
        ]
        prices = {"MSFT": [("2025-03-01", 300.0)]}
        paod, cur = _price_mock(prices)
        perf = match_fifo_lots(trades, paod, cur)

        assert perf.n_sells == 1
        assert len(perf.closed_roundtrips) == 0
        assert len(perf.open_positions) == 0


class TestFifoOrdering:
    def test_two_buys_one_sell_consumes_oldest(self):
        # Two buys of AAPL, then one sell — oldest gets matched
        trades = [
            _trade("D", "AAPL", "buy",  "2025-01-05", 1001, 15000),
            _trade("D", "AAPL", "buy",  "2025-02-05", 1001, 15000),
            _trade("D", "AAPL", "sell", "2025-03-05", 1001, 15000),
        ]
        prices = {"AAPL": [
            ("2025-01-05", 100.0),
            ("2025-02-05", 110.0),
            ("2025-03-05", 130.0),
        ]}
        paod, cur = _price_mock(prices)
        perf = match_fifo_lots(trades, paod, cur)

        assert len(perf.closed_roundtrips) == 1
        assert perf.closed_roundtrips[0].buy_date == "2025-01-05"
        # Remaining buy (Feb) is open
        assert len(perf.open_positions) == 1
        assert perf.open_positions[0].buy_date == "2025-02-05"


# ---------------------------------------------------------------------------
# P&L math / uncertainty bands
# ---------------------------------------------------------------------------

class TestPnlBounds:
    def test_bounds_widen_with_range(self):
        """A wider amount range → wider uncertainty on dollar P&L."""
        trades = [
            _trade("E", "AAPL", "buy",  "2025-01-01", 1001, 15000),   # wide
            _trade("E", "AAPL", "sell", "2025-06-01", 1001, 15000),
        ]
        prices = {"AAPL": [("2025-01-01", 100.0), ("2025-06-01", 110.0)]}
        paod, cur = _price_mock(prices)
        perf = match_fifo_lots(trades, paod, cur)
        rt = perf.closed_roundtrips[0]
        low, mid, high = rt.estimated_pnl()
        # 10% gain × $1001..$15000 band
        assert abs(low - 100.1) < 1.0     # 1001 × 0.10
        assert abs(mid - 800.05) < 1.0    # 8000.5 × 0.10
        assert abs(high - 1500.0) < 1.0   # 15000 × 0.10
        # Bounds strictly ordered
        assert low < mid < high

    def test_loss_flips_bounds_in_dollar_terms(self):
        """When the trade lost money, the LARGER position (high bound)
        means a BIGGER loss — so 'low dollar bound' = most negative."""
        trades = [
            _trade("F", "STOCK", "buy",  "2025-01-01", 1001, 15000),
            _trade("F", "STOCK", "sell", "2025-06-01", 1001, 15000),
        ]
        prices = {"STOCK": [("2025-01-01", 100.0), ("2025-06-01", 80.0)]}
        paod, cur = _price_mock(prices)
        perf = match_fifo_lots(trades, paod, cur)
        rt = perf.closed_roundtrips[0]
        low, mid, high = rt.estimated_pnl()
        assert low < 0 and mid < 0 and high < 0   # all losses
        # low is the WORST (most negative)
        assert low <= mid <= high
        # low = 15000 × -0.20 = -3000
        assert abs(low - (-3000.0)) < 2.0

    def test_no_prices_yields_none_bounds(self):
        trades = [
            _trade("G", "FAKE", "buy",  "2025-01-01", 1001, 15000),
            _trade("G", "FAKE", "sell", "2025-06-01", 1001, 15000),
        ]
        paod, cur = _price_mock({})  # no price data
        perf = match_fifo_lots(trades, paod, cur)
        rt = perf.closed_roundtrips[0]
        assert rt.return_pct is None
        assert rt.estimated_pnl() == (None, None, None)


class TestAggregation:
    def test_realized_and_unrealized_separate(self):
        trades = [
            _trade("H", "AAPL", "buy",  "2025-01-01", 1001, 15000),
            _trade("H", "AAPL", "sell", "2025-06-01", 1001, 15000),
            _trade("H", "MSFT", "buy",  "2025-02-01", 15001, 50000),
        ]
        prices = {
            "AAPL": [("2025-01-01", 100.0), ("2025-06-01", 110.0),
                     ("2025-12-01", 120.0)],
            "MSFT": [("2025-02-01", 300.0), ("2025-12-01", 360.0)],
        }
        paod, cur = _price_mock(prices)
        perf = match_fifo_lots(trades, paod, cur)

        assert len(perf.closed_roundtrips) == 1  # AAPL round-trip
        assert len(perf.open_positions) == 1     # MSFT still held

        rlow, rmid, rhigh = perf.realized_bounds()
        ulow, umid, uhigh = perf.unrealized_bounds()
        total_mid = perf.total_bounds()[1]
        assert abs(total_mid - (rmid + umid)) < 0.01

    def test_win_rate_counts_closed_only(self):
        trades = [
            _trade("I", "A", "buy",  "2025-01-01", 1001, 15000),
            _trade("I", "A", "sell", "2025-02-01", 1001, 15000),
            _trade("I", "B", "buy",  "2025-01-05", 1001, 15000),
            _trade("I", "B", "sell", "2025-02-05", 1001, 15000),
            _trade("I", "C", "buy",  "2025-03-01", 1001, 15000),  # open
        ]
        prices = {
            "A": [("2025-01-01", 100.0), ("2025-02-01", 120.0)],  # win
            "B": [("2025-01-05", 100.0), ("2025-02-05", 80.0)],    # loss
            "C": [("2025-03-01", 100.0), ("2025-12-01", 150.0)],
        }
        paod, cur = _price_mock(prices)
        perf = match_fifo_lots(trades, paod, cur)
        # 1 win / 2 closed = 50%, ignoring the open C position
        assert perf.closed_win_rate() == 0.5


class TestPartialSale:
    def test_partial_sale_treated_as_sell(self):
        trades = [
            _trade("J", "AAPL", "buy",          "2025-01-01", 15001, 50000),
            _trade("J", "AAPL", "partial_sale", "2025-06-01", 1001, 15000),
        ]
        prices = {"AAPL": [("2025-01-01", 100.0), ("2025-06-01", 130.0)]}
        paod, cur = _price_mock(prices)
        perf = match_fifo_lots(trades, paod, cur)

        # Sell is partial — closed slice + remaining open
        assert len(perf.closed_roundtrips) == 1
        assert len(perf.open_positions) == 1
        rt = perf.closed_roundtrips[0]
        assert abs(rt.return_pct - 0.30) < 1e-9

    def test_exchange_is_skipped(self):
        """Exchanges aren't buys or sells — skip them without creating
        false positions or matching against existing lots."""
        trades = [
            _trade("K", "AAPL", "exchange", "2025-05-01", 1001, 15000),
        ]
        paod, cur = _price_mock({})
        perf = match_fifo_lots(trades, paod, cur)
        assert perf.skipped_untracked == 1
        assert len(perf.closed_roundtrips) == 0
        assert len(perf.open_positions) == 0


class TestDataValidation:
    def test_missing_fields_dont_crash(self):
        bad = [
            {"member_name": "Z", "ticker": None, "transaction_type": "buy",
             "transaction_date": "2025-01-01", "amount_low": 1000, "amount_high": 2000},
            {"member_name": "Z", "ticker": "AAPL", "transaction_type": "buy",
             "transaction_date": None, "amount_low": 1000, "amount_high": 2000},
        ]
        paod, cur = _price_mock({})
        perf = match_fifo_lots(bad, paod, cur)
        assert perf.skipped_untracked == 2

    def test_negative_prices_treated_as_unknown(self):
        """A bad price lookup returning 0 or negative shouldn't compute a return."""
        trades = [
            _trade("L", "AAPL", "buy",  "2025-01-01", 1001, 15000),
            _trade("L", "AAPL", "sell", "2025-06-01", 1001, 15000),
        ]
        prices = {"AAPL": [("2025-01-01", 0.0), ("2025-06-01", 100.0)]}
        paod, cur = _price_mock(prices)
        perf = match_fifo_lots(trades, paod, cur)
        rt = perf.closed_roundtrips[0]
        assert rt.return_pct is None  # buy_price was 0
