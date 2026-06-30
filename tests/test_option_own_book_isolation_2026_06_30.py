"""Option specialists read OWN-BOOK positions only — never the shared
Alpaca conduit aggregate (2026-06-30).

`option_spread_risk` holds VETO authority and surfaces "current book Greeks"
to decide whether a proposal pushes the book past a delta/gamma/vega/theta
budget. It used to read `get_api(ctx).list_positions()` — the broker account's
positions, which on this platform is the AGGREGATE of every virtual profile
sharing that Alpaca conduit. That leaked one profile's exposure into another
profile's veto context (a cross-profile isolation violation: a profile must
judge only its OWN book). It now reads `client.get_positions(ctx=ctx)`, the
per-profile virtual book — same own-book routing `adversarial_reviewer` uses.

This file pins:
- OWN-BOOK ROUTING: `_current_positions` returns what `get_positions(ctx=ctx)`
  gives, and option legs keep their OCC symbol so Greeks still see them.
- CLASS PIN (fix-the-class): NO specialist anywhere calls `.list_positions()`
  directly — the leak can't return on a newly added specialist.
"""
from __future__ import annotations

import ast
import glob
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

from specialists import option_spread_risk


def test_current_positions_reads_own_book_via_get_positions(monkeypatch):
    """`_current_positions` sources from client.get_positions(ctx=ctx)."""
    seen = {}

    def fake_get_positions(api=None, ctx=None):
        seen["ctx"] = ctx
        return [
            {"symbol": "AAPL", "occ_symbol": None,
             "qty": 100, "current_price": 150.0},
        ]

    monkeypatch.setattr("client.get_positions", fake_get_positions)
    ctx = SimpleNamespace(is_virtual=True, db_path="/tmp/p999.db")
    out = option_spread_risk._current_positions(ctx)

    assert seen["ctx"] is ctx, "must pass the profile's own ctx to get_positions"
    assert out == [{"symbol": "AAPL", "occ_symbol": None,
                    "qty": 100.0, "current_price": 150.0}]


def test_option_leg_keeps_occ_so_greeks_still_detect_it(monkeypatch):
    """A virtual Position's "symbol" is the UNDERLYING; the OCC must be
    carried through or compute_book_greeks misclassifies the leg as stock."""
    # Real OCC: 6-char space-padded root + YYMMDD + C/P + 8-digit strike = 21.
    occ = "MSFT".ljust(6) + "260116" + "C" + "00400000"
    assert len(occ) == 21

    def fake_get_positions(api=None, ctx=None):
        return [{"symbol": "MSFT", "occ_symbol": occ,
                 "qty": -1, "current_price": 5.0}]

    monkeypatch.setattr("client.get_positions", fake_get_positions)
    out = option_spread_risk._current_positions(SimpleNamespace())

    assert out[0]["occ_symbol"] == occ
    assert out[0]["symbol"] == occ, "option dict keys off the OCC for greeks"
    # round-trips as an option into the greeks aggregator
    from options_greeks_aggregator import _is_option_position
    assert _is_option_position(out[0]) is True


def test_current_positions_failopen(monkeypatch):
    """Any error → [] (specialist still renders without the Greeks line)."""
    def boom(api=None, ctx=None):
        raise RuntimeError("book unavailable")

    monkeypatch.setattr("client.get_positions", boom)
    assert option_spread_risk._current_positions(SimpleNamespace()) == []


def test_no_specialist_calls_list_positions_directly():
    """CLASS PIN: no specialist reads the broker aggregate. Specialists that
    need the book must use client.get_positions(ctx=ctx) (own-book routing).
    This catches the leak returning on ANY specialist, not just the one we
    fixed."""
    offenders = []
    for path in glob.glob(os.path.join(REPO, "specialists", "*.py")):
        with open(path, "r") as fh:
            src = fh.read()
        if "list_positions" not in src:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "list_positions":
                offenders.append(os.path.basename(path))
                break
    assert not offenders, (
        "specialists must read the OWN-BOOK via client.get_positions(ctx=ctx), "
        "not the shared-conduit api.list_positions(): " + ", ".join(offenders)
    )
