"""RC2 root-cause fix: when `walk_replace_chain_forward` dead-ends
because Alpaca GC'd an intermediate replace link, the reconciler
must still be able to attribute the actual fill to the journaled
placement via a backward-walk fallback.

Failure mode pinned: trailing stop journaled as `original-1`,
replaced server-side multiple times, terminal `terminal-99` fires
and fills. By the time the reconciler runs, `api.get_order(
'intermediate-50')` returns None (Alpaca GC), so forward walk from
`original-1` dead-ends before reaching `terminal-99`. Without the
backward-walk fallback, `_detect_protective_fill` returns (None,
None) and the fill stays invisible.

Fix: `walk_replace_chain_backward` + `_find_terminal_via_backward_
walk` scan recent broker orders for filled SELLs on the symbol,
walk each candidate's `replaces` chain backward, and match the
candidate to the journaled placement if the trail reaches it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_walk_replace_chain_backward_finds_target():
    """Walking back via `replaces` from a terminal reaches the
    journaled target id within max_depth."""
    from reconcile_journal_to_broker import walk_replace_chain_backward

    # Chain: original -> intermediate -> terminal
    # `replaces` points backward.
    by_id = {
        "terminal": SimpleNamespace(id="terminal", replaces="intermediate"),
        "intermediate": SimpleNamespace(
            id="intermediate", replaces="original",
        ),
        "original": SimpleNamespace(id="original", replaces=None),
    }
    api = MagicMock()
    api.get_order = lambda oid: by_id.get(oid)
    assert walk_replace_chain_backward(api, "terminal", "original")
    # Target == start: True immediately
    assert walk_replace_chain_backward(api, "original", "original")
    # Different target: not in this chain
    assert not walk_replace_chain_backward(api, "terminal", "other")


def test_walk_replace_chain_backward_stops_at_dead_end():
    """If `replaces` is None before we hit target, return False —
    don't crash, don't loop forever."""
    from reconcile_journal_to_broker import walk_replace_chain_backward

    api = MagicMock()
    api.get_order = lambda oid: SimpleNamespace(id=oid, replaces=None)
    assert not walk_replace_chain_backward(api, "any-id", "target-id")


def test_walk_replace_chain_backward_handles_max_depth():
    """Don't walk forever — bounded by max_depth."""
    from reconcile_journal_to_broker import walk_replace_chain_backward

    # Infinite chain — every order replaces the next one
    api = MagicMock()
    api.get_order = lambda oid: SimpleNamespace(
        id=oid, replaces=oid + "-prev",
    )
    assert not walk_replace_chain_backward(
        api, "start", "never-found", max_depth=5,
    )


def test_walk_replace_chain_backward_None_terminal_safe():
    """API blip returns None — return False, don't crash."""
    from reconcile_journal_to_broker import walk_replace_chain_backward

    api = MagicMock()
    api.get_order = lambda oid: None
    assert not walk_replace_chain_backward(api, "x", "y")


def test_find_terminal_via_backward_walk_matches_filled_candidate():
    """The fallback finds a candidate broker order whose `replaces`
    chain leads back to the journaled placement, and returns the
    fill detail keyed on the JOURNALED oid (so the apply path's
    UPDATE-by-order_id matches the pending_protective row)."""
    from reconcile_journal_to_broker import (
        _find_terminal_via_backward_walk,
    )

    journaled_oid = "original-1"
    # Broker returns one filled SELL whose `replaces` chain leads
    # back to journaled_oid: terminal -> intermediate -> original-1.
    terminal = SimpleNamespace(
        id="terminal-99",
        side="sell",
        symbol="NVDA",
        status="filled",
        filled_qty="100",
        filled_avg_price="510.25",
        filled_at=datetime(2026, 6, 5, 14, 0, 0, tzinfo=timezone.utc),
        order_type="trailing_stop",
        replaces="intermediate-50",
    )
    intermediate = SimpleNamespace(
        id="intermediate-50",
        replaces="original-1",
    )
    by_id = {
        "terminal-99": terminal,
        "intermediate-50": intermediate,
    }
    api = MagicMock()
    api.get_order = lambda oid: by_id.get(oid)
    api.list_orders = MagicMock(return_value=[terminal])

    row = {
        "side": "buy",
        "symbol": "NVDA",
        "timestamp": "2026-06-04T14:00:00",
        "qty": 100.0,
    }
    used_sell_ids: set = set()
    detail = _find_terminal_via_backward_walk(
        api, row, journaled_oid, used_sell_ids,
    )

    assert detail is not None, (
        "Backward-walk fallback must find the terminal when the "
        "candidate's `replaces` chain traces to the journaled id"
    )
    assert detail["order_id"] == journaled_oid, (
        "Returned order_id MUST be the journaled id so the apply "
        "path's UPDATE by order_id matches the pending_protective row"
    )
    assert detail["filled_qty"] == pytest.approx(100.0)
    assert detail["filled_avg_price"] == pytest.approx(510.25)
    assert "terminal-99" in used_sell_ids, (
        "Terminal id must be recorded so subsequent reconciler "
        "passes (and sibling profiles) don't double-attribute it"
    )


