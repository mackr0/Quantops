"""2026-06-22 — positions are marked at the BROKER's price, not mid.

A held position's correct value is what the account is actually worth: the
broker's mark (a long at the bid, a short at the ask — the realizable
side). Marking at the data-snapshot MID overstated option-heavy books by
~half the bid/ask spread per leg, so the dashboard read higher than the
real Alpaca account (a smaller cousin of the phantom-equity overstatement).

`client._make_price_fetcher` now prefers the broker's `current_price`
(from `list_positions`) for held symbols, falling back to the data snapshot
only for symbols the broker doesn't hold (e.g. a just-opened leg).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _pos(symbol, current_price):
    return SimpleNamespace(symbol=symbol, current_price=current_price)


@pytest.fixture(autouse=True)
def _clear_broker_mark_cache():
    """The broker-mark cache is process-wide; clear it around each test so
    cases don't bleed (and so the cache-behavior test starts cold)."""
    import client
    client._broker_mark_cache.clear()
    yield
    client._broker_mark_cache.clear()


def test_broker_marks_cached_not_refetched_within_ttl():
    """list_positions is read at most once per account per TTL — repeatedly
    building the fetcher (i.e. repeated renders) must NOT re-poll Alpaca.
    This is the no-hammering / no-UI-jank guarantee."""
    from client import _make_price_fetcher
    api = MagicMock()
    api._key_id = "acct-key-AAA"
    api.list_positions.return_value = [_pos("ZQXY", 42.5)]
    f1 = _make_price_fetcher(api)
    f2 = _make_price_fetcher(api)
    f3 = _make_price_fetcher(api)
    assert f1("ZQXY") == 42.5 and f2("ZQXY") == 42.5 and f3("ZQXY") == 42.5
    assert api.list_positions.call_count == 1, (
        "broker marks must be cached and reused within the TTL, not "
        "re-fetched on every render (Alpaca hammering / UI jank)")


def test_held_stock_marked_at_broker_price():
    from client import _make_price_fetcher
    api = MagicMock()
    api.list_positions.return_value = [_pos("ZQXY", 42.5)]
    fetch = _make_price_fetcher(api)
    assert fetch("ZQXY") == 42.5


def test_held_option_marked_at_broker_mark_not_mid():
    """An OCC option leg is marked at the broker's mark — NOT the
    option-premium mid (which is what overstated the book)."""
    from client import _make_price_fetcher
    occ = "AMZN260731C00245000"
    api = MagicMock()
    api.list_positions.return_value = [_pos(occ, 8.20)]
    # If this fell through to the mid path it would hit the network; the
    # broker-mark preference must short-circuit it for both sides.
    fetch = _make_price_fetcher(api)
    assert fetch(occ, side="sell") == 8.20
    assert fetch(occ, side="buy") == 8.20


def test_unheld_symbol_falls_back_to_snapshot():
    """A symbol the broker doesn't hold (e.g. a just-opened leg) falls
    back to the data-snapshot path (latest trade)."""
    from client import _make_price_fetcher
    api = MagicMock()
    api.list_positions.return_value = [_pos("ZQXY", 42.5)]  # different sym
    trade = MagicMock()
    trade.price = 99.0
    api.get_latest_trade.return_value = trade
    fetch = _make_price_fetcher(api)
    assert fetch("UNHELD_UNIQ_AAA") == 99.0


def test_broker_marks_failure_falls_back_gracefully():
    """If list_positions fails, marking must not crash — it falls back to
    the snapshot path (no regression)."""
    from client import _make_price_fetcher
    api = MagicMock()
    api.list_positions.side_effect = RuntimeError("broker down")
    trade = MagicMock()
    trade.price = 12.0
    api.get_latest_trade.return_value = trade
    fetch = _make_price_fetcher(api)
    assert fetch("UNHELD_UNIQ_BBB") == 12.0


def test_zero_broker_mark_falls_back():
    """A held position with a 0/None broker mark (bad data) falls back
    rather than marking the position at $0."""
    from client import _make_price_fetcher
    api = MagicMock()
    api.list_positions.return_value = [_pos("ZQXY2", 0.0)]
    trade = MagicMock()
    trade.price = 7.0
    api.get_latest_trade.return_value = trade
    fetch = _make_price_fetcher(api)
    assert fetch("ZQXY2") == 7.0
