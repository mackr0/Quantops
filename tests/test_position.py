"""Pin the Position class — single canonical representation for an
open position. Phase 1 of the Position class refactor (2026-05-11).

These tests pin:
1. OCC vs stock detection in both factories.
2. Attribute correctness — broker_symbol, display_symbol, is_option,
   is_short, abs_qty.
3. Back-compat shim — `pos["symbol"]`, `pos.get("qty")`, `"foo" in pos`,
   `dict(pos)` all work as if pos were the legacy dict.
4. The shim returns the UNDERLYING for `pos["symbol"]` regardless of
   whether the source was Alpaca (OCC) or virtual (underlying), so
   no consumer that does `pos["symbol"]` sees inconsistent meanings.
5. Defense-in-depth assertion: Position constructed as 'option' MUST
   have occ_symbol; .broker_symbol asserts on it.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from position import Position, _is_occ_symbol, _underlying_from_occ


class TestOccDetection:
    def test_unpadded_call(self):
        assert _is_occ_symbol("PCG260612C00017000")

    def test_unpadded_put(self):
        assert _is_occ_symbol("RTX260618P00170000")

    def test_padded_form(self):
        assert _is_occ_symbol("MSFT  261219P00395000")

    def test_stock_ticker_rejected(self):
        assert not _is_occ_symbol("AAPL")
        assert not _is_occ_symbol("BRK.B")

    def test_garbage_rejected(self):
        assert not _is_occ_symbol("")
        assert not _is_occ_symbol(None)
        assert not _is_occ_symbol("12345")
        # Right letter but wrong shape
        assert not _is_occ_symbol("AAPLC00017000")

    def test_underlying_extraction_unpadded(self):
        assert _underlying_from_occ("PCG260612C00017000") == "PCG"

    def test_underlying_extraction_padded(self):
        assert _underlying_from_occ("MSFT  261219P00395000") == "MSFT"


class TestFromAlpaca:
    def test_stock_position(self):
        ap = SimpleNamespace(
            symbol="AAPL", qty="100", avg_entry_price="150.00",
            current_price="155.00", market_value="15500.00",
            unrealized_pl="500.00", unrealized_plpc="0.0333",
        )
        p = Position.from_alpaca(ap)
        assert p.is_stock and not p.is_option
        assert p.underlying == "AAPL"
        assert p.occ_symbol is None
        assert p.broker_symbol == "AAPL"
        assert p.display_symbol == "AAPL"
        assert p.qty_signed == 100.0 and p.is_long and p.abs_qty == 100.0

    def test_option_position_unpadded_occ(self):
        ap = SimpleNamespace(
            symbol="PCG260612C00017000", qty="6",
            avg_entry_price="0.47", current_price="0.30",
            market_value="180.00", unrealized_pl="-102.00",
            unrealized_plpc="-0.36",
        )
        p = Position.from_alpaca(ap)
        assert p.is_option and not p.is_stock
        assert p.underlying == "PCG"
        assert p.occ_symbol == "PCG260612C00017000"
        assert p.broker_symbol == "PCG260612C00017000"
        assert p.display_symbol == "PCG"
        assert p.qty_signed == 6.0

    def test_short_option_negative_qty(self):
        ap = SimpleNamespace(
            symbol="PCG260612C00018000", qty="-6",
            avg_entry_price="0.01", current_price="1.00",
            market_value="-600.00", unrealized_pl="-594.00",
            unrealized_plpc="-99.0",
        )
        p = Position.from_alpaca(ap)
        assert p.is_option
        assert p.is_short and not p.is_long
        assert p.abs_qty == 6.0


class TestFromVirtualRow:
    def test_stock_row(self):
        row = {"symbol": "AAPL", "occ_symbol": None, "qty": 100,
               "avg_entry_price": 150, "current_price": 155,
               "market_value": 15500, "unrealized_pl": 500,
               "unrealized_plpc": 0.0333}
        p = Position.from_virtual_row(row)
        assert p.is_stock
        assert p.underlying == "AAPL"
        assert p.broker_symbol == "AAPL"

    def test_option_row(self):
        row = {"symbol": "PCG", "occ_symbol": "PCG260612C00017000",
               "qty": 6, "avg_entry_price": 0.47, "current_price": 0.30,
               "market_value": 180, "unrealized_pl": -102,
               "unrealized_plpc": -0.36}
        p = Position.from_virtual_row(row)
        assert p.is_option
        assert p.underlying == "PCG"
        assert p.occ_symbol == "PCG260612C00017000"
        assert p.broker_symbol == "PCG260612C00017000"
        assert p.display_symbol == "PCG"

    def test_virtual_short_option_leg(self):
        """A multileg short leg comes through with negative qty
        post-2026-05-11 sell-to-open fix."""
        row = {"symbol": "RTX", "occ_symbol": "RTX260618P00170000",
               "qty": -1, "avg_entry_price": 3.15,
               "current_price": 3.50, "market_value": -350,
               "unrealized_pl": -35, "unrealized_plpc": -0.11}
        p = Position.from_virtual_row(row)
        assert p.is_option and p.is_short
        assert p.abs_qty == 1.0


class TestBrokerSymbolInvariant:
    def test_option_must_have_occ_to_broker_route(self):
        """Defense-in-depth: an 'option' Position MUST have
        occ_symbol. Constructing one without and then asking for
        broker_symbol should fail loudly, not silently return the
        underlying (which would re-create the phantom-stock-stops
        bug)."""
        bad = Position(
            instrument_kind="option",
            underlying="PCG", occ_symbol=None,
            qty_signed=1, avg_entry_price=0.5, current_price=0.5,
            market_value=50, unrealized_pl=0, unrealized_plpc=0,
        )
        with pytest.raises(AssertionError, match="occ_symbol"):
            _ = bad.broker_symbol


class TestDictShimBackCompat:
    def _stock(self):
        ap = SimpleNamespace(
            symbol="AAPL", qty="100", avg_entry_price="150",
            current_price="155", market_value="15500",
            unrealized_pl="500", unrealized_plpc="0.033",
        )
        return Position.from_alpaca(ap)

    def _option(self):
        ap = SimpleNamespace(
            symbol="PCG260612C00017000", qty="6",
            avg_entry_price="0.47", current_price="0.30",
            market_value="180", unrealized_pl="-102",
            unrealized_plpc="-0.36",
        )
        return Position.from_alpaca(ap)

    def test_subscript_access(self):
        p = self._stock()
        assert p["symbol"] == "AAPL"
        assert p["qty"] == 100.0
        assert p["current_price"] == 155.0

    def test_get_with_default(self):
        p = self._stock()
        assert p.get("symbol") == "AAPL"
        # Unknown keys return default
        assert p.get("nonexistent") is None
        assert p.get("nonexistent", "fallback") == "fallback"
        # Optional fields default to None
        assert p.get("ai_confidence") is None

    def test_in_operator(self):
        p = self._stock()
        assert "symbol" in p
        assert "qty" in p
        assert "occ_symbol" in p
        assert "ai_confidence" in p
        assert "nonexistent" not in p

    def test_dict_conversion(self):
        """Some consumers do dict(pos) to materialize all fields."""
        p = self._stock()
        d = dict(p)
        assert d["symbol"] == "AAPL"
        assert d["qty"] == 100.0
        assert d["occ_symbol"] is None

    def test_subscript_returns_underlying_for_options_too(self):
        """The legacy pos['symbol'] returned the UNDERLYING for
        virtual-profile option positions (that's how the macro's
        is_option detection worked: occ_symbol present + symbol is
        the underlying). The Alpaca direct returned the OCC for
        options. The shim normalizes to the underlying so every
        existing consumer sees consistent behavior."""
        p = self._option()
        assert p["symbol"] == "PCG"  # underlying, NOT the OCC
        assert p["occ_symbol"] == "PCG260612C00017000"
        # The macro's is_option check still works:
        assert bool(p.get("occ_symbol"))


class TestEnrichmentFields:
    def test_enrichment_passed_through_factories(self):
        ap = SimpleNamespace(
            symbol="AAPL", qty="100", avg_entry_price="150",
            current_price="155", market_value="15500",
            unrealized_pl="500", unrealized_plpc="0.033",
        )
        p = Position.from_alpaca(
            ap, ai_confidence=78, ai_reasoning="strong setup",
            stop_loss=140.0, take_profit=170.0, side_label="buy",
            timestamp="2026-05-11T10:00:00",
        )
        assert p.ai_confidence == 78
        assert p.ai_reasoning == "strong setup"
        assert p.stop_loss == 140.0
        assert p.take_profit == 170.0
        assert p.side_label == "buy"
        assert p.timestamp == "2026-05-11T10:00:00"
        # Shim sees them too
        assert p["ai_confidence"] == 78
        assert p["side"] == "buy"
        assert p["timestamp"] == "2026-05-11T10:00:00"

    def test_enrichment_defaults_to_none(self):
        ap = SimpleNamespace(
            symbol="AAPL", qty="100", avg_entry_price="150",
            current_price="155", market_value="15500",
            unrealized_pl="500", unrealized_plpc="0.033",
        )
        p = Position.from_alpaca(ap)
        # Every optional field is None when not supplied
        for k in ("ai_confidence", "ai_reasoning", "stop_loss",
                  "take_profit", "decision_price", "fill_price",
                  "slippage_pct", "timestamp", "reason", "pnl"):
            assert p.get(k) is None
