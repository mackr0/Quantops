"""Option-book Greeks are computed off the UNDERLYING spot, not the option
premium (2026-06-30).

`compute_book_greeks` falls back to a leg's own `current_price` as the spot
when no `price_lookup` is supplied — but for an option that field is the
PREMIUM (e.g. $3), not the underlying spot (e.g. $150), so the resulting
delta/gamma/vega/theta are nonsense. The two production callers that hold an
option book — `option_spread_risk` (a VETO specialist that judges proposals
against the book's net Greeks) and the `multi_scheduler` risk snapshot — were
calling it with no price_lookup. Both now pass `make_underlying_spot_lookup()`,
an ALPACA-FIRST (market_data.get_bars) underlying-spot lookup.

This file pins:
- HELPER: returns the latest close, memoizes per instance, fail-soft → None.
- PROOF: real spot vs the premium fallback yields materially different Greeks.
- WIRING: option_spread_risk passes a real spot lookup (no premium fallback).
"""
from __future__ import annotations

import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import options_greeks_aggregator as oga


def _future_call_occ(root="AAPL", strike_int=150):
    # 6-char root + YYMMDD (future) + C + 8-digit strike(×1000)
    return root.ljust(6) + "261218" + "C" + ("%08d" % (strike_int * 1000))


# ---------------------------------------------------------------------------
# make_underlying_spot_lookup
# ---------------------------------------------------------------------------

def test_helper_returns_latest_close_and_memoizes(monkeypatch):
    import pandas as pd
    calls = []

    def fake_get_bars(sym, timeframe="1Day", limit=200, api=None):
        calls.append(sym)
        return pd.DataFrame({"close": [100.0, 123.5]})

    monkeypatch.setattr("market_data.get_bars", fake_get_bars)
    lookup = oga.make_underlying_spot_lookup()
    assert lookup("NVDA") == 123.5          # latest close, not the first bar
    assert lookup("NVDA") == 123.5
    assert calls.count("NVDA") == 1          # memoized: one fetch per underlying


def test_helper_failsoft_on_error(monkeypatch):
    def boom(sym, **kw):
        raise RuntimeError("no data")

    monkeypatch.setattr("market_data.get_bars", boom)
    lookup = oga.make_underlying_spot_lookup()
    assert lookup("NVDA") is None


def test_helper_failsoft_on_empty_bars(monkeypatch):
    import pandas as pd
    monkeypatch.setattr("market_data.get_bars",
                        lambda *a, **k: pd.DataFrame({"close": []}))
    lookup = oga.make_underlying_spot_lookup()
    assert lookup("NVDA") is None


# ---------------------------------------------------------------------------
# The bug, demonstrated: premium-as-spot vs real spot
# ---------------------------------------------------------------------------

def test_real_spot_changes_greeks_vs_premium_fallback():
    """A 150-strike call: priced at the $3 premium it reads deep-OTM
    (~0 delta); priced at the real $150 spot it's ATM (~0.5 delta).
    The fix must produce the latter."""
    occ = _future_call_occ("AAPL", 150)
    leg = {"symbol": occ, "occ_symbol": occ, "qty": 1, "current_price": 3.0}
    today = date(2026, 6, 30)

    premium = oga.compute_book_greeks(
        [leg], iv_lookup=lambda s: 0.30, today=today)               # no spot lookup → premium fallback
    real = oga.compute_book_greeks(
        [leg], price_lookup=lambda s: 150.0, iv_lookup=lambda s: 0.30,
        today=today)

    assert premium["n_options_legs"] == 1
    assert real["n_options_legs"] == 1
    # ATM call delta (~+50 share-equiv) must dwarf the deep-OTM premium read.
    assert abs(real["net_delta"]) > abs(premium["net_delta"]) + 10.0


# ---------------------------------------------------------------------------
# Wiring: option_spread_risk passes a real spot lookup
# ---------------------------------------------------------------------------

def test_option_spread_risk_passes_spot_lookup(monkeypatch):
    from specialists import option_spread_risk
    captured = {}

    def fake_cbg(positions, price_lookup=None, **kw):
        captured["price_lookup"] = price_lookup
        return {"n_options_legs": 1, "net_delta": 1.0, "net_gamma": 0.0,
                "net_vega": 0.0, "net_theta": 0.0}

    monkeypatch.setattr("pipelines.risk.compute_book_greeks", fake_cbg)
    monkeypatch.setattr(
        "specialists.option_spread_risk._current_positions",
        lambda ctx: [{"symbol": "AAPL", "occ_symbol": None,
                      "qty": 100, "current_price": 150.0}],
    )
    ctx = SimpleNamespace(max_per_trade_loss=500.0)
    option_spread_risk.build_prompt(
        [{"symbol": "CWAN", "iv_rank": 65, "dte": 32, "spread_max_loss": 230}],
        ctx)

    assert callable(captured.get("price_lookup")), (
        "option_spread_risk must pass an underlying-spot price_lookup, not "
        "rely on the premium (current_price) fallback")
