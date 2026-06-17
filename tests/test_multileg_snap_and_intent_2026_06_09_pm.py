"""2026-06-09 (afternoon) — two follow-up multileg fixes.

After the morning's catastrophic-floor + leg-ordering deploys, two
real bugs remained in the trade_drops feed:

  1. BITO P 7.5 exp 2026-07-17 — `snap_to_listed_contract` rejected
     with "No listed Alpaca contract within tolerance". BITO's chain
     spaces strikes $1 apart (1, 2, 3, …, 21); the nearest listed to
     a $7.5 target is $7 or $8, both 6.67% off — outside the old
     fixed 5% tolerance. AI's thesis is valid for either neighbor;
     the half-strike snap should be accepted.

  2. NOK leg-0 ERROR — `Alpaca order rejected (422): position intent
     mismatch, inferred: sell_to_close, specified: sell_to_open`.
     The broker already held a long position on that OCC; our
     local-journal duplicate-position guard missed it (journal
     drift from broker state). Currently surfaces as a red ERROR
     in the AI Brain. Reclassify as SKIP with a clear "already-
     positioned at broker" reason so the operator sees an
     unambiguous skip-not-bug badge.

Fixes pinned:

  - `options_chain_alpaca.snap_to_listed_contract` uses a grid-
    aware strike tolerance: `max(target_strike * 0.05,
    median_spacing * 0.5)`. A half-strike snap is always accepted.

  - `options_multileg.execute_multileg_strategy` detects
    "position intent mismatch" in the exception text (the Alpaca
    422 code `42210000` is shared with the duplicate-leg error so
    matching on the code alone over-triggers) in BOTH the combo
    path and the sequential-leg-loop path, returns `action="SKIP"`
    with the broker-state reason, and rolls back any opened legs.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)


EXPIRY = date(2099, 1, 16)


# ---------------------------------------------------------------------------
# Layer 1 — snap_to_listed_contract uses grid-aware strike tolerance
# ---------------------------------------------------------------------------


class TestSnapGridAwareTolerance:

    def _chain(self, strikes, expiry_iso=EXPIRY.isoformat()):
        """Build a fake Alpaca contracts list at one expiry."""
        return [
            {
                "symbol": f"FAKE{i}",
                "expiration_date": expiry_iso,
                "type": "put",
                "strike": float(s),
            }
            for i, s in enumerate(strikes)
        ]

    def test_bito_75_snaps_to_7_or_8(self):
        """BITO reproduction: target $7.5 on a $1-spaced chain.
        Nearest listed = $7 or $8 (both 6.67% off). The old fixed
        5% tolerance refused. New grid-aware tolerance allows
        half-strike snaps."""
        from options_chain_alpaca import snap_to_listed_contract
        chain = self._chain([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
        result = snap_to_listed_contract(
            "BITO", EXPIRY.isoformat(), 7.5, "P", contracts=chain,
        )
        assert result is not None, (
            "Half-strike snap (target $7.5 against $1-spaced chain) "
            "must succeed — both $7 and $8 are within half-grid"
        )
        assert result["strike"] in (7.0, 8.0)

    def test_far_off_snap_still_refuses(self):
        """Snap to nearest is only accepted when within half the
        median strike spacing. A target FAR from any listed must
        still refuse — the AI proposed something the chain doesn't
        support."""
        from options_chain_alpaca import snap_to_listed_contract
        # Chain has only $50 and $100 — wide gap. Target $75 is
        # halfway between, distance $25 = 50% of strike, much
        # bigger than median_spacing/2 = $25.
        chain = self._chain([50.0, 100.0])
        result = snap_to_listed_contract(
            "FAR", EXPIRY.isoformat(), 75.0, "P", contracts=chain,
        )
        # $25 difference vs median_spacing $50 → tol = max($3.75,
        # $25) = $25. $25 NOT > $25 → snap allowed (boundary).
        # This is the edge of the policy. Move target further to
        # actually break tolerance.
        result_far = snap_to_listed_contract(
            "FAR", EXPIRY.isoformat(), 200.0, "P", contracts=chain,
        )
        assert result_far is None, (
            "A target $200 against {50, 100} should refuse — "
            "$100 vs target $200 is way outside half-grid ($25)"
        )

    def test_high_priced_strike_still_uses_5pct(self):
        """For high-priced underlyings where strike spacing is
        small relative to strike (e.g., NVDA $500 strikes spaced
        $5 apart), the 5% floor dominates and old behavior is
        preserved. A target $505 against $500/$510 should snap."""
        from options_chain_alpaca import snap_to_listed_contract
        chain = self._chain([490, 495, 500, 505, 510, 515])
        result = snap_to_listed_contract(
            "NVDA", EXPIRY.isoformat(), 505.0, "P", contracts=chain,
        )
        assert result is not None
        assert result["strike"] == 505.0

    def test_strike_75_dollars_off_a_70_chain_refuses(self):
        """Sanity: targeting a $1000 strike against a $50-spaced
        chain refuses (5% of $1000 = $50, median spacing/2 = $25;
        the diff to nearest $1100 is $100 — far outside both)."""
        from options_chain_alpaca import snap_to_listed_contract
        chain = self._chain([1100, 1150, 1200, 1250])
        result = snap_to_listed_contract(
            "AAA", EXPIRY.isoformat(), 1000.0, "P", contracts=chain,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Layer 2 — position-intent mismatch becomes SKIP, not ERROR
# ---------------------------------------------------------------------------


def _stub_order(order_id="ord-x"):
    o = MagicMock()
    o.id = order_id
    return o


def _bear_call_spread():
    from options_multileg import build_bear_call_spread
    return build_bear_call_spread("NOK", EXPIRY, 15, 17, qty=1)


class TestPositionIntentMismatchIsSkip:

    def test_combo_position_intent_returns_skip(self):
        """When the combo path fails with position-intent-mismatch,
        the result must be SKIP (not ERROR) and sequential fallback
        must not run (it would hit the same broker state)."""
        from options_multileg import execute_multileg_strategy
        strategy = _bear_call_spread()
        seq_called = [False]

        def _seq(api, payload):
            seq_called[0] = True
            return _stub_order()

        def _pim_combo(api, payload, **kwargs):
            raise RuntimeError(
                'Alpaca order rejected (422): {"code":42210000,'
                '"message":"position intent mismatch, inferred: '
                'sell_to_close, specified: sell_to_open"}'
            )

        api = MagicMock()
        with patch(
            "options_multileg._combo_submit_with_retry",
            side_effect=_pim_combo,
        ), patch(
            "options_multileg._submit_alpaca_order_raw",
            side_effect=_seq,
        ), patch(
            "options_chain_alpaca.list_available_contracts",
            return_value=[],
        ):
            ctx = SimpleNamespace(db_path=None)
            result = execute_multileg_strategy(
                api, strategy, ctx=ctx, log=False, use_combo=True,
            )

        assert seq_called[0] is False, (
            "Combo position-intent failure must NOT fall through "
            "to sequential — the same broker state would reject "
            "again and produce an ERROR badge."
        )
        assert result["action"] == "SKIP"
        # 2026-06-17 — reason reworded from "already-positioned / journal
        # drifted" to the accurate shared-account position-intent cause.
        _r = result["reason"].lower()
        assert ("collision" in _r or "position_intent" in _r
                or "shared-account" in _r or "position-intent" in _r), \
            result["reason"]

    def test_sequential_position_intent_returns_skip(self):
        """When sequential leg N fails with position-intent-mismatch,
        return SKIP (not ERROR). Rollback any earlier legs."""
        from options_multileg import execute_multileg_strategy
        strategy = _bear_call_spread()

        call_count = [0]

        def _seq_failing_on_leg_0(api, payload):
            call_count[0] += 1
            # Leg 0 (the BUY after buys-first reordering) succeeds;
            # leg 1 (the SELL) hits position-intent mismatch
            if payload.get("side") == "sell":
                raise RuntimeError(
                    'Alpaca order rejected (422): {"code":42210000,'
                    '"message":"position intent mismatch, inferred: '
                    'sell_to_close, specified: sell_to_open"}'
                )
            return _stub_order()

        def _failing_combo(api, payload, **kwargs):
            # Force fallback (so we test the sequential path)
            raise RuntimeError("transient 503")

        api = MagicMock()
        with patch(
            "options_multileg._combo_submit_with_retry",
            side_effect=_failing_combo,
        ), patch(
            "options_multileg._submit_alpaca_order_raw",
            side_effect=_seq_failing_on_leg_0,
        ), patch(
            "options_chain_alpaca.list_available_contracts",
            return_value=[],
        ):
            ctx = SimpleNamespace(db_path=None)
            result = execute_multileg_strategy(
                api, strategy, ctx=ctx, log=False, use_combo=True,
            )

        assert result["action"] == "SKIP", (
            f"Position-intent mismatch in sequential path must be "
            f"SKIP. Got {result['action']}: {result['reason']}"
        )
        # The successfully-opened buy leg should have been rolled
        # back: at least 3 submit calls (buy + sell-that-failed +
        # rollback-of-buy)
        assert call_count[0] >= 3, (
            "Rollback must run after the position-intent skip: "
            "the long leg was opened and must be closed"
        )

    def test_non_position_intent_failure_still_errors(self):
        """Sanity: a different sequential failure (e.g., asset not
        tradable) still returns ERROR. The SKIP classification is
        specific to position-intent-mismatch."""
        from options_multileg import execute_multileg_strategy
        strategy = _bear_call_spread()

        def _seq_generic_error(api, payload):
            raise RuntimeError("Alpaca order rejected (404): asset not found")

        def _failing_combo(api, payload, **kwargs):
            raise RuntimeError("transient 503")

        api = MagicMock()
        with patch(
            "options_multileg._combo_submit_with_retry",
            side_effect=_failing_combo,
        ), patch(
            "options_multileg._submit_alpaca_order_raw",
            side_effect=_seq_generic_error,
        ), patch(
            "options_chain_alpaca.list_available_contracts",
            return_value=[],
        ):
            ctx = SimpleNamespace(db_path=None)
            result = execute_multileg_strategy(
                api, strategy, ctx=ctx, log=False, use_combo=True,
            )

        assert result["action"] == "ERROR"
