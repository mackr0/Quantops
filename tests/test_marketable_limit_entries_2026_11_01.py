"""2026-11-01 — P2.2 step 2: entries are marketable limits; multi-leg
spreads never go to market; the stale-cancel task runs for every
profile; the slippage estimator sees the live spread.

Decision data (2,305 stock entries since 08-24): median adverse
slippage +1.6 bps against a 3.9 bps median spread, but p90 +26 bps and
47% of fills worse than the far touch — $43,025 of gross slippage in
ten weeks. A limit at far touch + 10 bps leaves the typical fill
unchanged and caps the tail.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, REPO)

import marketable_limit as ml  # noqa: E402


class TestMarketablePrice:
    def test_buy_pays_ask_plus_buffer_sell_accepts_bid_minus_buffer(self):
        assert ml.marketable_limit_price("buy", 99.90, 100.10) == pytest.approx(100.20, abs=0.005)
        assert ml.marketable_limit_price("sell", 99.90, 100.10) == pytest.approx(99.80, abs=0.005)
        assert ml.marketable_limit_price("short", 99.90, 100.10) == pytest.approx(99.80, abs=0.005)
        assert ml.marketable_limit_price("cover", 99.90, 100.10) == pytest.approx(100.20, abs=0.005)

    def test_sub_dollar_keeps_four_decimals(self):
        assert ml.marketable_limit_price("buy", 0.50, 0.51) == pytest.approx(0.5105, abs=1e-6)
        assert ml.marketable_limit_price("buy", 50.00, 50.01) == 50.06

    def test_entry_shape_from_a_two_sided_quote(self):
        e = ml.marketable_entry("XOM", "buy", 100.0,
                                nbbo={"bid": 99.95, "ask": 100.05, "spread_bps": 10.0})
        assert e["order_type"] == "limit"
        assert e["limit_price"] == pytest.approx(100.15, abs=0.005)
        assert e["spread_bps"] == 10.0 and e["buffer_bps"] == ml.MARKETABLE_BUFFER_BPS

    def test_no_or_bad_quote_means_market(self, monkeypatch):
        import market_data
        monkeypatch.setattr(market_data, "get_nbbo_spread_bps", lambda s: None)
        assert ml.marketable_entry("XOM", "buy", 100.0) is None
        assert ml.marketable_entry("XOM", "buy", 100.0, nbbo={"bid": 0, "ask": 100}) is None
        assert ml.marketable_entry("XOM", "buy", 100.0, nbbo={"bid": 101, "ask": 100}) is None

    def test_multileg_net_limit_concedes_toward_fillability(self):
        assert ml.multileg_limit_from_net(1.00) == 1.05        # debit: pay a little more
        assert ml.multileg_limit_from_net(-1.20) == -1.14      # credit: accept a little less
        assert ml.multileg_limit_from_net(0.10) == 0.12        # min two cents
        assert ml.multileg_limit_from_net(-0.01) == -0.02      # a credit never flips to a debit
        assert ml.multileg_limit_from_net(None) is None
        assert ml.multileg_limit_from_net(0) is None


class TestPipelineWiring:
    def test_buy_and_short_entries_use_the_marketable_price(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        assert 'getattr(ctx, "use_limit_orders"' not in src, "the superseded toggle must not drive execution"
        assert src.count("marketable_entry(symbol,") == 2, "BUY and SHORT entries"
        assert 'order_kwargs["limit_price"] = str(_ml["limit_price"])' in src
        assert 'order_kwargs["limit_price"] = str(_ml_s["limit_price"])' in src
        assert 'str(round(price, 2))' not in src[src.find("def execute_trade"):], (
            "no entry may still send a plain limit at the decision price")

    def test_entry_estimator_receives_the_live_spread(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        i = src.find('_est = estimate_slippage(\n                    symbol=symbol, qty=qty, side="buy"')
        assert i != -1 and 'spread_bps=_ml["spread_bps"] if _ml else None' in src[i:i + 400]

    def test_one_quote_fetch_per_entry(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        # the BUY telemetry block no longer fetches a second quote
        assert 'spread_at_decision = _ml["spread_bps"] if _ml else None' in src
        # the SHORT telemetry reuses the entry quote when it exists
        assert '_nbbo = ({"spread_bps": _ml_s["spread_bps"]} if _ml_s' in src

    def test_exits_and_covers_stay_market(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        i = src.find('side="sell",\n            type="market",')
        assert i != -1, "the SELL exit must remain a market order"
        tsrc = open(os.path.join(REPO, "trader.py")).read()
        assert tsrc.count('type="market"') >= 3, "trader.py exits/covers remain market"

    def test_stale_cancel_runs_for_every_profile(self):
        src = open(os.path.join(REPO, "multi_scheduler.py")).read()
        i = src.find("def _task_cancel_stale_orders")
        block = src[i:i + 2500]
        assert 'getattr(ctx, "use_limit_orders"' not in block
        assert "protective_ids" in block and "stale_cutoff = timedelta(minutes=5)" in block

    def test_multileg_never_market(self):
        src = open(os.path.join(REPO, "options_multileg.py")).read()
        i = src.find("multi-leg spreads NEVER go to market")
        assert i != -1
        assert "multileg_limit_from_net(" in src[i:i + 800]
        assert 'result["action"] = "SKIP"' in src[i:i + 1200]


class TestMultilegSkipsWithoutNet:
    def test_combo_skips_rather_than_market(self, monkeypatch):
        """Drive execute_multileg_strategy with a spec that has no net
        premium and no limit: it must SKIP, never POST a market combo."""
        import options_multileg as om
        posted = []
        monkeypatch.setattr(om, "_submit_alpaca_order_raw",
                            lambda api, payload: posted.append(payload) or (_ for _ in ()).throw(AssertionError("must not submit")))
        leg = om.OptionLeg(occ_symbol="CDE261127P00015000", underlying="CDE", expiry="2026-11-27",
                           strike=15.0, right="P", side="sell", qty=1)
        leg2 = om.OptionLeg(occ_symbol="CDE261127P00014000", underlying="CDE", expiry="2026-11-27",
                            strike=14.0, right="P", side="buy", qty=1)
        spec = om.OptionStrategy(name="bull_put", underlying="CDE", expiry="2026-11-27",
                                 legs=[leg, leg2], qty=1, spread_width_points=1.0,
                                 is_credit=True, thesis="t", net_premium_per_contract=None)
        class _Ctx:
            db_path = None; profile_id = 1; display_name = "T"; alpaca_account_id = 1
        res = om.execute_multileg_strategy(object(), spec, ctx=_Ctx(), log=False, limit_price=None)
        assert res["action"] == "SKIP" and "never go to market" in res["reason"]
        assert posted == []
