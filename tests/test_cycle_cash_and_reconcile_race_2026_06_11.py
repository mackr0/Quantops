"""In-cycle cash race + reconciler dedup-snapshot race (2026-06-11).

Two live incidents on p97 within minutes of each other:

1. NEGATIVE VIRTUAL CASH (−$6,132). Every trade in a cycle sizes
   against the cycle-start account snapshot; `dollars =
   min(max_dollars, cash)` capped each BUY by the SAME
   un-decremented balance, so 3 BUYs dispatched in one second
   overdraw cumulatively. The COUNT side of this race was fixed in
   May (_append_new_stock_position); the CASH side never was — the
   old max_total_positions=10 cap masked it by bounding deployment,
   and the operator's 999 ("AI decides") removed the mask.

2. FALSE RECONCILER HALT. The cross-profile sell-order dedup set is
   snapshotted at task START; p97's CPNG sell (93ecef03) was
   journaled mid-pass at 16:45:22 and the reconcile read the stale
   set at 16:46:45 → its own journaled sell looked like an orphan
   broker fill → false "synthesis needed" halt. Worse: with the
   bracket-child exemption, the protective path could INSERT a
   duplicate exit row for an already-journaled fill.

Fixes pinned here:
  * _adjust_cycle_cash debits/credits the shared account snapshot
    after every executed stock trade, wired beside
    _append_new_stock_position in the dispatch loop.
  * Hard cash floor in BOTH the BUY and SHORT sizing branches (the
    max(1, …) drawdown/correlation bumps would otherwise force a
    1-share trade from negative dollars).
  * reconcile_with_ctx checks the LIVE own journal for the
    candidate order_id before creating any backfill action, on
    both the phantom and protective paths.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# (1) _adjust_cycle_cash unit behavior
# ---------------------------------------------------------------------------

class TestAdjustCycleCash:

    def test_buy_debits(self):
        from trade_pipeline import _adjust_cycle_cash
        acct = {"cash": 100000.0}
        delta = _adjust_cycle_cash(
            acct, {"action": "BUY", "qty": 100, "price": 50.0,
                   "estimated_cost": 5000.0})
        assert delta == -5000.0
        assert acct["cash"] == 95000.0

    def test_sell_credits(self):
        from trade_pipeline import _adjust_cycle_cash
        acct = {"cash": 1000.0}
        _adjust_cycle_cash(
            acct, {"action": "SELL", "qty": 10, "price": 50.0})
        assert acct["cash"] == 1500.0

    def test_short_credits_and_cover_debits(self):
        from trade_pipeline import _adjust_cycle_cash
        acct = {"cash": 0.0}
        _adjust_cycle_cash(
            acct, {"action": "SHORT", "qty": 10, "price": 10.0})
        assert acct["cash"] == 100.0
        _adjust_cycle_cash(
            acct, {"action": "COVER", "qty": 10, "price": 9.0})
        assert acct["cash"] == 10.0

    def test_non_trade_results_are_noops(self):
        from trade_pipeline import _adjust_cycle_cash
        acct = {"cash": 500.0}
        for tr in (None, "x", {}, {"action": "SKIP"},
                   {"action": "BUY", "qty": 0, "price": 0}):
            assert _adjust_cycle_cash(acct, tr) == 0.0
        assert acct["cash"] == 500.0

    def test_three_buys_cannot_overdraw(self):
        """The p97 shape: 3 BUYs in one cycle against a balance that
        only covers two. With the debit applied between trades, the
        third sees the drained balance."""
        from trade_pipeline import _adjust_cycle_cash
        acct = {"cash": 45000.0}
        for _ in range(2):
            _adjust_cycle_cash(
                acct, {"action": "BUY", "qty": 100, "price": 200.0})
        assert acct["cash"] == 5000.0  # third BUY sizes against THIS


# ---------------------------------------------------------------------------
# (2) Source pins — wiring + cash floors
# ---------------------------------------------------------------------------

def test_dispatch_loop_adjusts_cash_beside_position_append():
    src = (REPO / "trade_pipeline.py").read_text()
    # First occurrence is the def; the CALL site in the dispatch
    # loop is the second.
    def_idx = src.index(
        "_append_new_stock_position(positions_list, trade_result)")
    append_idx = src.index(
        "_append_new_stock_position(positions_list, trade_result)",
        def_idx + 1)
    window = src[append_idx:append_idx + 700]
    assert "_adjust_cycle_cash(account, trade_result)" in window, (
        "Dispatch loop no longer debits the cycle cash snapshot "
        "after executed trades — multi-BUY cycles will overdraw "
        "again (p97 −$6,132 class)."
    )


def test_both_sizing_branches_have_cash_floor():
    src = (REPO / "trade_pipeline.py").read_text()
    n = src.count("Insufficient cash remaining this cycle")
    assert n >= 2, (
        f"Expected the hard cash floor in BOTH the BUY and SHORT "
        f"sizing branches; found {n} occurrence(s). The max(1, …) "
        "reduce bumps force 1-share trades from negative dollars "
        "without it."
    )


# ---------------------------------------------------------------------------
# (3) Source pins — reconciler live own-journal checks
# ---------------------------------------------------------------------------

def test_phantom_path_checks_live_journal_before_action():
    """A3 (2026-06-16): the fuzzy "backfill" dispatch was deleted and
    replaced by the own-order-id-only "orphan_close" halt. The same
    mid-pass race protection must live on the new path — a position
    that closed after the open-rows snapshot must NOT false-HALT."""
    src = (REPO / "reconcile_journal_to_broker.py").read_text()
    kind_idx = src.index('elif kind == "orphan_close":')
    append_idx = src.index('actions["orphan_close"].append', kind_idx)
    guard = src[kind_idx:append_idx]
    assert "SELECT 1 FROM trades WHERE order_id" in guard, (
        "orphan_close no longer live-checks the own journal — an exit "
        "journaled after the snapshot becomes a false orphan + false "
        "HALT (CPNG 93ecef03 class)."
    )


def test_protective_path_checks_live_journal_before_action():
    src = (REPO / "reconcile_journal_to_broker.py").read_text()
    detect_idx = src.index(
        "prot_kind, prot_detail = _detect_protective_fill")
    full_idx = src.index('if prot_kind == "backfill_full":', detect_idx)
    guard = src[detect_idx:full_idx]
    assert "SELECT 1 FROM trades WHERE order_id" in guard, (
        "Protective backfill no longer live-checks the own journal "
        "— the bracket-child exemption can INSERT a duplicate exit "
        "row for an already-journaled fill."
    )
    assert "pending_protective" in guard, (
        "The protective-path guard must let placeholder rows keep "
        "flowing through the pending-UPDATE path (only real exit "
        "rows indicate the fill is already journaled)."
    )
