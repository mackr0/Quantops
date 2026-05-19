"""Pin the live-IV wiring fix for docs/18 item #1.

Before 2026-05-19: `compute_book_greeks` and `portfolio_delta_exposure`
both silently fell back to `FALLBACK_IV=0.25` when callers didn't pass
`iv_lookup`. Every prod call site qualified (views.py dashboard,
multi_scheduler risk snapshot, options_trader, options_delta_hedger,
specialists.option_spread_risk), so the risk model has been computing
delta-adjusted exposure with a flat 25% IV for every option position
since Phase 6c shipped.

After 2026-05-19: both functions auto-build a per-call cached lookup
hitting `options_oracle.get_options_oracle` when `iv_lookup` is None
and `use_live_iv=True` (default). The 25% fallback now only fires when
the live lookup genuinely fails (oracle import error, no listed
options, or zero/missing call_iv) — which is what gets surfaced by the
fallback-IV degradation alarm (docs/18 item #6).

Tests below pin:
  1. Default path uses live IV (a position with looked-up IV=0.60
     produces a different delta-adjusted exposure than the same
     position with FALLBACK_IV=0.25)
  2. use_live_iv=False preserves historical fallback behavior (for
     deterministic tests + scripts that don't want the network call)
  3. Caller-passed iv_lookup wins over auto-wired default
  4. `default_iv_lookup_factory` caches per-call: two lookups for
     the same underlying hit the oracle once
  5. Oracle failures (no options, missing IV) cache None so the
     loop doesn't re-query within a cycle
  6. `fallback_iv_count` accurately reports how many legs used the
     0.25 fallback — the metric the degradation alarm reads
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _call_position(symbol="AAPL", strike=180, expiry_days=30, qty=1):
    """OCC-formatted call position. Format is exactly 21 chars:
    6-char root (left-padded with spaces? No — the codebase's OCC
    convention is just the unpadded ticker + YYMMDD + C/P + 8-digit
    strike). The _is_option_position guard reads `len(occ) == 21 and
    occ[12] in ("C", "P")` — meaning the date occupies chars 6-11
    and the C/P sits at char 12, so the root must be exactly 6 chars
    (space-padded). We follow that contract."""
    exp = date.today() + timedelta(days=expiry_days)
    root = f"{symbol:<6}"  # left-justified to 6 chars
    occ = (f"{root}{exp.strftime('%y%m%d')}C"
           f"{int(strike * 1000):08d}")
    assert len(occ) == 21, f"OCC must be 21 chars, got {len(occ)}: {occ!r}"
    return {
        "symbol": occ,
        "qty": qty,
        "current_price": 5.00,
    }


# ---------------------------------------------------------------------------
# (1) Live IV is auto-wired by default
# ---------------------------------------------------------------------------

def test_compute_book_greeks_default_auto_wires_live_iv(monkeypatch):
    """Without an iv_lookup kw, compute_book_greeks must auto-build
    the default factory. We capture what IV the underlying
    `_greek_contribution` saw — if it's the 0.60 we stubbed, the
    live path fired; if it's 0.25 (FALLBACK_IV) the auto-wire is
    broken."""
    from options_greeks_aggregator import compute_book_greeks

    captured = {}

    def _stub_greek_contribution(parsed, spot, iv, today=None):
        captured["iv"] = iv
        return {"delta": 0.5, "gamma": 0.01, "vega": 0.10,
                "theta": -0.05, "rho": 0.01}

    monkeypatch.setattr(
        "options_greeks_aggregator._greek_contribution",
        _stub_greek_contribution,
    )
    monkeypatch.setattr(
        "options_oracle.get_options_oracle",
        lambda u: {"has_options": True, "skew": {"call_iv": 0.60}},
    )

    summary = compute_book_greeks(
        [_call_position()],
        price_lookup=lambda s: 180.0,
        # NO iv_lookup — exercises the auto-wire path
    )
    assert captured["iv"] == pytest.approx(0.60)
    assert summary["fallback_iv_count"] == 0


def test_portfolio_delta_exposure_default_auto_wires_live_iv(monkeypatch):
    from pipelines.risk.exposure import portfolio_delta_exposure
    captured = {}

    def _stub_dapv(pos, spot, iv, today):
        captured["iv"] = iv
        return 1000.0

    monkeypatch.setattr(
        "pipelines.risk.exposure.delta_adjusted_position_value",
        _stub_dapv,
    )
    monkeypatch.setattr(
        "options_oracle.get_options_oracle",
        lambda u: {"has_options": True, "skew": {"call_iv": 0.42}},
    )

    portfolio_delta_exposure(
        [_call_position()],
        price_lookup=lambda s: 180.0,
    )
    assert captured["iv"] == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# (2) use_live_iv=False preserves historical behavior
# ---------------------------------------------------------------------------

def test_compute_book_greeks_use_live_iv_false_falls_back_to_25pct(monkeypatch):
    from options_greeks_aggregator import compute_book_greeks, FALLBACK_IV
    captured = {}

    def _stub(parsed, spot, iv, today=None):
        captured["iv"] = iv
        return {"delta": 0.5, "gamma": 0.01, "vega": 0.10,
                "theta": -0.05, "rho": 0.01}

    monkeypatch.setattr(
        "options_greeks_aggregator._greek_contribution", _stub,
    )
    # If use_live_iv=False respects, no oracle call should happen.
    monkeypatch.setattr(
        "options_oracle.get_options_oracle",
        lambda u: pytest.fail("oracle was called with use_live_iv=False"),
    )
    summary = compute_book_greeks(
        [_call_position()],
        price_lookup=lambda s: 180.0,
        use_live_iv=False,
    )
    assert captured["iv"] == pytest.approx(FALLBACK_IV)  # 0.25
    assert summary["fallback_iv_count"] == 1


# ---------------------------------------------------------------------------
# (3) Caller-supplied iv_lookup wins
# ---------------------------------------------------------------------------

def test_caller_iv_lookup_overrides_default(monkeypatch):
    from options_greeks_aggregator import compute_book_greeks
    captured = {}

    def _stub(parsed, spot, iv, today=None):
        captured["iv"] = iv
        return {"delta": 0.5, "gamma": 0.01, "vega": 0.10,
                "theta": -0.05, "rho": 0.01}

    monkeypatch.setattr(
        "options_greeks_aggregator._greek_contribution", _stub,
    )
    # If caller passes their own lookup, default factory must NOT run
    # (and certainly the oracle must not be hit).
    monkeypatch.setattr(
        "options_oracle.get_options_oracle",
        lambda u: pytest.fail("oracle called despite caller-supplied lookup"),
    )
    compute_book_greeks(
        [_call_position()],
        price_lookup=lambda s: 180.0,
        iv_lookup=lambda u: 0.99,  # nonsense value to make sure it's used
    )
    assert captured["iv"] == pytest.approx(0.99)


# ---------------------------------------------------------------------------
# (4) Per-call caching
# ---------------------------------------------------------------------------

def test_default_factory_caches_per_call(monkeypatch):
    """N positions on the same underlying must hit the oracle once."""
    from options_iv_lookup import default_iv_lookup_factory
    hit_counter = {"n": 0}

    def _spy(u):
        hit_counter["n"] += 1
        return {"has_options": True, "skew": {"call_iv": 0.30}}

    monkeypatch.setattr("options_oracle.get_options_oracle", _spy)
    lookup = default_iv_lookup_factory()
    for _ in range(5):
        lookup("AAPL")
    assert hit_counter["n"] == 1


def test_default_factory_caches_failures(monkeypatch):
    """When the oracle returns no IV, the factory must remember that
    too — otherwise an underlying with no options gets re-queried for
    every contract."""
    from options_iv_lookup import default_iv_lookup_factory
    hit_counter = {"n": 0}

    def _spy(u):
        hit_counter["n"] += 1
        return {"has_options": False}

    monkeypatch.setattr("options_oracle.get_options_oracle", _spy)
    lookup = default_iv_lookup_factory()
    for _ in range(5):
        assert lookup("PRIV") is None
    assert hit_counter["n"] == 1


def test_default_factory_handles_oracle_exceptions(monkeypatch):
    """An oracle exception must not propagate — the lookup returns
    None, caches None, and subsequent calls don't re-raise."""
    from options_iv_lookup import default_iv_lookup_factory

    def _explode(u):
        raise RuntimeError("synthetic oracle failure")

    monkeypatch.setattr("options_oracle.get_options_oracle", _explode)
    lookup = default_iv_lookup_factory()
    assert lookup("AAPL") is None
    # Doesn't re-raise on second call (cached)
    assert lookup("AAPL") is None


