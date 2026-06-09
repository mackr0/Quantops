"""2026-06-09 — per-profile cover isolation. Mirror of
test_per_profile_sell_isolation for the short side.

Before this date, `trader._process_exit_trigger`'s COVER branch
submitted a `buy` order with zero cross-account validation. The
sibling-share-consumption bug had a direct mirror on the short
side: pid A holds 100 NOK short, pid B holds 100 NOK short,
aggregate broker short = 200. Pid A's stop fires for 100 cover.
Submit_order buys 100 NOK from the broker. Whose 100 just got
closed? Alpaca FIFO — could be pid B's.

The 2026-06-09 (afternoon) sell-isolation fix only patched
`allowable_sell_qty`. Cover was explicitly flagged out of scope
in that commit message. This file pins the mirror fix:
`allowable_cover_qty` is wired into the cover branch with the
same per-profile + drift-detection logic.

Tests pin:

  1. Per-profile cap from journal (own_short_qty).
  2. Drift detection (broker short < own_journal_short_claim).
  3. Both call paths route through the guard.
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
# Scaffolding — mirror of the SELL test fixtures
# ---------------------------------------------------------------------------


def _profile_db_with_short(tmp_path, symbol, short_qty, cover_qty=0,
                            filename="p.db"):
    """Create a minimal profile DB with an open SHORT of `short_qty`
    and optionally a closed COVER of `cover_qty`. Net virtual short =
    short_qty - cover_qty."""
    from journal import init_db
    db = str(tmp_path / filename)
    init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "                    signal_type, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-06-08T15:00:00", symbol, "short", short_qty, 12.50,
             "SHORT", "open"),
        )
        if cover_qty:
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "                    price, signal_type, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-06-08T16:00:00", symbol, "cover", cover_qty, 12.00,
                 "COVER", "closed"),
            )
        conn.commit()
    return db


def _stub_api_with_shorts(short_qty_by_symbol):
    """API stub whose list_positions returns broker rows with
    NEGATIVE qty for shorts."""
    api = MagicMock()
    positions = []
    for sym, short_qty in short_qty_by_symbol.items():
        p = MagicMock()
        p.symbol = sym
        # broker shorts are negative qty
        p.qty = str(-short_qty)
        positions.append(p)
    api.list_positions.return_value = positions
    return api


# ---------------------------------------------------------------------------
# Layer 1 — per-profile cap from own journal
# ---------------------------------------------------------------------------


class TestPerProfileShortCap:

    def test_cover_within_own_short_proceeds(self, tmp_path):
        from order_guard import allowable_cover_qty
        db = _profile_db_with_short(tmp_path, "NOK", 500)
        api = _stub_api_with_shorts({"NOK": 500})
        qty, reason = allowable_cover_qty(api, "NOK", 200, db_path=db)
        assert qty == 200
        assert reason == "ok"

    def test_cover_exceeds_own_short_refuses(self, tmp_path):
        """Profile virtually holds 500 NOK short. Trigger asks to
        cover 800 (e.g. AI hallucination, stale state). REFUSE —
        don't consume sibling shorts."""
        from order_guard import allowable_cover_qty
        db = _profile_db_with_short(tmp_path, "NOK", 500)
        # Broker has 2000 aggregate short (siblings hold the rest)
        api = _stub_api_with_shorts({"NOK": 2000})
        qty, reason = allowable_cover_qty(api, "NOK", 800, db_path=db)
        assert qty == 0
        assert "virtually holds 500 short" in reason
        assert "800" in reason

    def test_cover_exact_own_short_proceeds(self, tmp_path):
        from order_guard import allowable_cover_qty
        db = _profile_db_with_short(tmp_path, "NOK", 500)
        api = _stub_api_with_shorts({"NOK": 500})
        qty, reason = allowable_cover_qty(api, "NOK", 500, db_path=db)
        assert qty == 500
        assert reason == "ok"

    def test_profile_with_no_short_refuses(self, tmp_path):
        """Profile journal has a closed short — net zero. Even if
        broker has shorts (siblings'), this profile cannot cover."""
        from order_guard import allowable_cover_qty
        db = _profile_db_with_short(
            tmp_path, "NOK", 500, cover_qty=500,
        )
        api = _stub_api_with_shorts({"NOK": 1500})  # siblings'
        qty, reason = allowable_cover_qty(api, "NOK", 100, db_path=db)
        assert qty == 0
        assert "virtually holds 0 short" in reason


# ---------------------------------------------------------------------------
# Layer 2 — drift detection (broker short < own journal short)
# ---------------------------------------------------------------------------


class TestCoverDriftDetection:

    def test_broker_less_short_than_journal_refuses(self, tmp_path):
        """Journal says 500 short; broker is only 200 short.
        Drift exists. REFUSE — don't silently cover 200."""
        from order_guard import allowable_cover_qty
        db = _profile_db_with_short(tmp_path, "NOK", 500)
        api = _stub_api_with_shorts({"NOK": 200})
        qty, reason = allowable_cover_qty(api, "NOK", 400, db_path=db)
        assert qty == 0
        assert "drift" in reason.lower()
        assert "200" in reason  # broker short
        assert "500" in reason  # journal claim

    def test_broker_long_or_flat_with_journal_short_refuses(self, tmp_path):
        """Journal says 500 short; broker is flat or long. Drift —
        a sibling already covered our entire position."""
        from order_guard import allowable_cover_qty
        db = _profile_db_with_short(tmp_path, "NOK", 500)
        api = _stub_api_with_shorts({})  # broker has no NOK at all
        qty, reason = allowable_cover_qty(api, "NOK", 100, db_path=db)
        assert qty == 0
        assert "drift" in reason.lower()


# ---------------------------------------------------------------------------
# Layer 3 — call-site wired in trader.py
# ---------------------------------------------------------------------------


def test_trader_cover_branch_passes_db_path_to_guard():
    """Source pin: the COVER branch in trader._process_exit_trigger
    must import and call allowable_cover_qty with db_path=db_path.
    Without it the per-profile check is skipped and the cross-
    profile short consumption bug returns on the short side."""
    src = (REPO_ROOT / "trader.py").read_text()
    # The COVER branch starts with `if is_short:` followed by the
    # buy-order submit. The guard must appear inside that branch.
    short_branch_idx = src.find("if is_short:")
    assert short_branch_idx > 0, "is_short branch anchor missing"
    window = src[short_branch_idx:short_branch_idx + 2000]
    assert "from order_guard import allowable_cover_qty" in window, (
        "trader.py's COVER branch must import allowable_cover_qty. "
        "Without it the cross-profile short-consumption mirror bug "
        "is unprotected on the short side."
    )
    assert "db_path=db_path" in window, (
        "allowable_cover_qty must be called with db_path=db_path. "
        "Without it the per-profile journal check is skipped."
    )


# ---------------------------------------------------------------------------
# Layer 4 — the guard returns either (requested, "ok") or (0, reason)
# ---------------------------------------------------------------------------


def test_no_downsize_in_cover_guard():
    """Mirror of the sell-side requirement: there must be NO path
    where the function returns a positive qty less than requested.
    Either approve in full or refuse with reason."""
    src = (REPO_ROOT / "order_guard.py").read_text()
    fn_start = src.find("def allowable_cover_qty")
    assert fn_start > 0
    fn_end = src.find("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end if fn_end > 0 else len(src)]
    assert "downsized" not in body.lower(), (
        "The 'downsized' return path is the bug class. Same as the "
        "sell-side rewrite — refuse with reason instead of silently "
        "shrinking the cover qty."
    )
