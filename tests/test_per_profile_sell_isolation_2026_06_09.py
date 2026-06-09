"""2026-06-09 — per-profile sell isolation.

Before this date, `allowable_sell_qty` checked the AGGREGATE broker
pool across all profiles sharing an Alpaca account. When the
aggregate had fewer shares than the request, it DOWNSIZED to the
aggregate qty. That mechanism is exactly how profile A consumed
profile B's shares:

  - Profile A buys 3655 LXEH → journal A: 3655
  - Sibling profiles buy 4887 LXEH between them → aggregate broker
    pool = 3655 + 4887 = 8542 (but stop-loss fires consume some)
  - At 15:51:30 A proposes SELL 2979 LXEH. Aggregate is 2979 at this
    point (siblings' stops had eaten down to that). Old guard:
    "broker has 2979, request 2979 — ok, go." A sells 2979.
  - But of those 2979 shares: 676 are A's, 1788 are pid 44's, 1191
    are pid 45's, 1908 are pid 43's. A consumed sibling shares
    without their journals being updated. Phantom positions form.

The 2026-06-09 rewrite enforces strict per-profile isolation:

  1. A profile may sell ONLY what its own journal claims it holds
     (via `get_virtual_positions` on its own DB).
  2. The aggregate broker pool is consulted as a SANITY check only:
     if broker < own_journal_qty, that's DRIFT (sibling already
     consumed our share, or external action closed the position).
     Refuse and surface loudly — do NOT silently downsize.
  3. The DOWNSIZE path is removed entirely. The guard returns either
     `(requested_qty, "ok")` or `(0, reason)`. No partial fills.

Tests pin the contract at multiple layers:

  - Per-profile qty cap from journal.
  - Drift detection (broker < journal).
  - Pid 42 LXEH 15:51:30 reproduction — the historical incident.
  - Both callers (`trade_pipeline`, `trader`) pass `db_path`.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Test scaffolding — minimal profile DB with N buys and M sells
# ---------------------------------------------------------------------------


def _profile_db_with_position(tmp_path, symbol, buy_qty, sell_qty=0,
                              filename="p.db"):
    """Create a minimal profile DB with an open BUY of `buy_qty` and
    optionally a closed SELL of `sell_qty`. Net virtual position =
    buy_qty - sell_qty."""
    from journal import init_db
    db = str(tmp_path / filename)
    init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "                    signal_type, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-06-08T15:00:00", symbol, "buy", buy_qty, 1.25,
             "BUY", "open"),
        )
        if sell_qty:
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "                    price, signal_type, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-06-08T16:00:00", symbol, "sell", sell_qty, 1.30,
                 "SELL", "closed"),
            )
        conn.commit()
    return db


def _stub_api(broker_qty_by_symbol):
    """API stub whose list_positions returns broker rows for the
    given symbols/qtys."""
    api = MagicMock()
    positions = []
    for sym, qty in broker_qty_by_symbol.items():
        p = MagicMock()
        p.symbol = sym
        p.qty = str(qty)
        positions.append(p)
    api.list_positions.return_value = positions
    return api


# ---------------------------------------------------------------------------
# Layer 1 — profile's own journal caps the sell qty
# ---------------------------------------------------------------------------


class TestPerProfileVirtualCap:

    def test_sell_within_own_qty_proceeds(self, tmp_path):
        """Profile owns 1000 LXEH (per its journal). Asks to sell 500.
        Broker has 1000 (only this profile's). Guard says ok."""
        from order_guard import allowable_sell_qty
        db = _profile_db_with_position(tmp_path, "LXEH", 1000)
        api = _stub_api({"LXEH": 1000})
        qty, reason = allowable_sell_qty(api, "LXEH", 500, db_path=db)
        assert qty == 500
        assert reason == "ok"

    def test_sell_exceeds_own_qty_refuses(self, tmp_path):
        """Profile owns 1000 LXEH. Asks to sell 1500 (e.g. AI
        hallucination, stale state). REFUSE — don't consume siblings."""
        from order_guard import allowable_sell_qty
        db = _profile_db_with_position(tmp_path, "LXEH", 1000)
        # Broker has 8000 (lots of sibling shares too), but that's
        # irrelevant to the per-profile cap
        api = _stub_api({"LXEH": 8000})
        qty, reason = allowable_sell_qty(api, "LXEH", 1500, db_path=db)
        assert qty == 0, (
            "Profile must not be allowed to sell more than its own "
            f"journal claims. Got qty={qty}, reason={reason}"
        )
        assert "virtually holds 1000" in reason
        assert "1500" in reason

    def test_sell_exactly_own_qty_proceeds(self, tmp_path):
        from order_guard import allowable_sell_qty
        db = _profile_db_with_position(tmp_path, "LXEH", 1000)
        api = _stub_api({"LXEH": 1000})
        qty, reason = allowable_sell_qty(api, "LXEH", 1000, db_path=db)
        assert qty == 1000
        assert reason == "ok"

    def test_profile_with_zero_position_refuses(self, tmp_path):
        """Profile's journal shows the position fully closed (buy +
        matching sell). Even though broker may have shares (from
        sibling profiles), this profile cannot sell anything."""
        from order_guard import allowable_sell_qty
        db = _profile_db_with_position(
            tmp_path, "LXEH", 1000, sell_qty=1000,
        )
        api = _stub_api({"LXEH": 5000})  # siblings hold the rest
        qty, reason = allowable_sell_qty(api, "LXEH", 500, db_path=db)
        assert qty == 0
        assert "virtually holds 0" in reason


# ---------------------------------------------------------------------------
# Layer 2 — drift detection (broker < own journal)
# ---------------------------------------------------------------------------


class TestDriftDetection:

    def test_broker_less_than_journal_refuses(self, tmp_path):
        """Profile journal says 1000 LXEH; broker has only 500.
        Drift exists. REFUSE — don't silently sell 500 (that's the
        old downsize bug). Operator gets a clear refused-with-drift
        signal."""
        from order_guard import allowable_sell_qty
        db = _profile_db_with_position(tmp_path, "LXEH", 1000)
        api = _stub_api({"LXEH": 500})
        qty, reason = allowable_sell_qty(api, "LXEH", 800, db_path=db)
        assert qty == 0
        assert "drift" in reason.lower()
        assert "500" in reason  # broker qty
        assert "1000" in reason  # journal claim

    def test_broker_zero_with_journal_position_refuses(self, tmp_path):
        """Profile thinks it has 1000; broker has 0 (sibling already
        consumed). Refuse."""
        from order_guard import allowable_sell_qty
        db = _profile_db_with_position(tmp_path, "LXEH", 1000)
        api = _stub_api({})  # broker has none
        qty, reason = allowable_sell_qty(api, "LXEH", 500, db_path=db)
        assert qty == 0
        assert "drift" in reason.lower()


# ---------------------------------------------------------------------------
# Layer 3 — historical reproduction (pid 42 LXEH 15:51:30)
# ---------------------------------------------------------------------------


class TestPid42LXEHReproduction:
    """Replay the 2026-06-08 15:51:30 scenario with the new guard
    and verify it would have refused — preventing the sibling-share
    consumption that caused 4 phantom journal rows."""

    def test_pid42_sell_of_2979_lxeh_is_refused_under_new_policy(
        self, tmp_path,
    ):
        """At 15:51:30 pid 42 proposed SELL 2979 LXEH. Pid 42's
        journal claimed 3655 (its original BUY). Broker aggregate
        was 2979 (down from earlier sibling buys, then sibling
        stops). Pre-rewrite: guard saw broker=2979 >= request=2979,
        returned ok. Sold 2979, consuming 2303 sibling shares.

        Post-rewrite: per-profile check passes (3655 >= 2979). But
        broker (2979) is LESS than pid 42's journal claim (3655) →
        DRIFT detected → REFUSE. Pid 42's sell does NOT execute. No
        siblings consumed."""
        from order_guard import allowable_sell_qty
        # Pid 42's own journal: 3655 long
        db = _profile_db_with_position(tmp_path, "LXEH", 3655,
                                       filename="p42.db")
        # Aggregate broker has 2979 (the rest is sibling-side, but
        # the new guard doesn't know or care — only that broker <
        # journal claim means drift)
        api = _stub_api({"LXEH": 2979})
        qty, reason = allowable_sell_qty(
            api, "LXEH", 2979, db_path=db,
        )
        assert qty == 0, (
            "REGRESSION ALERT: pid 42's 2979 LXEH sell would be "
            "permitted, recreating the sibling-share consumption "
            "bug from 2026-06-08. Got qty={}, reason={}".format(qty, reason)
        )
        assert "drift" in reason.lower(), (
            f"Refusal reason must surface drift so the operator can "
            f"investigate. Got: {reason}"
        )


# ---------------------------------------------------------------------------
# Layer 4 — both callers pass db_path (structural pin)
# ---------------------------------------------------------------------------


def test_trade_pipeline_passes_db_path_to_guard():
    """Structural pin: the SELL branch in trade_pipeline.py must pass
    `db_path` to allowable_sell_qty. Without this kwarg the per-
    profile check is skipped and the guard falls back to the broker-
    only path — silently re-enabling cross-profile contamination
    once a sibling has been consumed (broker_qty would still match
    requested_qty in many cases)."""
    src = (REPO_ROOT / "trade_pipeline.py").read_text()
    # Find the SELL-branch allowable_sell_qty call
    call_idx = src.find("from order_guard import allowable_sell_qty")
    assert call_idx > 0
    # Get the next ~600 chars (covers the multi-line call)
    window = src[call_idx:call_idx + 600]
    assert "db_path=db_path" in window, (
        "trade_pipeline.py's SELL branch must pass db_path=db_path "
        "to allowable_sell_qty. Without it the per-profile check is "
        "skipped — the cross-profile contamination bug returns."
    )


def test_trader_passes_db_path_to_guard():
    """Same pin for trader.py (the stop-trigger SELL path)."""
    src = (REPO_ROOT / "trader.py").read_text()
    call_idx = src.find("from order_guard import allowable_sell_qty")
    assert call_idx > 0
    window = src[call_idx:call_idx + 600]
    assert "db_path=db_path" in window, (
        "trader.py's stop-trigger SELL path must pass db_path=db_path "
        "to allowable_sell_qty."
    )


# ---------------------------------------------------------------------------
# Layer 5 — the downsize path is GONE
# ---------------------------------------------------------------------------


def test_no_downsize_path_in_allowable_sell_qty():
    """The pre-2026-06-09 'downsize to broker_qty' return is GONE.
    Audit the source for the canonical signature: every return must
    be `(requested_qty, ...)`, `(0, ...)`, or the OCC/permissive
    passthrough. There must be NO return of `(broker_qty, ...)`
    where broker_qty < requested_qty.

    Verified structurally: the string 'downsized' should not appear
    as a return reason. (The OLD code returned reason='downsized:
    broker has only N shares' from the downsize branch.)"""
    src = (REPO_ROOT / "order_guard.py").read_text()
    # Find the allowable_sell_qty function body
    fn_start = src.find("def allowable_sell_qty")
    assert fn_start > 0
    fn_end = src.find("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end if fn_end > 0 else len(src)]
    assert "downsized" not in body.lower(), (
        "The 'downsized' return path is the bug — one profile gets "
        "permission to take whatever's at the broker (which can "
        "include sibling shares). Remove all downsize returns."
    )
