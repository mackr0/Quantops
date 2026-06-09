"""2026-06-09 (post-reset) — strike-snap collision detection.

Earlier today's "grid-aware snap" commit widened
`snap_to_listed_contract`'s per-strike tolerance so neighboring
listed contracts could be substituted. The commit message flagged
the upstream collapse risk as "separate change":

  > "The NOK case ALSO has an upstream strike-snapper bug
  >  (collapsed both legs to NOK 15C). With this fix, that case
  >  now produces a clear 'duplicate-leg' drop reason instead of
  >  an 'uncovered' red herring. Fixing the strike snapper itself
  >  is a separate change."

That deferral was the wrong call. The first post-reset cycle saw
the same collapse on NU260717P00011000 — a bull_put_spread whose
two PUT strikes both snapped to the $11 contract, producing a
zero-width spread the broker rejects.

This fix detects the collision BEFORE submitting to the broker:
after the per-leg snap loop in `execute_multileg_strategy`,
check that every snapped leg has a unique OCC. If two legs
collide, refuse with a clean reason naming the AI as the source
(the strikes it proposed were too close on this chain).

Contract pinned:
  1. AI strikes that snap to the SAME contract → refuse with
     "Strike-snap collision" reason. No broker submission.
  2. AI strikes that snap to DIFFERENT contracts → proceed as
     before.
  3. The duplicate-leg combo error path still catches whatever
     slips through this guard (e.g. snapper returns same OCC
     for genuinely identical strikes the AI proposed by mistake).
"""
from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)


EXPIRY = date(2099, 1, 16)


# ---------------------------------------------------------------------------
# Layer 1 — collision is detected and refused before broker submit
# ---------------------------------------------------------------------------


class TestStrikeSnapCollisionDetection:

    def _make_chain(self, strikes, opt_type="put"):
        """Synthetic Alpaca contract list at one expiry."""
        return [
            {
                "symbol": f"NU{i:06d}",
                "expiration_date": EXPIRY.isoformat(),
                "type": opt_type,
                "strike": float(s),
            }
            for i, s in enumerate(strikes)
        ]

    def test_two_strikes_collapse_to_same_contract_refuses(self):
        """AI proposes bull_put_spread short=11.5, long=10.5 on a
        chain spaced $1 (strikes 10, 11, 12, …). The grid-aware
        tolerance allows half-strike snaps, so BOTH 11.5 and 10.5
        snap to $11 (each is 0.5 away). Result: snapped legs
        collide → refuse."""
        from options_multileg import (
            execute_multileg_strategy, build_bull_put_spread,
        )

        # Build the strategy with the AI's pre-snap strikes
        strategy = build_bull_put_spread(
            "NU", EXPIRY, 10.5, 11.5, qty=1,
        )

        # The chain has $10, $11, $12 — short (11.5) snaps to $11,
        # long (10.5) also snaps to $11 because $11 is the closest
        # contract within tolerance to both. Synthetic chain:
        chain = self._make_chain([10, 11, 12, 13])

        api = MagicMock()
        # Stub the snapper to ALWAYS return the $11 contract,
        # forcing the collision (matches what the real snapper
        # does for this case on a $1-spaced chain).
        snapped_11 = {
            "symbol": "NU260717P00011000",
            "expiration_date": EXPIRY.isoformat(),
            "type": "put",
            "strike": 11.0,
        }
        with patch(
            "options_chain_alpaca.list_available_contracts",
            return_value=chain,
        ), patch(
            "options_chain_alpaca.snap_to_listed_contract",
            return_value=snapped_11,
        ):
            ctx = type("C", (), {"db_path": None})()
            result = execute_multileg_strategy(
                api, strategy, ctx=ctx, log=False,
                use_combo=True,
            )

        assert result["action"] == "ERROR", (
            f"Strike-snap collision must REFUSE before any broker "
            f"submit. Got action={result['action']}"
        )
        assert "strike-snap collision" in result["reason"].lower(), (
            f"Refusal reason must name the strike-snap collision "
            f"so the operator sees the AI as the source, not a "
            f"downstream broker symptom. Got: {result['reason']}"
        )
        assert "NU260717P00011000" in result["reason"], (
            "Refusal reason must name the collided OCC contract so "
            "the operator can trace the chain on Alpaca's side."
        )
        # And no broker submit happened
        assert not api.submit_order.called, (
            "Strike-snap collision must NOT submit to broker."
        )

    def test_distinct_snaps_proceed_normally(self):
        """Sanity check: when the snapper produces DIFFERENT
        contracts for the two legs, the strategy proceeds as
        before. The collision guard must not block legitimate
        spreads."""
        from options_multileg import (
            execute_multileg_strategy, build_bull_put_spread,
        )

        strategy = build_bull_put_spread(
            "NU", EXPIRY, 10.0, 12.0, qty=1,
        )
        chain = [
            {"symbol": "NU260717P00010000",
             "expiration_date": EXPIRY.isoformat(),
             "type": "put", "strike": 10.0},
            {"symbol": "NU260717P00012000",
             "expiration_date": EXPIRY.isoformat(),
             "type": "put", "strike": 12.0},
        ]

        # Stub snapper to return distinct contracts for the two
        # input strikes
        def _snap(symbol, target_expiry, target_strike, opt_type,
                  contracts=None):
            for c in chain:
                if abs(c["strike"] - target_strike) < 0.01:
                    return c
            return None

        api = MagicMock()
        api.submit_order.return_value = MagicMock(id="combo-ok")
        with patch(
            "options_chain_alpaca.list_available_contracts",
            return_value=chain,
        ), patch(
            "options_chain_alpaca.snap_to_listed_contract",
            side_effect=_snap,
        ):
            ctx = type("C", (), {"db_path": None})()
            result = execute_multileg_strategy(
                api, strategy, ctx=ctx, log=False,
                use_combo=True,
            )
        # No collision → either MULTILEG_OPEN (combo succeeded) or
        # some other non-collision outcome. The key invariant: the
        # collision reason must NOT appear.
        assert "strike-snap collision" not in (
            result.get("reason") or ""
        ).lower(), (
            f"Distinct snapped contracts must NOT trigger collision "
            f"detection. Got: {result['reason']}"
        )


# ---------------------------------------------------------------------------
# Layer 2 — source pin (refactor protection)
# ---------------------------------------------------------------------------


def test_collision_detection_is_present_in_snap_block():
    """Source-level pin: the collision-detection loop must live
    INSIDE the snap block (between `_snap` calls and the
    `strategy.legs = snapped_legs` assignment) so a refactor that
    moves the snap loop can't silently skip the check."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "options_multileg.py").read_text()
    fn_start = src.find("def execute_multileg_strategy")
    assert fn_start > 0
    fn_end = src.find("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end if fn_end > 0 else len(src)]
    # The post-snap collision check uses `seen_occ` and refuses
    # on duplicates. A refactor that removes either the dict or
    # the duplicate test breaks this contract.
    assert "seen_occ" in body, (
        "Strike-snap collision detection must use a `seen_occ` "
        "tracking dict to identify duplicate OCC symbols across "
        "snapped legs. Without this the broker-side duplicate "
        "rejection is the only safety net."
    )
    assert "Strike-snap collision" in body, (
        "The collision refusal reason text must include "
        "'Strike-snap collision' so the brain badge surfaces the "
        "AI-side root cause instead of a downstream symptom."
    )