def test_find_terminal_via_backward_walk_ignores_unrelated_orders():
    """A filled SELL whose `replaces` chain does NOT lead to the
    journaled id must be skipped — could belong to a sibling
    profile or a different position."""
    from reconcile_journal_to_broker import (
        _find_terminal_via_backward_walk,
    )

    sibling_terminal = SimpleNamespace(
        id="sibling-99",
        side="sell",
        symbol="NVDA",
        status="filled",
        filled_qty="100",
        filled_avg_price="510.25",
        filled_at=datetime(2026, 6, 5, 14, 0, 0, tzinfo=timezone.utc),
        order_type="trailing_stop",
        replaces="sibling-prev",
    )
    sibling_prev = SimpleNamespace(id="sibling-prev", replaces=None)
    by_id = {
        "sibling-99": sibling_terminal,
        "sibling-prev": sibling_prev,
    }
    api = MagicMock()
    api.get_order = lambda oid: by_id.get(oid)
    api.list_orders = MagicMock(return_value=[sibling_terminal])

    row = {
        "side": "buy",
        "symbol": "NVDA",
        "timestamp": "2026-06-04T14:00:00",
        "qty": 100.0,
    }
    detail = _find_terminal_via_backward_walk(
        api, row, "my-original-id", set(),
    )
    assert detail is None, (
        "Unrelated broker SELL (chain doesn't trace to my journaled "
        "id) must NOT be attributed to my position"
    )


def test_detect_protective_fill_uses_backward_walk_on_dead_end():
    """Integration: when forward walk dead-ends, _detect_protective_
    fill invokes the backward-walk fallback and returns 'backfill_
    full' if a matching candidate is found."""
    from reconcile_journal_to_broker import _detect_protective_fill

    journaled_oid = "original-2"
    # Forward walk dead-ends: original-2 is `replaced` with
    # replaced_by=GC'd-id, and get_order(GC'd-id) returns None.
    # Backward walk: terminal-fill's replaces chain leads to
    # original-2.
    terminal = SimpleNamespace(
        id="terminal-fill",
        side="sell",
        symbol="MSFT",
        status="filled",
        filled_qty="50",
        filled_avg_price="420.10",
        filled_at=datetime(2026, 6, 5, 14, 0, 0, tzinfo=timezone.utc),
        order_type="trailing_stop",
        replaces="original-2",
    )
    by_id = {
        "original-2": SimpleNamespace(
            id="original-2", status="replaced",
            replaced_by="ghost-id",
        ),
        # ghost-id intentionally missing — simulates Alpaca GC
        "terminal-fill": terminal,
    }
    api = MagicMock()
    api.get_order = lambda oid: by_id.get(oid)
    api.list_orders = MagicMock(return_value=[terminal])

    row = {
        "side": "buy",
        "symbol": "MSFT",
        "timestamp": "2026-06-04T14:00:00",
        "qty": 50.0,
        "protective_stop_order_id": None,
        "protective_tp_order_id": None,
        "protective_trailing_order_id": "original-2",
    }
    kind, detail = _detect_protective_fill(api, row, set())
    assert kind == "backfill_full", (
        f"Expected backfill_full via backward-walk fallback when "
        f"forward walk dead-ends; got {kind!r}"
    )
    assert detail["order_id"] == "original-2", (
        "Must return JOURNALED oid so the apply path's "
        "UPDATE-pending_protective-by-order_id matches"
    )
    assert detail["filled_avg_price"] == pytest.approx(420.10)
