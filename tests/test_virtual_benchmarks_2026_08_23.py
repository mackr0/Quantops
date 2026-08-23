"""Virtual benchmarks — static null portfolios tracked without a broker.

docs/25 item 1.12 / decision D6 (2026-08-23). Pins:
  - selection is deterministic across processes (sha256 seed, not the
    per-process-salted hash() the broker path used);
  - holdings are chosen once at creation and the day-zero snapshot is
    exactly the initial capital (series starts at 0.00%);
  - mark-to-market equity = cash + Σ qty × close; dividends are
    credited once, on/after the payable date, never before ex-date
    relative to the start, and show up in cash and equity;
  - an unpriceable holding means NO snapshot for that benchmark (a
    fabricated equity is worse than a gap) and a failed count;
  - the daily entry point is idempotent per ET day;
  - the comparative-returns payload carries the benchmarks with
    negative ids and the same point shape as profiles.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import virtual_benchmarks as vb  # noqa: E402

UNIVERSE = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]


@pytest.fixture
def db(tmp_path):
    """A bare master DB for the module's own tables."""
    path = str(tmp_path / "master.db")
    conn = sqlite3.connect(path)
    vb.ensure_tables(conn)
    conn.close()
    return path


def _prices(table):
    return lambda sym: table.get(sym)


class TestSelection:
    def test_seed_is_stable_across_processes(self):
        # sha256-based: the same label must give the same seed anywhere.
        assert vb.stable_seed("BENCH-Random-01") == vb.stable_seed("BENCH-Random-01")
        assert vb.stable_seed("BENCH-Random-01") != vb.stable_seed("BENCH-Random-02")
        # Known value pins the algorithm itself.
        assert vb.stable_seed("x") == (int.from_bytes(
            __import__("hashlib").sha256(b"x").digest()[:8], "big")
            & 0x7FFF_FFFF_FFFF_FFFF)
        assert vb.stable_seed("x") < 2 ** 63   # fits SQLite INTEGER

    def test_pick_is_deterministic_and_skips_unpriced(self):
        a = vb.pick_random_symbols(7, UNIVERSE, 3)
        b = vb.pick_random_symbols(7, UNIVERSE, 3)
        assert a == b and len(a) == 3
        priced = {s: 10.0 for s in UNIVERSE}
        priced[a[0]] = None
        c = vb.pick_random_symbols(7, UNIVERSE, 3, priced=priced)
        assert a[0] not in c and len(c) == 3


