"""Option positions in `get_virtual_positions` must be tracked
separately from any stock holding on the same underlying, and the
unrealized P&L / % must use the option contract's actual current
premium — not the underlying stock's price.

Caught 2026-05-08: an MSFT bull_put_spread leg was showing
"+13332.9%" on the dashboard because the FIFO grouped it under
"MSFT" (the underlying) and the price_fetcher returned the stock
price ($416) compared to the entry premium ($3.10). Math: (416 -
3.10) / 3.10 = 133.32 = +13,332%. Nonsense.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def opt_db(monkeypatch):
    """A trades table that includes occ_symbol so option legs can
    be tracked separately."""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    path = os.path.join(tmpdir, "opt.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            pnl REAL,
            status TEXT DEFAULT 'open',
            occ_symbol TEXT,
            option_strategy TEXT,
            expiry TEXT,
            strike REAL
        )
    """)
    conn.commit()
    conn.close()
    return path


def _buy_opt(db, underlying, occ, qty, premium):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price, occ_symbol) "
        "VALUES (?, 'buy', ?, ?, ?)",
        (underlying, qty, premium, occ),
    )
    conn.commit()
    conn.close()


def _buy_stock(db, symbol, qty, price):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price) "
        "VALUES (?, 'buy', ?, ?)",
        (symbol, qty, price),
    )
    conn.commit()
    conn.close()


