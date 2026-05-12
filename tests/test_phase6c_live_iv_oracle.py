"""Phase 6c — live IV oracle in delta-adjusted exposure (2026-05-12).

Phase 6b wired delta-adjusted portfolio exposure but used a hard
fallback IV of 0.25 for every option position regardless of the
underlying's actual volatility. A name trading at 60% IV near
earnings was scored with the same delta sensitivity as a quiet
name at 15%.

Phase 6c wires `options_oracle.get_options_oracle` so each
position picks up its underlying's live ATM IV. Per-call caching
prevents repeated chain fetches when multiple positions share an
underlying.

This file pins:
- LIVE LOOKUP: when use_live_iv=True (default) and no explicit
  iv_lookup is passed, the live oracle is invoked.
- CACHING: multiple calls within the same lookup invocation only
  hit the oracle once per underlying.
- FALLBACK: oracle returning None → exposure math falls back to
  FALLBACK_IV.
- ORACLE FAILURE: exception → fallback (failure-tolerant).
- USE_LIVE_IV=FALSE: legacy callers that want the pre-Phase-6c
  fallback behavior can opt out.
- EXPLICIT IV_LOOKUP WINS: when caller passes its own iv_lookup,
  the live oracle is NOT invoked (caller is in control).
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines.risk import exposure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _occ(underlying="CWAN", expiry_days=32, strike=50.0, right="C"):
    today = date.today()
    expiry = today + timedelta(days=expiry_days)
    yymmdd = expiry.strftime("%y%m%d")
    strike_str = f"{int(round(strike * 1000)):08d}"
    root = underlying.ljust(6)
    return f"{root}{yymmdd}{right}{strike_str}"


def _call(underlying="CWAN", qty=1, strike=50.0, current_price=2.40):
    sym = _occ(underlying, strike=strike)
    return {
        "symbol": sym, "occ_symbol": sym, "qty": qty,
        "current_price": current_price,
    }


# ---------------------------------------------------------------------------
# LIVE LOOKUP factory
# ---------------------------------------------------------------------------

class TestDefaultIVLookupFactory:
    def test_returns_live_iv_when_oracle_has_data(self):
        with patch(
            "options_oracle.get_options_oracle",
            return_value={"has_options": True,
                           "skew": {"call_iv": 0.42}},
        ):
            lookup = exposure._default_iv_lookup_factory()
            assert lookup("CWAN") == pytest.approx(0.42)

    def test_returns_none_when_no_chain(self):
        with patch(
            "options_oracle.get_options_oracle",
            return_value={"has_options": False},
        ):
            lookup = exposure._default_iv_lookup_factory()
            assert lookup("PENNYSTOCK") is None

    def test_returns_none_when_skew_iv_zero(self):
        with patch(
            "options_oracle.get_options_oracle",
            return_value={"has_options": True,
                           "skew": {"call_iv": 0.0}},
        ):
            lookup = exposure._default_iv_lookup_factory()
            assert lookup("CWAN") is None

    def test_returns_none_on_oracle_exception(self):
        with patch(
            "options_oracle.get_options_oracle",
            side_effect=RuntimeError("network down"),
        ):
            lookup = exposure._default_iv_lookup_factory()
            assert lookup("CWAN") is None

    def test_caches_per_underlying(self):
        """Multiple lookups of the same underlying hit the oracle
        ONCE — important for large books with many positions on
        the same name."""
        call_count = {"n": 0}

        def fake_oracle(symbol):
            call_count["n"] += 1
            return {"has_options": True, "skew": {"call_iv": 0.30}}

        with patch(
            "options_oracle.get_options_oracle",
            side_effect=fake_oracle,
        ):
            lookup = exposure._default_iv_lookup_factory()
            iv1 = lookup("AAPL")
            iv2 = lookup("AAPL")
            iv3 = lookup("AAPL")
        assert iv1 == iv2 == iv3 == pytest.approx(0.30)
        assert call_count["n"] == 1, (
            f"Expected 1 oracle call (cached), got {call_count['n']}"
        )

    def test_empty_underlying_returns_none(self):
        lookup = exposure._default_iv_lookup_factory()
        assert lookup("") is None


# ---------------------------------------------------------------------------
# effective_positions_for_risk_model — wires live IV by default
# ---------------------------------------------------------------------------

class TestEffectivePositionsLiveIV:
    def test_use_live_iv_default_true_invokes_oracle(self):
        positions = [_call(underlying="CWAN")]
        prices = {"CWAN": 50.0}
        oracle_calls = {"n": 0}

        def fake_oracle(symbol):
            oracle_calls["n"] += 1
            return {"has_options": True, "skew": {"call_iv": 0.45}}

        with patch(
            "options_oracle.get_options_oracle",
            side_effect=fake_oracle,
        ):
            eff = exposure.effective_positions_for_risk_model(
                positions, price_lookup=prices.get,
            )

        assert oracle_calls["n"] >= 1, (
            "use_live_iv=True (default) must trigger oracle call"
        )
        # Position picked up → effective entry exists
        assert len(eff) == 1
        assert eff[0]["symbol"] == "CWAN"

    def test_use_live_iv_false_skips_oracle(self):
        """Legacy callers can opt out — sometimes test isolation
        or batch jobs don't want network calls."""
        positions = [_call(underlying="CWAN")]
        prices = {"CWAN": 50.0}
        oracle_calls = {"n": 0}

        def fake_oracle(symbol):
            oracle_calls["n"] += 1
            return {"has_options": True, "skew": {"call_iv": 0.45}}

        with patch(
            "options_oracle.get_options_oracle",
            side_effect=fake_oracle,
        ):
            exposure.effective_positions_for_risk_model(
                positions, price_lookup=prices.get,
                use_live_iv=False,
            )

        assert oracle_calls["n"] == 0, (
            "use_live_iv=False must NOT invoke the oracle"
        )

    def test_explicit_iv_lookup_wins(self):
        """When caller passes its own iv_lookup, the live oracle
        is not invoked. Caller is in control."""
        positions = [_call(underlying="CWAN")]
        prices = {"CWAN": 50.0}
        oracle_calls = {"n": 0}
        explicit_iv_calls = {"n": 0}

        def fake_oracle(symbol):
            oracle_calls["n"] += 1
            return {"has_options": True, "skew": {"call_iv": 0.99}}

        def explicit_iv(symbol):
            explicit_iv_calls["n"] += 1
            return 0.20

        with patch(
            "options_oracle.get_options_oracle",
            side_effect=fake_oracle,
        ):
            exposure.effective_positions_for_risk_model(
                positions, price_lookup=prices.get,
                iv_lookup=explicit_iv, use_live_iv=True,
            )

        assert oracle_calls["n"] == 0
        assert explicit_iv_calls["n"] >= 1


