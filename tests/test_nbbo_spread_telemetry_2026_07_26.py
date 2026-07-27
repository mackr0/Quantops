"""NBBO spread telemetry at decision (P2.2 step 1, 2026-07-26).

The equity path never read the NBBO: get_snapshot aliased the last
TRADE price into latest_bid/latest_ask, so nothing in the system
knew the spread it was crossing. Step 1 is telemetry only — real
bid/ask + spread_bps in the snapshot, and spread_bps_at_decision
stamped on entry trade rows — with NO execution behavior change
(the slippage estimator still receives spread_bps=None; feeding it
the real spread changes predicted costs and specialist firing, and
that rollout — step 2, marketable limits — is operator-timed).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

import market_data as md  # noqa: E402


class _Quote:
    """Shape of alpaca_trade_api QuoteV2: raw 'bp'/'ap' keys plus the
    mapped attribute names (which have varied across client versions
    — the live incident: bidprice vs bid_price)."""

    def __init__(self, bid, ask):
        self._raw = {"bp": bid, "ap": ask}
        self.bid_price = bid
        self.ask_price = ask


def _client(bid, ask):
    c = MagicMock()
    c.get_latest_quote.return_value = _Quote(bid, ask)
    return c


class TestGetNbbo:
    def test_two_sided_quote(self):
        with patch.object(md, "_get_alpaca_data_client",
                          return_value=_client(99.95, 100.05)):
            out = md.get_nbbo_spread_bps("AAPL")
        assert out["bid"] == 99.95 and out["ask"] == 100.05
        assert out["spread_bps"] == 10.0  # 0.10 on mid 100.00

    def test_one_sided_or_crossed_refused(self):
        for bid, ask in ((0, 100.05), (99.95, 0), (100.10, 100.00)):
            with patch.object(md, "_get_alpaca_data_client",
                              return_value=_client(bid, ask)):
                assert md.get_nbbo_spread_bps("AAPL") is None

    def test_crypto_and_no_client_return_none(self):
        assert md.get_nbbo_spread_bps("BTC/USD") is None
        with patch.object(md, "_get_alpaca_data_client",
                          return_value=None):
            assert md.get_nbbo_spread_bps("AAPL") is None


class TestSnapshotHonestBidAsk:
    def test_snapshot_carries_real_nbbo(self):
        client = _client(99.95, 100.05)
        trade = MagicMock()
        trade.price = 100.01
        client.get_latest_trade.return_value = trade
        import pandas as pd
        bars = pd.DataFrame({"close": [99.0, 100.0],
                             "volume": [1000, 2000]})
        with patch.object(md, "_get_alpaca_data_client",
                          return_value=client), \
             patch.object(md, "get_bars", return_value=bars):
            snap = md.get_snapshot("AAPL")
        assert snap["latest_bid"] == 99.95
        assert snap["latest_ask"] == 100.05
        assert snap["spread_bps"] == 10.0, (
            "get_snapshot aliased the trade price into bid/ask again "
            "— the NBBO must be a real quote."
        )

    def test_snapshot_falls_back_when_no_quote(self):
        client = _client(0, 0)
        trade = MagicMock()
        trade.price = 100.01
        client.get_latest_trade.return_value = trade
        import pandas as pd
        bars = pd.DataFrame({"close": [99.0, 100.0],
                             "volume": [1000, 2000]})
        with patch.object(md, "_get_alpaca_data_client",
                          return_value=client), \
             patch.object(md, "get_bars", return_value=bars):
            snap = md.get_snapshot("AAPL")
        assert snap["latest_bid"] == 100.01
        assert snap["spread_bps"] is None


class TestJournalColumn:
    def test_log_trade_stamps_spread(self):
        from journal import init_db, log_trade
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(path)
        try:
            log_trade(symbol="XYZ", side="buy", qty=1, price=10.0,
                      order_id="o-1", spread_bps_at_decision=7.5,
                      db_path=path)
            log_trade(symbol="XYZ", side="buy", qty=1, price=10.0,
                      order_id="o-2", db_path=path)
            conn = sqlite3.connect(path)
            rows = conn.execute(
                "SELECT order_id, spread_bps_at_decision FROM trades "
                "ORDER BY id").fetchall()
            conn.close()
            assert rows[0] == ("o-1", 7.5)
            assert rows[1] == ("o-2", None)  # no-quote decisions stay NULL
        finally:
            os.unlink(path)


class TestNoBehaviorChange:
    def test_entry_estimator_still_blind_to_spread(self):
        """Step 1 is telemetry. The entry-path estimate_slippage call
        must NOT receive the live spread until the operator times the
        step-2 rollout (it would shift predicted costs and
        wide_spread/slippage_high specialist firing)."""
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        i = src.find('_est = estimate_slippage(\n                    symbol=symbol, qty=qty, side="buy"')
        assert i != -1
        block = src[i:i + 400]
        assert "spread_bps" not in block, (
            "the entry estimator now receives a spread argument — "
            "that is the step-2 behavior change, which is "
            "operator-timed. Revert or get the operator's go."
        )

    def test_both_entry_sides_stamp_spread(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        assert src.count("spread_bps_at_decision=") >= 2, (
            "one of the entry paths (buy/short) stopped stamping "
            "spread_bps_at_decision."
        )