class TestOptionPositionTracking:
    def test_option_leg_tracked_separately_from_underlying(self, opt_db):
        """A long option leg on MSFT and stock holding of MSFT must
        produce TWO distinct positions, each with the right entry
        price. Without this, FIFO combines a $3.10 premium with a
        $400 stock price and produces nonsense."""
        from journal import get_virtual_positions
        _buy_stock(opt_db, "MSFT", 10, 400.0)
        _buy_opt(opt_db, "MSFT", "MSFT  261219P00395000", 1, 3.10)
        pos = get_virtual_positions(db_path=opt_db)
        # Two positions
        assert len(pos) == 2, pos
        by_occ = {p["occ_symbol"]: p for p in pos}
        # Stock entry stays at $400
        stock = by_occ.get(None)
        assert stock is not None
        assert stock["symbol"] == "MSFT"
        assert stock["avg_entry_price"] == pytest.approx(400.0)
        assert stock["qty"] == 10
        # Option entry stays at $3.10 premium
        opt = by_occ.get("MSFT  261219P00395000")
        assert opt is not None
        assert opt["symbol"] == "MSFT"
        assert opt["avg_entry_price"] == pytest.approx(3.10)
        assert opt["qty"] == 1

    def test_option_pl_uses_option_premium_not_underlying(self, opt_db):
        """The unrealized% on an option leg must be computed from the
        option contract's actual premium, not the underlying's stock
        price. This is the fix for the +13,332% bogus % seen on
        2026-05-08."""
        from journal import get_virtual_positions

        _buy_opt(opt_db, "MSFT", "MSFT  261219P00395000", 1, 3.10)

        # Price fetcher receives the OCC symbol for option positions
        # and returns the option's current premium (e.g., $4.20).
        # If it received "MSFT" (underlying) it would return $416 and
        # produce the +13332% bug.
        seen_keys = []

        def fetcher(key):
            seen_keys.append(key)
            if key == "MSFT  261219P00395000":
                return 4.20  # option premium up modestly from $3.10
            if key == "MSFT":
                return 416.0  # underlying stock — should NOT be queried for opt
            return 0.0

        pos = get_virtual_positions(db_path=opt_db, price_fetcher=fetcher)
        assert len(pos) == 1
        opt = pos[0]
        # Fetcher was queried for the OCC, not the underlying
        assert "MSFT  261219P00395000" in seen_keys, (
            f"Price fetcher was queried with {seen_keys} — should "
            "include the OCC symbol for option positions"
        )
        # Current price = option premium (not stock price)
        assert opt["current_price"] == pytest.approx(4.20)
        # Unrealized P&L = (4.20 - 3.10) * 1 contract * 100 shares = $110
        assert opt["unrealized_pl"] == pytest.approx(110.0)
        # Unrealized% = (4.20 - 3.10) / 3.10 = +35.5% — sensible.
        assert opt["unrealized_plpc"] == pytest.approx(0.3548, rel=0.01)
        # NOT +13,332% (which was the bug)
        assert opt["unrealized_plpc"] < 1.0
        # Market value applies x100 contract multiplier
        assert opt["market_value"] == pytest.approx(420.0)

    def test_short_option_leg_pl_correct_sign(self, opt_db):
        """Short option leg (sell-to-open) gains when premium falls."""
        from journal import get_virtual_positions

        # Sell-to-open the $30 put @ $0.05
        conn = sqlite3.connect(opt_db)
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, occ_symbol) "
            "VALUES (?, 'short', ?, ?, ?)",
            ("SCHD", 3, 0.05, "SCHD  260612P00030000"),
        )
        conn.commit()
        conn.close()

        # Premium decayed to $0.02 (good for short — buy back cheaper)
        pos = get_virtual_positions(
            db_path=opt_db,
            price_fetcher=lambda k: 0.02 if "SCHD" in k and "P000" in k else 0.0,
        )
        assert len(pos) == 1
        opt = pos[0]
        assert opt["qty"] < 0  # short = negative qty
        # unrealized_pl = (entry - current) * qty * 100 = (0.05 - 0.02) * 3 * 100 = $9
        assert opt["unrealized_pl"] == pytest.approx(9.0)
        # plpc positive (we profited)
        assert opt["unrealized_plpc"] > 0

    def test_long_leg_one_sided_market_does_not_inflate_pnl(self, opt_db):
        """Caught 2026-05-08: SCHD $28 put (long, qty=3, entry $0.15)
        showed +413.3% unrealized because the price fetcher returned
        the ask ($0.77) on a market where bid=$0. A long holder
        cannot sell at $0.77 — the realistic exit is the bid (or
        $0 when there's no bid). Fix: FIFO passes side='buy' for
        longs, fetcher returns 0 instead of ask, FIFO falls back
        to entry. Result: position shows 0% unrealized (honest
        'no liquidity' mark) instead of fake +413%."""
        from journal import get_virtual_positions

        # Three contracts of the long $28 put @ $0.15 entry
        _buy_opt(opt_db, "SCHD", "SCHD260612P00028000", 3, 0.15)

        # Price fetcher receives side='buy' for the long position
        # and returns 0 (one-sided market: ask exists, bid doesn't).
        seen_calls = []

        def fetcher(key, side="buy"):
            seen_calls.append((key, side))
            return 0.0  # bid-side fallback for long with no bid

        positions = get_virtual_positions(
            db_path=opt_db, price_fetcher=fetcher,
        )
        assert len(positions) == 1
        pos = positions[0]
        # Side hint reached the fetcher
        assert seen_calls == [("SCHD260612P00028000", "buy")]
        # Price falls back to entry (no inflated +413%)
        assert pos["current_price"] == pytest.approx(0.15)
        assert pos["unrealized_pl"] == pytest.approx(0.0)
        assert pos["unrealized_plpc"] == pytest.approx(0.0)

    def test_short_leg_uses_ask_side_hint(self, opt_db):
        """Symmetric: a short option leg passes side='sell' to the
        fetcher so the conservative side (ask) is used in
        one-sided-market fallback."""
        from journal import get_virtual_positions

        # Sell-to-open the $30 put @ $0.05 entry
        conn = sqlite3.connect(opt_db)
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, occ_symbol) "
            "VALUES (?, 'short', ?, ?, ?)",
            ("SCHD", 3, 0.05, "SCHD260612P00030000"),
        )
        conn.commit()
        conn.close()

        seen_calls = []

        def fetcher(key, side="buy"):
            seen_calls.append((key, side))
            return 0.10  # current price for the short, regardless

        positions = get_virtual_positions(
            db_path=opt_db, price_fetcher=fetcher,
        )
        assert len(positions) == 1
        # Short position → fetcher called with side='sell'
        assert seen_calls == [("SCHD260612P00030000", "sell")]

    def test_two_option_legs_same_underlying_separate_positions(self, opt_db):
        """A bull_put_spread has two legs (long $28 put + short $30
        put) on the same underlying. They must show as TWO positions
        in the FIFO output, not be combined."""
        from journal import get_virtual_positions
        # Long leg: buy-to-open the $28 put
        _buy_opt(opt_db, "SCHD", "SCHD  260612P00028000", 3, 0.15)
        # Short leg: sell-to-open the $30 put
        conn = sqlite3.connect(opt_db)
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, occ_symbol) "
            "VALUES (?, 'short', ?, ?, ?)",
            ("SCHD", 3, 0.05, "SCHD  260612P00030000"),
        )
        conn.commit()
        conn.close()

        pos = get_virtual_positions(db_path=opt_db)
        assert len(pos) == 2, [p["occ_symbol"] for p in pos]
        occs = {p["occ_symbol"] for p in pos}
        assert "SCHD  260612P00028000" in occs
        assert "SCHD  260612P00030000" in occs