# ---------------------------------------------------------------------------
# Live IV affects delta-equivalent exposure values
# ---------------------------------------------------------------------------

class TestLiveIVChangesExposure:
    def test_otm_call_iv_changes_exposure_substantially(self):
        """For an OTM call, delta is highly sensitive to IV — a
        long OTM call at 25% IV has tiny delta (~0.1) but at 60% IV
        gets meaningful delta (~0.3+). This confirms the live
        oracle IV is feeding into the Greeks calc, not just the
        fallback constant."""
        # OTM call (spot=50, strike=55 → 10% OTM, delta sensitive
        # to IV but not deep-OTM-floor).
        positions = [_call(underlying="CWAN", strike=55.0)]
        prices = {"CWAN": 50.0}

        eff_fallback = exposure.effective_positions_for_risk_model(
            positions, price_lookup=prices.get, use_live_iv=False,
        )
        mv_fallback = eff_fallback[0]["market_value"]

        with patch(
            "options_oracle.get_options_oracle",
            return_value={"has_options": True,
                           "skew": {"call_iv": 0.60}},
        ):
            eff_live = exposure.effective_positions_for_risk_model(
                positions, price_lookup=prices.get,
            )
        mv_live = eff_live[0]["market_value"]

        # OTM with high IV produces materially more delta-eq
        # exposure than OTM at low IV. Expect 2x+ difference.
        assert mv_live > mv_fallback * 1.5, (
            f"Live IV (0.60) produced {mv_live:.0f} but fallback "
            f"(0.25) produced {mv_fallback:.0f} — for an OTM call, "
            f"the higher-IV exposure should be at least 1.5x the "
            f"fallback. The live oracle isn't being threaded through."
        )
