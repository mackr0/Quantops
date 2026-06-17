"""2026-06-17 — universe alignment: the experiment only trades
easy-to-borrow names. Hard-to-borrow / non-shortable names
(easy_to_borrow=False — ICCM/SUGP/NEOV/SOUN/TSLG class) are excluded
because the broker rejects GTC protective brackets on them (they ride
naked) and systematic institutional funds screen them out.

Gate is enforced at TWO places:
  * run_full_screen_for_segment — filters the universe before any
    strategy / the AI sees the name;
  * execute_trade (buy + short) — backstop that also catches
    AI-PROPOSED names, which bypass the screener (ICCM came from an AI
    pick, not the screen).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _asset(sym, etb, tradable=True):
    return SimpleNamespace(symbol=sym, easy_to_borrow=etb, tradable=tradable)


@pytest.fixture(autouse=True)
def _reset_cache():
    import tradability
    tradability._CACHE["set"] = None
    tradability._CACHE["ts"] = 0.0
    yield
    tradability._CACHE["set"] = None
    tradability._CACHE["ts"] = 0.0


def _api(assets):
    api = MagicMock()
    api.list_assets.return_value = assets
    return api


class TestGate:

    def test_htb_excluded_etb_included(self):
        from tradability import is_experiment_tradable, filter_tradable
        api = _api([
            _asset("ICCM", False), _asset("SUGP", False),
            _asset("AAPL", True), _asset("MSFT", True),
        ])
        assert is_experiment_tradable(api, "ICCM") is False
        assert is_experiment_tradable(api, "SUGP") is False
        assert is_experiment_tradable(api, "AAPL") is True
        assert is_experiment_tradable(api, "MSFT") is True
        assert filter_tradable(api, ["ICCM", "AAPL", "SUGP", "MSFT"]) == ["AAPL", "MSFT"]

    def test_case_insensitive(self):
        from tradability import is_experiment_tradable
        api = _api([_asset("AAPL", True), _asset("ICCM", False)])
        assert is_experiment_tradable(api, "aapl") is True
        assert is_experiment_tradable(api, "iccm") is False

    def test_not_tradable_excluded_even_if_etb(self):
        from tradability import is_experiment_tradable
        api = _api([_asset("AAPL", True), _asset("HALT", True, tradable=False)])
        assert is_experiment_tradable(api, "HALT") is False
        assert is_experiment_tradable(api, "AAPL") is True

    def test_caches_one_broker_call(self):
        from tradability import is_experiment_tradable
        api = _api([_asset("AAPL", True), _asset("ICCM", False)])
        for _ in range(5):
            is_experiment_tradable(api, "AAPL")
            is_experiment_tradable(api, "ICCM")
        assert api.list_assets.call_count == 1, "asset set must be cached, not refetched"

    def test_fail_open_when_broker_errs_and_no_cache(self):
        from tradability import is_experiment_tradable, filter_tradable
        api = MagicMock()
        api.list_assets.side_effect = RuntimeError("alpaca down")
        # never block all entries on a broker blip with no cache to fall back on
        assert is_experiment_tradable(api, "ICCM") is True
        assert filter_tradable(api, ["ICCM", "AAPL"]) == ["ICCM", "AAPL"]

    def test_empty_set_treated_as_failure_fail_open(self):
        from tradability import is_experiment_tradable
        api = _api([])  # empty/garbage response must not nuke the whole universe
        assert is_experiment_tradable(api, "AAPL") is True

    def test_stale_cache_used_when_refresh_fails(self):
        import tradability
        from tradability import is_experiment_tradable
        api = _api([_asset("AAPL", True), _asset("ICCM", False)])
        assert is_experiment_tradable(api, "ICCM") is False  # build cache
        # expire TTL, then break the broker — must reuse the good stale set
        tradability._CACHE["ts"] = 0.0
        api.list_assets.side_effect = RuntimeError("alpaca down")
        assert is_experiment_tradable(api, "ICCM") is False
        assert is_experiment_tradable(api, "AAPL") is True


# ---------------------------------------------------------------------------
# Structural pins — the gate can't silently disappear from the trade path.
# ---------------------------------------------------------------------------


def test_execute_trade_gates_buy_and_short():
    src = (REPO / "trade_pipeline.py").read_text()
    i = src.find("def execute_trade")
    body = src[i:]
    # both the BUY block and the SELL/SHORT block must consult the gate
    assert body.count("is_experiment_tradable(api, symbol)") >= 2, (
        "execute_trade must gate BOTH new longs and new shorts on "
        "easy_to_borrow")
    assert "from tradability import is_experiment_tradable" in body


def test_screen_filters_universe():
    src = (REPO / "multi_scheduler.py").read_text()
    i = src.find("def run_full_screen_for_segment")
    end = src.find("\ndef ", i + 1)
    body = src[i:end if end > 0 else len(src)]
    assert "from tradability import filter_tradable" in body
    assert "filter_tradable(" in body, (
        "the screen must drop hard-to-borrow names before strategies/AI "
        "see them")