class TestFetchOptionPremium:
    """Pin the actual option-premium fetcher's behavior. The journal
    stores OCC with internal padding (`WMT   260612P00117000`);
    Alpaca's API returns/accepts the unpadded form
    (`WMT260612P00117000`). The fetcher must normalize before
    sending and look up by unpadded key."""

    def _mock_response(self, status, body):
        from unittest.mock import MagicMock
        r = MagicMock()
        r.status_code = status
        r.json = MagicMock(return_value=body)
        return r

    def test_padded_occ_strips_internal_whitespace_for_request(self,
                                                                monkeypatch):
        from unittest.mock import MagicMock
        captured = {}

        def fake_get(url, **kw):
            captured["url"] = url
            captured["params"] = kw.get("params") or {}
            return self._mock_response(200, {
                "snapshots": {
                    "WMT260612P00117000": {
                        "latestQuote": {"ap": 1.40, "bp": 1.30},
                        "latestTrade": {"p": 1.35},
                    },
                },
            })

        monkeypatch.setattr("requests.get", fake_get)
        from client import _fetch_option_premium
        # Padded form (as stored in journal)
        premium = _fetch_option_premium("WMT   260612P00117000")
        # Sent unpadded
        assert captured["params"]["symbols"] == "WMT260612P00117000"
        # Mid of bid/ask = 1.35
        assert premium == pytest.approx(1.35)

    def test_returns_mid_when_two_sided_quote(self, monkeypatch):
        def fake_get(url, **kw):
            return self._mock_response(200, {
                "snapshots": {
                    "WMT260612P00117000": {
                        "latestQuote": {"ap": 2.00, "bp": 1.00},
                        "latestTrade": {"p": 1.80},
                    },
                },
            })

        monkeypatch.setattr("requests.get", fake_get)
        from client import _fetch_option_premium
        # Mid = (2.00 + 1.00) / 2 = 1.50, NOT the last trade
        assert _fetch_option_premium("WMT260612P00117000") == 1.50

    def test_falls_back_to_last_trade_on_one_sided_quote(self,
                                                          monkeypatch):
        """Illiquid contract with stub bid (e.g. $0.01 / $1.40):
        the mid ($0.705) is unrepresentative; the last trade is
        more reliable."""
        def fake_get(url, **kw):
            return self._mock_response(200, {
                "snapshots": {
                    "WMT260612P00117000": {
                        # bid is stub ($0); ask alone is meaningless
                        "latestQuote": {"ap": 1.40, "bp": 0.0},
                        "latestTrade": {"p": 1.02},
                    },
                },
            })

        monkeypatch.setattr("requests.get", fake_get)
        from client import _fetch_option_premium
        assert _fetch_option_premium("WMT260612P00117000") == 1.02

    def test_falls_back_to_daily_close_when_no_quote_or_trade(self,
                                                                monkeypatch):
        def fake_get(url, **kw):
            return self._mock_response(200, {
                "snapshots": {
                    "WMT260612P00117000": {
                        "latestQuote": {"ap": 0.0, "bp": 0.0},
                        "dailyBar": {"c": 1.05},
                    },
                },
            })

        monkeypatch.setattr("requests.get", fake_get)
        from client import _fetch_option_premium
        assert _fetch_option_premium("WMT260612P00117000") == 1.05

    def test_long_position_with_one_sided_ask_returns_zero_not_ask(self,
                                                                    monkeypatch):
        """Caught 2026-05-08: a SCHD $28 put leg (long) showed
        +413% because the market was bid=$0 / ask=$0.77 and the
        fetcher returned the ask. A long holder cannot SELL at
        $0.77 (no buyer at any price); the realistic mark is $0
        or entry. Returning ask here fakes a gain that doesn't
        exist."""
        def fake_get(url, **kw):
            return self._mock_response(200, {
                "snapshots": {
                    "SCHD260612P00028000": {
                        # Stub ask, no real bid, no trade, no bar
                        "latestQuote": {"ap": 0.77, "bp": 0.0},
                    },
                },
            })

        monkeypatch.setattr("requests.get", fake_get)
        from client import _fetch_option_premium
        # Long position: must NOT use the offer side. Returns 0
        # so the FIFO falls back to entry (current=entry, 0%
        # unrealized — honest "no liquidity" mark).
        assert _fetch_option_premium(
            "SCHD260612P00028000", side="buy",
        ) == 0.0

    def test_short_position_with_one_sided_bid_returns_zero_not_bid(self,
                                                                     monkeypatch):
        """Symmetric: a short holder cannot BUY-to-close at the
        bid alone (no seller at any price). Use entry as fallback
        rather than understate the cost-to-close."""
        def fake_get(url, **kw):
            return self._mock_response(200, {
                "snapshots": {
                    "X261219C00050000": {
                        "latestQuote": {"ap": 0.0, "bp": 1.10},
                    },
                },
            })

        monkeypatch.setattr("requests.get", fake_get)
        from client import _fetch_option_premium
        # Short position: must NOT use the bid side alone.
        assert _fetch_option_premium(
            "X261219C00050000", side="sell",
        ) == 0.0

    def test_long_position_uses_bid_when_only_bid_available(self,
                                                              monkeypatch):
        """When ONLY the bid is available (ask=0), a long holder's
        realistic exit is the bid — that's the conservative
        valuation. Use it."""
        def fake_get(url, **kw):
            return self._mock_response(200, {
                "snapshots": {
                    "X261219C00050000": {
                        "latestQuote": {"ap": 0.0, "bp": 1.10},
                    },
                },
            })

        monkeypatch.setattr("requests.get", fake_get)
        from client import _fetch_option_premium
        # Long with bid available → use the bid (their exit price)
        assert _fetch_option_premium(
            "X261219C00050000", side="buy",
        ) == 1.10

    def test_short_position_uses_ask_when_only_ask_available(self,
                                                              monkeypatch):
        """Symmetric: short holder closes by buying at ask."""
        def fake_get(url, **kw):
            return self._mock_response(200, {
                "snapshots": {
                    "TECK260612P00057000": {
                        "latestQuote": {"ap": 1.62, "bp": 0.0},
                    },
                },
            })

        monkeypatch.setattr("requests.get", fake_get)
        from client import _fetch_option_premium
        assert _fetch_option_premium(
            "TECK260612P00057000", side="sell",
        ) == 1.62

    def test_returns_zero_on_missing_snapshot(self, monkeypatch):
        def fake_get(url, **kw):
            return self._mock_response(200, {"snapshots": {}})

        monkeypatch.setattr("requests.get", fake_get)
        from client import _fetch_option_premium
        # Caller (FIFO) will fall back to avg_entry — better than
        # showing a +13332% on a misread response.
        assert _fetch_option_premium("WMT260612P00117000") == 0.0

    def test_returns_zero_on_http_error(self, monkeypatch):
        def fake_get(url, **kw):
            return self._mock_response(500, {})

        monkeypatch.setattr("requests.get", fake_get)
        from client import _fetch_option_premium
        assert _fetch_option_premium("WMT260612P00117000") == 0.0


