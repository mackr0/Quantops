"""Tap-a-ticker company-info modal (2026-07-08).

Clicking/tapping a stock symbol anywhere in the UI opens a modal with
the standard company snapshot: name, market cap, sector, industry,
exchange, price + day change, 52-week range, volumes.

Pinned here (per the UI-surfaces-need-API-smoke-tests rule):
  • /api/symbol-info/<symbol> — happy path, unknown-symbol 404,
    option-contract (OCC) and garbage symbols rejected, login required.
  • symbol_info.get_symbol_info assembly — per-source degradation
    nulls a field, never the payload; unknown asset → None.
  • sector_classifier.get_company_profile — cache-first with TTL,
    negative caching (an unknown symbol doesn't refetch every tap),
    the Alpaca-inactive guard (no yfinance calls for symbols Alpaca
    doesn't carry — the Alpaca-first data rule), and the shared
    payload warming sector_cache.
  • Static wiring — the modal + capture-phase delegation live in
    base.html (capture: symbol cells sit inside rows with their own
    onclick expanders, the modal tap must not toggle the row), the
    symLink JS helper exists for JS-rendered tables, the target
    templates actually wrap their symbol cells, and the stylesheet
    version was bumped so phones don't render with the stale sheet.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import symbol_info
from symbol_info import _valid_symbol, get_symbol_info

_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)


def _read(rel):
    return open(os.path.join(_ROOT, rel)).read()


# ------------------------------------------------- symbol validation

def test_valid_symbols():
    for s in ("AAPL", "BRK.B", "MARA", "F", "SPY"):
        assert _valid_symbol(s) is True


def test_occ_and_garbage_rejected():
    assert _valid_symbol("NFLX260117C01000000") is False  # option contract
    assert _valid_symbol("../etc/passwd") is False
    assert _valid_symbol("aapl'; DROP TABLE") is False
    assert _valid_symbol("") is False


# ------------------------------------------------- assembly

@pytest.fixture
def stubbed_sources(monkeypatch):
    monkeypatch.setattr(symbol_info, "_asset_fields",
                        lambda s: {"name": "Apple Inc.",
                                   "exchange": "NASDAQ", "tradable": True})
    monkeypatch.setattr(symbol_info, "_price_fields",
                        lambda s: {"price": 195.5, "day_change_pct": 1.2,
                                   "volume": 5.1e7, "prev_close": 193.2})
    monkeypatch.setattr(symbol_info, "_range_fields",
                        lambda s: {"week52_high": 240.0,
                                   "week52_low": 160.0,
                                   "avg_volume": 4.8e7})
    import sector_classifier
    monkeypatch.setattr(sector_classifier, "get_company_profile",
                        lambda s, db_path=None: {
                            "name": "Apple Inc.", "industry": "Consumer "
                            "Electronics", "market_cap": 3.0e12,
                            "gics_sector": "Technology"})


def test_assembles_full_payload(stubbed_sources):
    info = get_symbol_info("aapl")
    assert info["symbol"] == "AAPL"
    assert info["name"] == "Apple Inc."
    assert info["market_cap"] == 3.0e12
    assert info["sector"] == "Technology"
    assert info["week52_high"] == 240.0
    assert info["day_change_pct"] == 1.2


def test_unknown_asset_returns_none(monkeypatch):
    monkeypatch.setattr(symbol_info, "_asset_fields", lambda s: None)
    assert get_symbol_info("ZZZZZ") is None


def test_degraded_source_nulls_field_not_payload(monkeypatch):
    monkeypatch.setattr(symbol_info, "_asset_fields",
                        lambda s: {"name": "Ford Motor Company",
                                   "exchange": "NYSE", "tradable": True})
    monkeypatch.setattr(symbol_info, "_price_fields",
                        lambda s: {"price": None, "day_change_pct": None,
                                   "volume": None, "prev_close": None})
    monkeypatch.setattr(symbol_info, "_range_fields",
                        lambda s: {"week52_high": None, "week52_low": None,
                                   "avg_volume": None})
    import sector_classifier

    def _boom(s, db_path=None):
        raise RuntimeError("fundamentals source down")
    monkeypatch.setattr(sector_classifier, "get_company_profile", _boom)
    info = get_symbol_info("F")
    assert info is not None
    assert info["name"] == "Ford Motor Company"
    assert info["market_cap"] is None  # degraded, not fatal


# ------------------------------------------------- company profile cache

@pytest.fixture
def prof_db(tmp_path):
    return str(tmp_path / "master.db")


def _fake_yf(monkeypatch, info_payload, calls):
    class _T:
        def __init__(self, sym):
            calls.append(sym)
            self.info = info_payload
    monkeypatch.setitem(sys.modules, "yfinance",
                        SimpleNamespace(Ticker=_T))


def test_profile_cache_first_then_ttl(prof_db, monkeypatch):
    import sector_classifier as sc
    import screener
    monkeypatch.setattr(screener, "is_alpaca_active", lambda s: True)
    calls = []
    _fake_yf(monkeypatch, {"longName": "NVIDIA Corporation",
                           "industry": "Semiconductors",
                           "marketCap": 2.9e12,
                           "sector": "Technology"}, calls)
    p1 = sc.get_company_profile("NVDA", db_path=prof_db)
    assert p1["name"] == "NVIDIA Corporation"
    assert p1["market_cap"] == 2.9e12
    assert calls == ["NVDA"]
    # second lookup: cache hit, no new fetch
    p2 = sc.get_company_profile("NVDA", db_path=prof_db)
    assert p2 == p1
    assert calls == ["NVDA"]
    # stale row → refetch
    conn = sqlite3.connect(prof_db)
    conn.execute("UPDATE company_profile_cache SET fetched_at = "
                 "datetime('now', '-8 days')")
    conn.commit()
    conn.close()
    sc.get_company_profile("NVDA", db_path=prof_db)
    assert calls == ["NVDA", "NVDA"]


def test_profile_negative_caching(prof_db, monkeypatch):
    # unknown company: all-None row cached so every tap doesn't refetch
    import sector_classifier as sc
    import screener
    monkeypatch.setattr(screener, "is_alpaca_active", lambda s: True)
    calls = []
    _fake_yf(monkeypatch, {}, calls)
    p = sc.get_company_profile("ZZZQ", db_path=prof_db)
    assert p["market_cap"] is None
    sc.get_company_profile("ZZZQ", db_path=prof_db)
    assert calls == ["ZZZQ"]  # one fetch, then negative-cache hits


def test_profile_respects_alpaca_first_guard(prof_db, monkeypatch):
    # symbols Alpaca doesn't carry never reach yfinance (Alpaca-first
    # rule — the guard the sector path already enforces)
    import sector_classifier as sc
    import screener
    monkeypatch.setattr(screener, "is_alpaca_active", lambda s: False)
    calls = []
    _fake_yf(monkeypatch, {"marketCap": 1}, calls)
    p = sc.get_company_profile("DELISTED", db_path=prof_db)
    assert p["market_cap"] is None
    assert calls == []


def test_profile_warms_sector_cache_from_same_payload(prof_db, monkeypatch):
    import sector_classifier as sc
    import screener
    monkeypatch.setattr(screener, "is_alpaca_active", lambda s: True)
    calls = []
    _fake_yf(monkeypatch, {"longName": "JPMorgan Chase & Co.",
                           "marketCap": 6.0e11,
                           "sector": "Financial Services"}, calls)
    sc.get_company_profile("JPM", db_path=prof_db)
    conn = sqlite3.connect(prof_db)
    row = conn.execute("SELECT sector FROM sector_cache WHERE "
                       "symbol='JPM'").fetchone()
    conn.close()
    assert row is not None  # ONE payload fed BOTH caches
    assert calls == ["JPM"]


# ------------------------------------------------- endpoint

@pytest.fixture
def client(tmp_main_db):
    import config
    config.DB_PATH = tmp_main_db
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        yield client


@pytest.fixture
def logged_in_client(client, tmp_main_db):
    import config
    config.DB_PATH = tmp_main_db
    from models import create_user
    create_user("test@test.com", "password123", "Test", is_admin=True)
    client.post("/login", data={
        "email": "test@test.com",
        "password": "password123",
    }, follow_redirects=True)
    return client


def test_endpoint_requires_login(client):
    resp = client.get("/api/symbol-info/AAPL")
    assert resp.status_code in (302, 401)


def test_endpoint_happy_path(logged_in_client, monkeypatch):
    monkeypatch.setattr(symbol_info, "get_symbol_info",
                        lambda s: {"symbol": "AAPL", "name": "Apple Inc.",
                                   "market_cap": 3.0e12, "price": 195.5})
    resp = logged_in_client.get("/api/symbol-info/AAPL")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["name"] == "Apple Inc."
    assert data["market_cap"] == 3.0e12


def test_endpoint_unknown_symbol_404(logged_in_client, monkeypatch):
    monkeypatch.setattr(symbol_info, "get_symbol_info", lambda s: None)
    resp = logged_in_client.get("/api/symbol-info/ZZZZZ")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_endpoint_rejects_occ_contract(logged_in_client):
    # real validator path (no stub): an option contract id is not a
    # company — 404, and no upstream fetch is attempted
    resp = logged_in_client.get("/api/symbol-info/NFLX260117C01000000")
    assert resp.status_code == 404


# ------------------------------------------------- static wiring pins

def test_modal_and_delegation_in_base_template():
    src = _read("templates/base.html")
    assert 'id="sym-modal-backdrop"' in src
    assert "/api/symbol-info/" in src
    assert "window.symLink" in src
    # CAPTURE-phase delegation: symbol cells live inside rows with
    # their own onclick expanders — the modal tap must win
    assert "}, true);" in src.split("var link = e.target.closest"
                                    "('.sym-link');")[1][:2000]
    assert "e.stopPropagation();" in src


def test_templates_wrap_symbol_cells():
    for rel, expected_min in (("templates/_trades_table.html", 3),
                              ("templates/dashboard.html", 1),
                              ("templates/performance.html", 2)):
        src = _read(rel)
        assert src.count('class="sym-link"') >= expected_min, (
            f"{rel} lost its tappable symbol wrapping")
    dash = _read("templates/dashboard.html")
    assert dash.count("symLink(") >= 2, (
        "dashboard JS-rendered tables must render tappable symbols")


def test_stylesheet_has_modal_and_version_bumped():
    css = _read("static/style.css")
    assert ".sym-modal-backdrop" in css
    assert ".sym-link" in css
    base = _read("templates/base.html")
    assert "style.css?v=20260702-mobilenav" not in base, (
        "stylesheet version must be bumped or phones keep the stale sheet")
