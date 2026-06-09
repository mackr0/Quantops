"""2026-06-09 — sequential multileg fallback safety.

Pre-fix: when the atomic MLEG combo failed (transient 5xx or a
permanent client error like duplicate-strike snap collapse),
`execute_multileg_strategy` fell through to a sequential per-leg
submission. The sequential path submitted legs in the strategy's
`legs[]` order. Credit-spread builders (bull_put_spread,
bear_call_spread, iron_condor) emit shorts FIRST in `legs[]` so the
journal rows display the credit-receiving leg first. When that
short leg was submitted alone, Alpaca saw an uncovered short →
403 `account not eligible to trade uncovered option`. The
operator-visible drop badge read "Catastrophic / ERROR" with the
uncovered-short reason — but the real cause was leg ordering, not
account eligibility.

Today's NOK reproduction (pid 41 14:12:16): a bear_call_spread on
NOK had both legs snap to NOK260717C00015000 (strike picker /
snapper bug). Combo rejected with `invalid legs: leg.1 symbol is
duplicated`. Sequential fallback ran: leg 0 = sell-to-open
(short call); Alpaca rejected as uncovered. The drop reason said
"uncovered" — operator's first impression was an Alpaca approval-
level mismatch; actual root cause was a duplicate-strike upstream.

Post-fix:

  1. Sequential fallback sorts legs `buy` first, `sell` last. For
     a vertical spread that means the long leg fills before the
     short leg is submitted — Alpaca sees the short as covered and
     accepts. Stable sort so intra-side ordering (which matters
     for iron condor wing matching) is preserved.

  2. Combo rejections that include "duplicated" / "duplicate" in
     the error string skip the sequential fallback entirely.
     Sequential CAN'T fix duplicate legs — it would either net the
     spread to zero (longs-first, second leg as sell_to_close) or
     re-trigger uncovered (shorts-first). Refusing fallback
     surfaces the real cause in trade_drops instead of a
     misleading downstream symptom.

Contract pinned at three levels:

  - `_execute_sequential_legs` (the sorted-leg pass) submits buys
    before sells.
  - Combo failure with "duplicated" never enters the sequential
    path.
  - Rollback unchanged: iterates the `submitted` list in
    submission order (no leg lost).
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
# Layer 1 — sequential leg ordering puts buys before sells
# ---------------------------------------------------------------------------


def _stub_order(order_id="ord-123"):
    o = MagicMock()
    o.id = order_id
    return o


def _bear_call_spread():
    """Credit spread: short lower-strike call + long higher-strike
    call. Builder emits short FIRST (credit-spread convention)."""
    from options_multileg import build_bear_call_spread
    return build_bear_call_spread("NOK", EXPIRY, 15, 17, qty=1)


def _bull_put_spread():
    """Credit spread: short higher-strike put + long lower-strike
    put. Builder emits short FIRST."""
    from options_multileg import build_bull_put_spread
    return build_bull_put_spread("NU", EXPIRY, 10, 12, qty=1)


class TestSequentialFallbackLegOrdering:

    def _run_with_failing_combo(self, strategy):
        """Run execute_multileg_strategy with combo path forced to
        fail (transient 5xx-style), then capture every order payload
        the sequential path submits."""
        from options_multileg import execute_multileg_strategy

        submitted_payloads: list[dict] = []

        def _fake_raw(api, payload):
            submitted_payloads.append(dict(payload))
            return _stub_order()

        # Force combo to fail (non-duplicate error so fallback runs)
        def _failing_combo(api, payload, **kwargs):
            raise RuntimeError("transient 503")

        api = MagicMock()
        with patch(
            "options_multileg._combo_submit_with_retry",
            side_effect=_failing_combo,
        ), patch(
            "options_multileg._submit_alpaca_order_raw",
            side_effect=_fake_raw,
        ), patch(
            "options_chain_alpaca.list_available_contracts",
            return_value=[],
        ):
            ctx = SimpleNamespace(db_path=None)
            result = execute_multileg_strategy(
                api, strategy, ctx=ctx, log=False, use_combo=True,
            )
        return result, submitted_payloads

    def test_bear_call_spread_buys_submitted_before_sells(self):
        """Credit spread: builder emits [short, long]. Sequential
        fallback must reorder to [long, short] so the long leg is
        open at the broker before the short hits."""
        strategy = _bear_call_spread()
        # Sanity: builder really does emit short-first
        assert strategy.legs[0].side == "sell", (
            "Test premise broken — builder no longer emits short-"
            "first; revisit this test"
        )
        result, payloads = self._run_with_failing_combo(strategy)
        assert result["action"] == "MULTILEG_OPEN", (
            f"Sequential fallback should succeed with stubbed raw "
            f"submits; got {result['action']}: {result.get('reason')}"
        )
        # The order of submission must be longs first
        sides_in_submit_order = [p["side"] for p in payloads]
        assert sides_in_submit_order == ["buy", "sell"], (
            f"Sequential submission order must be buy-then-sell to "
            f"avoid the uncovered-short rejection. Got: "
            f"{sides_in_submit_order}"
        )

    def test_bull_put_spread_buys_submitted_before_sells(self):
        strategy = _bull_put_spread()
        assert strategy.legs[0].side == "sell"
        result, payloads = self._run_with_failing_combo(strategy)
        assert result["action"] == "MULTILEG_OPEN"
        sides = [p["side"] for p in payloads]
        assert sides == ["buy", "sell"]

    def test_iron_condor_all_buys_before_all_sells(self):
        """Iron condor has 4 legs: 2 shorts + 2 longs. Sequential
        order must be [long, long, short, short] — every long open
        before any short is submitted."""
        from options_multileg import build_iron_condor
        strategy = build_iron_condor(
            "AAPL", EXPIRY, 140, 145, 160, 165, qty=1,
        )
        # Builder emits shorts-first by convention
        assert strategy.legs[0].side == "sell"
        assert strategy.legs[1].side == "sell"
        result, payloads = self._run_with_failing_combo(strategy)
        assert result["action"] == "MULTILEG_OPEN"
        sides = [p["side"] for p in payloads]
        # Two buys, then two sells — no interleaving
        assert sides == ["buy", "buy", "sell", "sell"], (
            f"Iron condor sequential order must group all buys "
            f"first, then all sells. Got: {sides}"
        )

    def test_debit_spread_ordering_unchanged_when_already_buy_first(self):
        """Debit spread (bull_call_spread): builder already emits
        the long leg first. Sorting must be a no-op — preserves
        the existing correct order."""
        from options_multileg import build_bull_call_spread
        strategy = build_bull_call_spread("AAPL", EXPIRY, 150, 160, qty=1)
        # Builder emits long-first for debit spreads
        assert strategy.legs[0].side == "buy"
        assert strategy.legs[1].side == "sell"
        result, payloads = self._run_with_failing_combo(strategy)
        sides = [p["side"] for p in payloads]
        assert sides == ["buy", "sell"]


# ---------------------------------------------------------------------------
# Layer 2 — combo "duplicated" rejection refuses sequential fallback
# ---------------------------------------------------------------------------


class TestDuplicatedLegRejectionDoesNotFallback:

    def test_duplicated_combo_error_returns_immediately(self):
        """When combo fails with 'leg.N symbol is duplicated', the
        sequential path must NOT run (it would obscure the real
        cause). Result is ERROR with the duplicate-leg reason."""
        from options_multileg import execute_multileg_strategy
        strategy = _bear_call_spread()

        sequential_called = [False]

        def _seq_should_not_run(api, payload):
            sequential_called[0] = True
            return _stub_order()

        def _dup_combo(api, payload, **kwargs):
            raise RuntimeError(
                'Alpaca order rejected (422): {"code":42210000,'
                '"message":"invalid legs: [leg.1 symbol '
                '\\"NOK260717C00015000\\" is duplicated]"}'
            )

        api = MagicMock()
        with patch(
            "options_multileg._combo_submit_with_retry",
            side_effect=_dup_combo,
        ), patch(
            "options_multileg._submit_alpaca_order_raw",
            side_effect=_seq_should_not_run,
        ), patch(
            "options_chain_alpaca.list_available_contracts",
            return_value=[],
        ):
            ctx = SimpleNamespace(db_path=None)
            result = execute_multileg_strategy(
                api, strategy, ctx=ctx, log=False, use_combo=True,
            )

        assert sequential_called[0] is False, (
            "Sequential fallback must NOT run after a duplicate-"
            "leg combo failure — it can't fix duplicate strikes "
            "and only produces a misleading downstream error."
        )
        assert result["action"] == "ERROR"
        assert "duplicate" in result["reason"].lower(), (
            f"Drop reason must surface the real duplicate-leg "
            f"cause, not a downstream symptom. Got: {result['reason']}"
        )

    def test_non_duplicate_combo_failure_still_falls_back(self):
        """Transient 5xx / other combo failures should STILL fall
        back to sequential — the refusal is specific to the
        duplicate-leg structural failure."""
        from options_multileg import execute_multileg_strategy
        strategy = _bear_call_spread()

        sequential_payloads = []

        def _seq(api, payload):
            sequential_payloads.append(dict(payload))
            return _stub_order()

        def _transient_500(api, payload, **kwargs):
            raise RuntimeError(
                'Alpaca order rejected (503): {"code":50000000,'
                '"message":"service unavailable"}'
            )

        api = MagicMock()
        with patch(
            "options_multileg._combo_submit_with_retry",
            side_effect=_transient_500,
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

        assert result["action"] == "MULTILEG_OPEN", (
            "Non-duplicate combo failures must still fall back to "
            "sequential. Refusal is specific to structural "
            "duplicate-leg errors."
        )
        # And the fallback used the buys-first ordering
        assert [p["side"] for p in sequential_payloads] == [
            "buy", "sell",
        ]


# ---------------------------------------------------------------------------
# Layer 3 — structural pin (source-level)
# ---------------------------------------------------------------------------


def test_sequential_path_sorts_legs_by_side():
    """Source-code pin: the sequential fallback loop must iterate
    a sorted-by-side sequence, NOT `strategy.legs` directly. A
    future refactor that drops the sort would silently re-introduce
    the uncovered-short bug."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent /
           "options_multileg.py").read_text()
    # Find the sequential fallback block
    seq_block_start = src.find("# Sequential fallback — submit each leg")
    assert seq_block_start > 0, (
        "Couldn't find sequential-fallback anchor comment; "
        "test anchor broke"
    )
    # In the next ~2000 chars, the iteration MUST go through a
    # sorted view, not `strategy.legs` directly.
    window = src[seq_block_start:seq_block_start + 2000]
    assert "sorted(" in window and "strategy.legs" in window, (
        "Sequential fallback must build a sorted view of "
        "strategy.legs before iterating"
    )
    # And the iteration loop must consume the sorted variable, not
    # strategy.legs directly. We anchor on the canonical helper name.
    assert "for i, leg in enumerate(sequential_legs)" in window, (
        "Sequential loop must iterate the sorted `sequential_legs` "
        "list, not `strategy.legs` directly — without the sort the "
        "uncovered-short bug returns on credit spreads"
    )