# ---------------------------------------------------------------------------
# (5) Fallback counter tracks degradation accurately
# ---------------------------------------------------------------------------

def test_fallback_iv_count_reflects_real_oracle_failures(monkeypatch):
    """When the oracle returns valid IV for some symbols and nothing
    for others, fallback_iv_count must equal the number of failed
    lookups — that's the metric the degradation alarm consumes."""
    from options_greeks_aggregator import compute_book_greeks

    iv_map = {"AAPL": 0.40, "MSFT": 0.30}  # NVDA missing

    def _oracle(u):
        if u in iv_map:
            return {"has_options": True, "skew": {"call_iv": iv_map[u]}}
        return {"has_options": False}

    monkeypatch.setattr("options_oracle.get_options_oracle", _oracle)
    monkeypatch.setattr(
        "options_greeks_aggregator._greek_contribution",
        lambda parsed, spot, iv, today=None: {
            "delta": 0.5, "gamma": 0.01, "vega": 0.10,
            "theta": -0.05, "rho": 0.01,
        },
    )

    positions = [
        _call_position("AAPL", qty=1),
        _call_position("MSFT", qty=1),
        _call_position("NVDA", qty=1),  # missing
    ]
    summary = compute_book_greeks(
        positions, price_lookup=lambda s: 100.0,
    )
    assert summary["fallback_iv_count"] == 1
    assert summary["n_options_legs"] == 3
