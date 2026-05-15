"""Phase 3 of Position class refactor: display + audit migrated to
Position attributes (2026-05-11).

- `views._enriched_positions` uses pos.is_option / pos.display_symbol
  for OCC-vs-underlying decisions.
- `virtual_audit` recognizes legitimate short option legs (option
  positions with qty<0) and does NOT flag them as data integrity
  issues.

2026-05-15 — extended to also recognize legitimate STOCK shorts
(qty<0 backed by a 'short' side entry in the journal). The
correct stock-short contract is pinned in
test_virtual_audit_distinguishes_legitimate_shorts.py.
"""
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture(autouse=True)
def _clear_dashboard_cache():
    """Phase 1 cache lives module-level; clear between tests so
    one test's mocked positions don't leak into the next."""
    import views
    views._dashboard_cache.clear()
    yield
    views._dashboard_cache.clear()


@pytest.fixture
def tmp_profile_db(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    profile_id = 999
    db_path = f"quantopsai_profile_{profile_id}.db"
    from journal import init_db
    init_db(db_path)
    return profile_id, db_path


def _ctx_with_positions(positions):
    api = MagicMock()
    api.list_positions.return_value = [
        SimpleNamespace(
            symbol=p["symbol"], qty=str(p["qty"]),
            market_value=str(p.get("market_value", 1000)),
            unrealized_pl=str(p.get("unrealized_pl", 0)),
            unrealized_plpc=str(p.get("unrealized_plpc", 0)),
            current_price=str(p.get("current_price", 100)),
            avg_entry_price=str(p.get("avg_entry_price", 99)),
        )
        for p in positions
    ]
    return SimpleNamespace(
        get_alpaca_api=lambda: api, db_path="test.db",
        display_name="Test", segment="small",
    )


class TestEnrichedPositionsUsesDisplaySymbol:
    def test_option_position_renders_underlying_as_symbol(self, tmp_profile_db):
        """Per the macro contract: symbol = underlying ('PCG'),
        occ_symbol = OCC string. The macro then renders OPT badge +
        underlying + OCC on three lines. Without display_symbol, an
        Alpaca-direct option position would have symbol = OCC and
        the macro would render the OCC as the strong header."""
        from views import _enriched_positions
        profile_id, _ = tmp_profile_db

        ctx = _ctx_with_positions([
            {"symbol": "PCG260612C00017000", "qty": 6,
             "current_price": 0.30, "avg_entry_price": 0.47,
             "unrealized_pl": -102, "unrealized_plpc": -0.36,
             "market_value": 180},
        ])
        out = _enriched_positions(ctx, profile_id)
        assert len(out) == 1
        # symbol is the underlying, NOT the OCC string
        assert out[0]["symbol"] == "PCG"
        assert out[0]["occ_symbol"] == "PCG260612C00017000"

    def test_stock_position_unchanged(self, tmp_profile_db):
        """Regression: stock positions still render symbol as ticker."""
        from views import _enriched_positions
        profile_id, _ = tmp_profile_db

        ctx = _ctx_with_positions([
            {"symbol": "AAPL", "qty": 10,
             "current_price": 155, "avg_entry_price": 150,
             "unrealized_pl": 50, "unrealized_plpc": 0.033,
             "market_value": 1550},
        ])
        out = _enriched_positions(ctx, profile_id)
        assert out[0]["symbol"] == "AAPL"
        assert out[0]["occ_symbol"] is None


class TestVirtualAuditAcceptsShortOptionLegs:
    def test_negative_qty_on_option_leg_not_flagged(self, tmp_path):
        """Multileg short legs are legitimately negative — the audit
        must not flag them. Caught 2026-05-11 after the sell-to-open
        fix made short legs visible."""
        db_path = str(tmp_path / "p.db")
        from journal import init_db
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        # Multileg short leg
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, "
            "price, fill_price, occ_symbol, signal_type, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-05-11T13:44:20", "RTX", "sell", 1.0, 3.15,
             3.15, "RTX260618P00170000", "MULTILEG", "open"),
        )
        conn.commit()
        conn.close()

        from virtual_audit import audit_virtual_profile
        with patch("journal.get_virtual_account_info",
                   return_value={"equity": 10000, "cash": 9700,
                                 "portfolio_value": 300}):
            problems = audit_virtual_profile(
                db_path, initial_capital=10000, profile_name="t",
            )
        assert not any("Negative position" in p for p in problems), (
            f"Audit incorrectly flagged short option leg: {problems}"
        )

    # The correct stock-short audit contract (legitimate stock
    # shorts must NOT be flagged) is pinned in
    # test_virtual_audit_distinguishes_legitimate_shorts.py.
