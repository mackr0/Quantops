"""2026-07-22 — THE CREATOR FIX: options_roll_manager's auto-close no
longer corrupts the books.

The p212 GOOG 375/380 incident's writer was one UPDATE:
    UPDATE trades SET status='pending_fill', pnl=?, order_id=?
    WHERE id=<the OPEN row>
It (a) clobbered the open row — order_id overwritten with the close
order's id on every retry, and (b) computed pnl with a short-leg-only
formula, inverting the long leg's sign (+2,148 booked, truth −2,148) —
fabricating +4,608 realized P&L and latching the integrity kill switch
for a full trading day.

Pins:
  1. sign math, both directions (the REAL helper _close_pnl_estimate,
     with the incident's exact numbers)
  2. the in-place clobber is GONE — the auto-close body contains no
     UPDATE that sets order_id on an existing row
  3. the close gets its OWN pending_fill row (log_trade with pnl —
     the fill state machine's option-close discriminator)
  4. resubmit dedup exists (the old status flip doubled as the retry
     guard; its replacement must prevent close-stacking)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestSignMath:

    def test_short_leg_incident_numbers(self):
        """375C: sold to open @10.05, closed @1.85 → +2,460."""
        from options_roll_manager import _close_pnl_estimate
        assert abs(_close_pnl_estimate("sell", 10.05, 1.85, 3)
                   - (10.05 - 1.85) * 300) < 1e-9

    def test_long_leg_incident_numbers(self):
        """380C: bought to open @8.35, closed @1.19 → −2,148 (the old
        formula booked +2,148 — the fabricated half of the +4,608)."""
        from options_roll_manager import _close_pnl_estimate
        pnl = _close_pnl_estimate("buy", 8.35, 1.19, 3)
        assert abs(pnl - (1.19 - 8.35) * 300) < 1e-9
        assert pnl < 0, "long leg closed below basis must be a LOSS"

    def test_long_leg_profit_direction(self):
        from options_roll_manager import _close_pnl_estimate
        assert _close_pnl_estimate("buy", 2.00, 5.00, 1) == 300.0


class TestNoInPlaceClobber:

    def _auto_close_body(self):
        src = open(os.path.join(REPO, "options_roll_manager.py")).read()
        idx = src.index("THE p212 GOOG-SPREAD CORRUPTION FIX")
        return src, src[idx - 4000:idx + 3000]

    def test_open_row_order_id_never_overwritten(self):
        src, body = self._auto_close_body()
        # Strip comment lines — the fix's own rationale comment quotes
        # the forbidden SQL; only EXECUTABLE occurrences count.
        code = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#"))
        assert "SET status='pending_fill', pnl=?" not in code, (
            "the in-place clobber is back — the open row's order_id/"
            "pnl must never be overwritten by a close")

    def test_close_gets_its_own_pending_row(self):
        _src, body = self._auto_close_body()
        assert "log_trade(" in body
        assert 'status="pending_fill"' in body
        assert "pnl=realized_est" in body, (
            "the close row must carry the pnl estimate — it is the "
            "fill state machine's option-close discriminator")

    def test_uses_direction_aware_estimate(self):
        _src, body = self._auto_close_body()
        assert "_close_pnl_estimate(" in body

    def test_resubmit_dedup_present(self):
        src = open(os.path.join(REPO, "options_roll_manager.py")).read()
        idx = src.index("RESUBMIT DEDUP")
        window = src[idx:idx + 1800]
        assert "pending_fill" in window and "continue" in window, (
            "without the dedup, every cycle stacks another close order "
            "now that the open row keeps status='open'")