class TestCreate:
    def test_buy_hold_holdings_and_day_zero_snapshot(self, db):
        bid = vb.create_benchmark(1, "BENCH-BuyHoldSPY", "buy_hold", 250_000,
                                  price_fn=_prices({"SPY": 500.0}),
                                  start_date="2026-08-24", db_path=db)
        rows = vb.list_benchmarks(1, db_path=db)
        assert rows[0]["id"] == bid
        h = rows[0]["holdings"]
        assert h == [{"symbol": "SPY", "qty": 475, "entry_price": 500.0}]
        # 475 × 500 = 237,500 spent; 12,500 cash; equity = capital exactly
        assert rows[0]["cash"] == 12_500.0
        assert vb.equity_series(bid, db_path=db) == [("2026-08-24", 250_000.0)]

    def test_random_picks_equal_weight_and_idempotent(self, db):
        prices = {s: float(10 + i) for i, s in enumerate(UNIVERSE)}
        bid = vb.create_benchmark(1, "BENCH-Random-01", "random", 100_000,
                                  price_fn=_prices(prices), universe=UNIVERSE,
                                  start_date="2026-08-24", db_path=db)
        again = vb.create_benchmark(1, "BENCH-Random-01", "random", 100_000,
                                    price_fn=_prices(prices), universe=UNIVERSE,
                                    start_date="2026-08-24", db_path=db)
        assert again == bid
        h = vb.list_benchmarks(1, db_path=db)[0]["holdings"]
        assert len(h) == vb.RANDOM_PICK_COUNT
        per_pick = 100_000 * (1 - vb.CASH_BUFFER) / vb.RANDOM_PICK_COUNT
        for item in h:
            assert item["qty"] == int(per_pick // prices[item["symbol"]])

    def test_random_draw_respects_the_universe_price_floor(self, db):
        """The AI arms may only trade names at/above the operator's
        price floor; the null must sample the same population. Sub-floor
        names are skipped deterministically (next of the same seeded
        sequence is taken)."""
        prices = {s: 50.0 for s in UNIVERSE}
        cheap = vb.pick_random_symbols(vb.stable_seed("BENCH-Random-01"),
                                       UNIVERSE, 2)
        for s in cheap:
            prices[s] = 3.0                       # below a $10 floor
        vb.create_benchmark(1, "BENCH-Random-01", "random", 100_000,
                            price_fn=_prices(prices), universe=UNIVERSE,
                            min_price=10.0, start_date="2026-08-24",
                            db_path=db)
        held = {h["symbol"] for h in vb.list_benchmarks(1, db_path=db)[0]["holdings"]}
        assert not (held & set(cheap))
        assert len(held) == vb.RANDOM_PICK_COUNT

    def test_delete_benchmarks_removes_rows_and_snapshots(self, db):
        bid = vb.create_benchmark(1, "BENCH-BuyHoldSPY", "buy_hold", 1000,
                                  price_fn=_prices({"SPY": 5.0}),
                                  start_date="2026-08-24", db_path=db)
        assert vb.equity_series(bid, db_path=db)
        assert vb.delete_benchmarks(["BENCH-BuyHoldSPY"], db_path=db) == 1
        assert vb.list_benchmarks(1, db_path=db) == []
        assert vb.equity_series(bid, db_path=db) == []

    def test_unpriceable_spy_refuses_to_create(self, db):
        with pytest.raises(RuntimeError):
            vb.create_benchmark(1, "BENCH-BuyHoldSPY", "buy_hold", 1000,
                                price_fn=_prices({}), db_path=db)
        assert vb.list_benchmarks(1, db_path=db) == []

    def test_standard_set_has_ten_random_replicas(self, db):
        prices = {s: 20.0 for s in UNIVERSE}
        prices["SPY"] = 500.0
        ids = vb.create_standard_set(1, 250_000, price_fn=_prices(prices),
                                     universe=UNIVERSE, start_date="2026-08-24",
                                     db_path=db)
        assert len(ids) == 11
        names = [b["name"] for b in vb.list_benchmarks(1, db_path=db)]
        assert names[0] == "BENCH-BuyHoldSPY"
        assert names[1:] == [f"BENCH-Random-{i:02d}" for i in range(1, 11)]
        # Replicas differ: different seeds → (almost surely) different picks
        picks = [tuple(h["symbol"] for h in b["holdings"])
                 for b in vb.list_benchmarks(1, db_path=db)[1:]]
        assert len(set(picks)) > 1


class TestMarkToMarket:
    def test_equity_is_cash_plus_marked_positions(self, db):
        bid = vb.create_benchmark(1, "BENCH-BuyHoldSPY", "buy_hold", 250_000,
                                  price_fn=_prices({"SPY": 500.0}),
                                  start_date="2026-08-24", db_path=db)
        out = vb.mark_to_market(as_of="2026-08-25", user_id=1,
                                price_fn=_prices({"SPY": 510.0}),
                                dividend_fn=lambda *a: [], db_path=db)
        assert out == {"marked": 1, "failed": 0,
                       "details": [{"name": "BENCH-BuyHoldSPY", "ok": True,
                                    "equity": 12_500 + 475 * 510.0,
                                    "credited": 0.0}]}
        assert vb.equity_series(bid, db_path=db)[-1] == (
            "2026-08-25", 12_500 + 475 * 510.0)

    def test_dividend_credited_once_on_payable_date(self, db):
        bid = vb.create_benchmark(1, "BENCH-BuyHoldSPY", "buy_hold", 250_000,
                                  price_fn=_prices({"SPY": 500.0}),
                                  start_date="2026-08-24", db_path=db)
        div = [{"symbol": "SPY", "ex_date": "2026-09-19",
                "payable_date": "2026-10-31", "rate": 1.80}]
        # Before payable date: nothing credited.
        vb.mark_to_market(as_of="2026-09-20", user_id=1,
                          price_fn=_prices({"SPY": 500.0}),
                          dividend_fn=lambda *a: div, db_path=db)
        b = vb.list_benchmarks(1, db_path=db)[0]
        assert b["cash"] == 12_500.0 and b["dividends_to_date"] == 0
        # On payable date: credited exactly once even across re-runs.
        for _ in range(2):
            vb.mark_to_market(as_of="2026-10-31", user_id=1,
                              price_fn=_prices({"SPY": 500.0}),
                              dividend_fn=lambda *a: div, db_path=db)
        b = vb.list_benchmarks(1, db_path=db)[0]
        assert b["cash"] == 12_500.0 + 475 * 1.80
        assert b["dividends_to_date"] == 475 * 1.80
        assert vb.equity_series(bid, db_path=db)[-1][1] == 250_000 + 475 * 1.80

    def test_dividend_with_ex_date_before_start_is_ignored(self, db):
        vb.create_benchmark(1, "BENCH-BuyHoldSPY", "buy_hold", 250_000,
                            price_fn=_prices({"SPY": 500.0}),
                            start_date="2026-08-24", db_path=db)
        div = [{"symbol": "SPY", "ex_date": "2026-06-19",
                "payable_date": "2026-07-31", "rate": 1.80}]
        vb.mark_to_market(as_of="2026-08-25", user_id=1,
                          price_fn=_prices({"SPY": 500.0}),
                          dividend_fn=lambda *a: div, db_path=db)
        assert vb.list_benchmarks(1, db_path=db)[0]["dividends_to_date"] == 0

    def test_unpriceable_holding_writes_no_snapshot(self, db, caplog):
        bid = vb.create_benchmark(1, "BENCH-BuyHoldSPY", "buy_hold", 250_000,
                                  price_fn=_prices({"SPY": 500.0}),
                                  start_date="2026-08-24", db_path=db)
        out = vb.mark_to_market(as_of="2026-08-25", user_id=1,
                                price_fn=_prices({}),
                                dividend_fn=lambda *a: [], db_path=db)
        assert out["marked"] == 0 and out["failed"] == 1
        assert [d for d, _ in vb.equity_series(bid, db_path=db)] == ["2026-08-24"]
        assert "NOT written" in caplog.text

    def test_run_daily_if_due_is_idempotent_per_day(self, db):
        vb.create_benchmark(1, "BENCH-BuyHoldSPY", "buy_hold", 250_000,
                            price_fn=_prices({"SPY": 500.0}),
                            start_date="2026-08-24", db_path=db)
        calls = []

        def price(sym):
            calls.append(sym)
            return 505.0

        first = vb.run_daily_if_due(1, as_of="2026-08-25", price_fn=price,
                                    dividend_fn=lambda *a: [], db_path=db)
        second = vb.run_daily_if_due(1, as_of="2026-08-25", price_fn=price,
                                     dividend_fn=lambda *a: [], db_path=db)
        assert first["marked"] == 1 and second is None
        assert calls == ["SPY"]


class TestSeries:
    def test_series_shape_matches_profiles_with_negative_ids(self, db):
        bid = vb.create_benchmark(1, "BENCH-BuyHoldSPY", "buy_hold", 250_000,
                                  price_fn=_prices({"SPY": 500.0}),
                                  start_date="2026-08-24", db_path=db)
        vb.mark_to_market(as_of="2026-08-25", user_id=1,
                          price_fn=_prices({"SPY": 505.0}),
                          dividend_fn=lambda *a: [], db_path=db)
        s = vb.list_series(1, db_path=db)
        assert len(s) == 1
        assert s[0]["profile_id"] == -bid and s[0]["virtual"] is True
        assert s[0]["strategy_type"] == "buy_hold"
        assert s[0]["points"][0] == {"date": "2026-08-24", "return_pct": 0.0}
        assert s[0]["points"][1]["date"] == "2026-08-25"
        assert s[0]["points"][1]["return_pct"] == round(
            (475 * 5.0) / 250_000 * 100, 4)

    def test_comparative_payload_includes_benchmarks(self, monkeypatch):
        import comparative_returns as cr
        monkeypatch.setattr("models.get_user_profiles", lambda uid: [])
        monkeypatch.setattr(
            "virtual_benchmarks.list_series",
            lambda uid: [{"profile_id": -1, "profile_name": "BENCH-BuyHoldSPY",
                          "strategy_type": "buy_hold", "initial_capital": 1.0,
                          "points": [{"date": "2026-08-24", "return_pct": 0.0}],
                          "virtual": True}])
        payload = cr.build_payload(1)
        assert payload["empty_state"] is False
        assert payload["series"][0]["profile_id"] == -1


class TestFetchParsing:
    def test_fetch_returns_empty_without_credentials(self, monkeypatch, caplog):
        monkeypatch.setattr("market_data._resolve_alpaca_credentials",
                            lambda: ("", "", "https://paper-api.alpaca.markets"))
        assert vb.fetch_cash_dividends("SPY", "2026-08-01", "2026-08-23") == []
        assert "no Alpaca data credentials" in caplog.text

    def test_fetch_parses_cash_dividends_only(self, monkeypatch):
        import io
        import json as _json
        monkeypatch.setattr("market_data._resolve_alpaca_credentials",
                            lambda: ("k", "s", "https://paper-api.alpaca.markets"))
        body = _json.dumps([
            {"ca_type": "dividend", "ca_sub_type": "cash", "cash": "1.8",
             "ex_date": "2026-09-19", "payable_date": "2026-10-31",
             "initiating_symbol": "SPY"},
            {"ca_type": "dividend", "ca_sub_type": "stock", "cash": "0",
             "ex_date": "2026-09-19", "initiating_symbol": "SPY"},
            {"ca_type": "split", "ex_date": "2026-09-19",
             "initiating_symbol": "SPY"},
        ]).encode()

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("urllib.request.urlopen",
                            lambda req, timeout=20: _Resp(body))
        out = vb.fetch_cash_dividends("SPY", "2026-08-01", "2026-10-31")
        assert out == [{"symbol": "SPY", "ex_date": "2026-09-19",
                        "payable_date": "2026-10-31", "rate": 1.8}]