class TestOCCSymbolDetection:
    """Backing the price-fetcher's option-vs-stock routing."""

    def test_is_occ_symbol_recognizes_padded_form(self):
        """Padded form (21 chars, root padded to 6 with spaces) is
        what some internal builders produce."""
        from client import _is_occ_symbol
        assert _is_occ_symbol("MSFT  261219P00395000") is True
        assert _is_occ_symbol("AAPL  250516C00150000") is True
        # Short root (padded to 6) is also valid
        assert _is_occ_symbol("F     261219C00012000") is True

    def test_is_occ_symbol_recognizes_unpadded_form(self):
        """Unpadded form is what Alpaca's API returns and what the
        journal stores when the OCC was logged via Alpaca's response.
        Caught 2026-05-08 (WMT bull_put_spread leg showing 0% on the
        dashboard because _is_occ_symbol required exactly 21 chars
        and rejected the 18-char unpadded form, routing the price
        fetch through the stock-price path which fails)."""
        from client import _is_occ_symbol
        assert _is_occ_symbol("WMT260612P00117000") is True   # 18 chars
        assert _is_occ_symbol("MSFT261219P00395000") is True  # 19 chars
        assert _is_occ_symbol("AAPL250516C00150000") is True
        # Short root unpadded
        assert _is_occ_symbol("F261219C00012000") is True     # 16 chars
        assert _is_occ_symbol("X261219P00050000") is True     # 16 chars

    def test_is_occ_symbol_rejects_stock_tickers(self):
        from client import _is_occ_symbol
        assert _is_occ_symbol("AAPL") is False
        assert _is_occ_symbol("MSFT") is False
        assert _is_occ_symbol("BRK.B") is False
        assert _is_occ_symbol("") is False
        assert _is_occ_symbol(None) is False

    def test_is_occ_symbol_rejects_wrong_shape(self):
        from client import _is_occ_symbol
        # 21 chars but no C/P at the right slot
        assert _is_occ_symbol("AAAAAAAAAAAAA12345678") is False
        # Trailing chars not all digits
        assert _is_occ_symbol("AAAAAA261219Cabc12345") is False
        # Date portion not digits
        assert _is_occ_symbol("MSFTabcdefP00395000") is False
        # Too long
        assert _is_occ_symbol("EXTRAEXTRA261219C00050000") is False
