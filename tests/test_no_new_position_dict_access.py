"""Phase 5 of Position class refactor (2026-05-11): structural
guardrail that pins the architectural invariant — both position
producers must return `Position` objects, never raw dicts.

Why this guardrail (and not a regex sweep over the codebase)
------------------------------------------------------------
The 23-phantom-stock-stop incident traced back to the symbol field
meaning TWO different things depending on producer:
  - client.get_positions returned OCC for option positions
  - journal.get_virtual_positions returned the underlying

The Position class + two factories (Position.from_alpaca and
Position.from_virtual_row) eliminate the ambiguity at the type
level. Every consumer reads attributes that are unambiguous.

If a future commit changes either producer to return a raw dict
again, every consumer that uses `pos.broker_symbol` / `pos.is_option`
breaks loudly. This test catches that regression at the producer
level — much more reliable than scanning consumer code for dict
patterns (which produces false positives on every non-Position
dict in the system).

Phase 5 deliberately does NOT remove Position's __getitem__ shim.
Existing consumers continue using the shim during incremental
migration. Phase 5b+ migrates consumers to attribute access; when
zero remain, the shim can be dropped in a clean commit.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from position import Position


class TestProducersReturnPositionObjects:
    def test_client_get_positions_returns_position(self):
        """client.get_positions must return List[Position]. If it
        regresses to List[dict], the symbol-vs-OCC overload bug
        class is re-introduced for every option position consumer."""
        from client import get_positions

        api = MagicMock()
        opt_pos = SimpleNamespace(
            symbol="PCG260612C00017000", qty="6",
            avg_entry_price="0.47", current_price="0.30",
            market_value="180.0", unrealized_pl="-102.0",
            unrealized_plpc="-0.36",
        )
        stk_pos = SimpleNamespace(
            symbol="AAPL", qty="100",
            avg_entry_price="150.0", current_price="155.0",
            market_value="15500.0", unrealized_pl="500.0",
            unrealized_plpc="0.033",
        )
        api.list_positions.return_value = [opt_pos, stk_pos]
        out = get_positions(api=api)

        assert len(out) == 2
        for p in out:
            assert isinstance(p, Position), (
                f"Producer regressed to dict-return for {p}. "
                "Must return Position objects."
            )
        # OCC routing correct
        occ_pos = [p for p in out if p.is_option][0]
        assert occ_pos.broker_symbol == "PCG260612C00017000"
        assert occ_pos.display_symbol == "PCG"
        # Stock routing correct
        stk = [p for p in out if p.is_stock][0]
        assert stk.broker_symbol == "AAPL"
        assert stk.display_symbol == "AAPL"

    def test_get_virtual_positions_returns_position(self):
        """journal.get_virtual_positions must return List[Position]."""
        import sqlite3
        import tempfile
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            from journal import init_db, get_virtual_positions
            init_db(db_path)
            conn = sqlite3.connect(db_path)
            # Stock long
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, fill_price, status) "
                "VALUES (?,?,?,?,?,?,?)",
                ("2026-05-11T10:00:00", "AAPL", "buy", 100, 150.0,
                 150.0, "open"),
            )
            # Option short (multileg short leg)
            conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, qty, "
                "price, fill_price, occ_symbol, signal_type, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-05-11T13:44:20", "RTX", "sell", 1.0, 3.15,
                 3.15, "RTX260618P00170000", "MULTILEG", "open"),
            )
            conn.commit()
            conn.close()
            out = get_virtual_positions(db_path=db_path)

            assert len(out) == 2
            for p in out:
                assert isinstance(p, Position), (
                    f"get_virtual_positions returned a non-Position "
                    f"value: {type(p).__name__} — must be Position."
                )
            # Stock + option positions both correctly classified
            kinds = sorted(p.instrument_kind for p in out)
            assert kinds == ["option", "stock"]
        finally:
            os.unlink(db_path)


class TestPositionConstructionInvariants:
    def test_option_must_have_occ_symbol(self):
        """Defense-in-depth: an 'option' Position WITHOUT occ_symbol
        is malformed; .broker_symbol must raise rather than silently
        returning the underlying (the bug shape that produced the
        phantom stock-stops). The factories enforce this; this test
        pins the assertion in case someone constructs Position
        directly."""
        bad = Position(
            instrument_kind="option",
            underlying="PCG", occ_symbol=None,
            qty_signed=1, avg_entry_price=0.5, current_price=0.5,
            market_value=50, unrealized_pl=0, unrealized_plpc=0,
        )
        with pytest.raises(AssertionError):
            _ = bad.broker_symbol

    def test_factories_are_the_only_place_that_decides_kind(self):
        """The OCC-vs-stock decision lives in TWO places only:
        Position.from_alpaca and Position.from_virtual_row.
        Adding another factory must come with the same OCC detection
        + assertion behavior."""
        import inspect
        from position import Position
        members = inspect.getmembers(Position, predicate=inspect.ismethod)
        classmethod_names = [
            name for name, obj in inspect.getmembers(Position)
            if isinstance(inspect.getattr_static(Position, name, None),
                          classmethod)
        ]
        # Currently exactly the two factories. If a new one is added,
        # update this test AND ensure the new factory does OCC
        # detection consistently.
        assert set(classmethod_names) == {
            "from_alpaca", "from_virtual_row",
        }, (
            "New Position classmethod factory detected. Every "
            "factory must own the OCC-vs-stock decision identically. "
            f"Current: {classmethod_names}"
        )
