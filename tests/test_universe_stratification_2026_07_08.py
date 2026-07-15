"""Universe funnel rework (2026-07-08): sector-stratified dollar-volume
selection, 11-sector taxonomy, real ETF exclusion.

THE DIAGNOSIS (adversarially verified on prod): the universe was the
top-100 of ~8,000 Alpaca equities by RAW SHARE VOLUME, re-sorted by
share volume again to a top-50 — a leaderboard mega-cap tech and
crypto-momentum names own permanently. Measured: the entire 13-profile
fleet evaluated 14 distinct symbols in a full day, 34.6% of cycles had
an EMPTY shortlist, zero energy names reached the menu in 3 days,
~30/100 universe slots were ETFs leaking past a hand-typed blocklist,
and utilities/staples/materials/real-estate were UNREPRESENTABLE in
the 7-bucket taxonomy — while the AI's own market context told it to
rotate into exactly those sectors. Books converged on the same 11
tickers; concentration warnings then produced refusals (one phase) or
cap-starved idle cash (the other).

THE FIX pinned here: (1) 11-sector taxonomy aligned key-for-key with
market_data.SECTOR_ETFS + honest 'unclassified' default; (2) fund/ETF
exclusion by Alpaca's registered asset NAME; (3) stratified universe:
every sector gets floor slots filled by its most dollar-liquid names
passing the operator floors, top-3 momentum sectors get bonus slots,
remainder by global dollar-volume rank — widens what can COMPETE,
never forces what gets bought; (4) screen_by_price_range ranks by
dollar-ADV, not raw share count.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import screener
import sector_classifier as sc
from screener import _is_fund_name, _stratify_by_sector

_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)


def _read(rel):
    return open(os.path.join(_ROOT, rel)).read()


# ------------------------------------------------- taxonomy

def test_eleven_sectors_align_with_sector_etf_keys():
    # The rotation signal (market_data.get_sector_rotation →
    # macro_data.get_sector_momentum_ranking) keys by SECTOR_ETFS.
    # The taxonomy must match key-for-key or the stratifier's rotation
    # bonus silently never lands.
    from market_data import SECTOR_ETFS
    assert set(SECTOR_ETFS.keys()) == sc.INTERNAL_SECTORS
    assert len(sc.INTERNAL_SECTORS) == 11


def test_defensive_sectors_are_first_class():
    assert {"utilities", "consumer_staples", "materials",
            "real_estate"} <= sc.INTERNAL_SECTORS
    assert sc._GICS_TO_INTERNAL["Utilities"] == "utilities"
    assert sc._GICS_TO_INTERNAL["Consumer Defensive"] == "consumer_staples"
    assert sc._GICS_TO_INTERNAL["Basic Materials"] == "materials"
    assert sc._GICS_TO_INTERNAL["Real Estate"] == "real_estate"


@pytest.fixture
def fresh_db(tmp_path):
    return str(tmp_path / "master.db")


def test_bulk_cache_reader_no_network(fresh_db):
    sc._init_schema(fresh_db)
    conn = sqlite3.connect(fresh_db)
    stale = (datetime.utcnow() - timedelta(days=10)).isoformat()
    conn.execute("INSERT INTO sector_cache VALUES ('XOM','energy',"
                 "datetime('now'))")
    conn.execute("INSERT INTO sector_cache VALUES (?,?,?)",
                 ("OLD1", "tech", stale))
    conn.commit()
    conn.close()
    with patch("sector_classifier._yfinance_sector") as mock_yf:
        out = sc.get_sectors_cached_bulk(
            ["XOM", "OLD1", "NEE", "ZZZMISS"], db_path=fresh_db)
    mock_yf.assert_not_called()  # bulk reader NEVER fetches
    assert out["XOM"] == "energy"      # fresh cache hit
    assert "OLD1" not in out           # stale row ignored
    assert out["NEE"] == "utilities"   # fallback map (offline parity)
    assert "ZZZMISS" not in out        # miss → absent, not guessed


# ------------------------------------------------- fund detection

def test_fund_names_detected():
    for name in (
        "iShares Bitcoin Trust ETF",
        "VanEck Gold Miners ETF",
        "Schwab US Dividend Equity ETF",
        "KraneShares CSI China Internet ETF",
        "ProShares UltraPro Short QQQ",
        "Sprott Physical Silver Trust",
        "Direxion Daily South Korea Bull 3X Shares",
        "iShares Ethereum Trust ETF",
    ):
        assert _is_fund_name(name) is True, name


def test_operating_companies_not_detected():
    # the precision cases the token list was designed around
    for name in (
        "Northern Trust Corporation",      # bank, not a trust product
        "Invesco Ltd.",                    # the manager's own stock
        "Apple Inc. Common Stock",
        "Truist Financial Corporation",
        "Exxon Mobil Corporation",
        "NextEra Energy, Inc.",
        "",
        None,
    ):
        assert _is_fund_name(name) is False, name


# ------------------------------------------------- stratifier

def _pool(spec):
    """spec: [(sym, dollar_vol)] → pool tuples sorted by $vol desc."""
    rows = [(s, dv / 10.0, 10.0) for s, dv in spec]  # price 10
    return sorted(rows, key=lambda r: r[1] * r[2], reverse=True)


@pytest.fixture
def fake_sectors(monkeypatch):
    mapping = {}
    monkeypatch.setattr(sc, "get_sectors_cached_bulk",
                        lambda syms, db_path=None: {
                            s: mapping[s] for s in syms if s in mapping})
    monkeypatch.setattr(sc, "get_sector",
                        lambda s, db_path=None: mapping.get(
                            s, "unclassified"))
    import macro_data
    monkeypatch.setattr(macro_data, "get_sector_momentum_ranking",
                        lambda: {"top_3": []})
    return mapping


def test_every_sector_floor_honored_under_extreme_skew(fake_sectors):
    # 100x dollar-volume gap: 40 tech whales vs 1 name per other sector.
    # Pre-fix behavior kept only whales; every sector must now surface.
    spec = [(f"TEC{i}", 1e9) for i in range(40)]
    others = {"ENE1": "energy", "UTL1": "utilities",
              "STA1": "consumer_staples", "MAT1": "materials",
              "REA1": "real_estate", "HLT1": "healthcare",
              "FIN1": "finance", "IND1": "industrial",
              "CDS1": "consumer_disc", "CMS1": "comm_services"}
    spec += [(s, 1e7) for s in others]
    fake_sectors.update({f"TEC{i}": "tech" for i in range(40)})
    fake_sectors.update(others)
    out = _stratify_by_sector(_pool(spec), 40)
    for s in others:
        assert s in out, f"{s} lost its sector floor"
    # and tech still dominates the remainder on merit
    assert sum(1 for s in out if s.startswith("TEC")) >= 25


def test_rotation_bonus_expands_leading_sectors(fake_sectors, monkeypatch):
    import macro_data
    monkeypatch.setattr(macro_data, "get_sector_momentum_ranking",
                        lambda: {"top_3": ["energy", "utilities",
                                           "healthcare"]})
    spec = [(f"TEC{i}", 1e9) for i in range(30)]
    spec += [(f"ENE{i}", 1e6 - i) for i in range(10)]
    fake_sectors.update({f"TEC{i}": "tech" for i in range(30)})
    fake_sectors.update({f"ENE{i}": "energy" for i in range(10)})
    out = _stratify_by_sector(_pool(spec), 30)
    n_energy = sum(1 for s in out if s.startswith("ENE"))
    assert n_energy == screener._SECTOR_FLOOR_SLOTS + \
        screener._ROTATION_BONUS_SLOTS, (
        "a leading-momentum sector must get floor + bonus slots — this "
        "is what lets the AI act on its own rotation signal")


def test_unclassified_gets_no_floor(fake_sectors):
    # unknowns compete only in the global remainder — a wave of
    # unclassifiable tickers must never claim guaranteed slots
    spec = [(f"UNK{i}", 1e9) for i in range(20)]
    spec += [("ENE1", 1e6)]
    fake_sectors.update({"ENE1": "energy"})  # UNKs absent → unclassified
    out = _stratify_by_sector(_pool(spec), 10)
    assert "ENE1" in out          # classified name keeps its floor
    assert len(out) == 10         # remainder filled by the unknowns


def test_small_max_symbols_degrades_round_robin(fake_sectors):
    # max_symbols below the total floor budget: round-robin means one
    # slot per sector in rank order, not alphabet-first exhaustion
    others = {"ENE1": "energy", "UTL1": "utilities", "HLT1": "healthcare",
              "FIN1": "finance", "TEC1": "tech"}
    spec = [(s, 1e6) for s in others]
    fake_sectors.update(others)
    out = _stratify_by_sector(_pool(spec), 3)
    assert len(out) == 3
    assert len(set(out)) == 3     # three DIFFERENT sectors, no dupes


def test_stratifier_fails_open_to_dollar_volume(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("sector machinery down")
    monkeypatch.setattr(sc, "get_sectors_cached_bulk", _boom)
    pool = _pool([("AAA", 3e6), ("BBB", 2e6), ("CCC", 1e6)])
    out = _stratify_by_sector(pool, 2)
    assert out == ["AAA", "BBB"]  # pure dollar-volume top-N


def test_live_lookup_budget_bounded(fake_sectors, monkeypatch):
    # cache misses beyond the budget stay unclassified this refresh —
    # a cold cache must never stampede the fundamentals source
    calls = []
    monkeypatch.setattr(sc, "get_sector",
                        lambda s, db_path=None: calls.append(s) or
                        "unclassified")
    monkeypatch.setattr(screener, "_STRATIFY_MAX_LIVE_LOOKUPS", 7)
    spec = [(f"MISS{i}", 1e6 - i) for i in range(50)]
    _stratify_by_sector(_pool(spec), 20)
    assert len(calls) == 7


# ------------------------------------------------- price-range sort

def test_screen_by_price_range_preserves_stratified_input_order(monkeypatch):
    # 2026-07-09, the first-live-session lesson: the dynamic universe
    # arrives PRIORITY-ORDERED by the sector stratifier — ANY global
    # re-sort here (share volume before, dollar-ADV briefly) un-
    # stratifies it ahead of the top-`limit` cut, and the fleet's
    # first morning re-narrowed to 18/18 tech candidates. The output
    # must follow the INPUT order, whatever the liquidity ranks say.
    import pandas as pd

    def _bars(universe, limit=30):
        out = {}
        for sym, price, vol in (("UTIL", 40.0, 2_000_000),   # $80M ADV
                                ("MEGATECH", 60.0, 50_000_000),  # $3B ADV
                                ("STAPLE", 30.0, 3_000_000)):    # $90M ADV
            if sym in universe:
                out[sym] = pd.DataFrame({
                    "close": [price] * 30, "volume": [vol] * 30,
                    "high": [price] * 30, "low": [price] * 30,
                })
        return out

    monkeypatch.setattr(screener, "_get_bars_for_symbols", _bars)
    res = screener.screen_by_price_range(
        min_price=1.0, max_price=100.0, min_volume=100_000,
        min_adv=5_000_000, limit=10,
        universe=["UTIL", "MEGATECH", "STAPLE"])  # stratified order
    syms = [r["symbol"] for r in res]
    # a liquidity re-sort would put MEGATECH first; stratified
    # priority order must survive
    assert syms == ["UTIL", "MEGATECH", "STAPLE"]


def test_scheduler_candidate_cut_preserves_priority_order():
    # The candidate cut used to run on an arbitrary-order set() — the
    # second place the first live session re-narrowed to tech. It
    # must iterate "candidates" (stratified order) first and cut a
    # LIST, never a set. (2026-07-15: the fixed `symbols[:30]` became
    # the CORE + ROTATING window — core slots keep the stratified
    # head, the rest rotate deterministically by bucket — but the
    # list-not-set invariant is unchanged and stays pinned.)
    src = _read("multi_scheduler.py")
    region = src.split('for cat in ("candidates", "volume_surges",')[0]
    assert region.rstrip().endswith("symbols = []\n    _seen = set()".rstrip()) or \
        "symbols = []" in region[-400:], (
        "the screener union must be an order-preserving list")
    assert "core = symbols[:_CANDIDATE_CORE_SLOTS]" in src, (
        "the window must take its core from the ordered list's head")
    assert "result = list(symbols)[:30]" not in src, (
        "the arbitrary set-order cut must stay dead")
    assert "result = set(" not in src, (
        "no set() may ever produce the candidate window")


def test_candidate_window_core_plus_deterministic_rotation():
    # 2026-07-15 — the fixed first-30 cut froze the same 30 names for
    # a whole day fleet-wide, leaving every other floor-passer
    # structurally unreachable (prod 07-14: 47 passers, back 17 never
    # scanned). Contract: the head of the stratified order is ALWAYS
    # scanned; the remaining slots rotate deterministically with the
    # bucket index; every tail name appears within a bounded number of
    # buckets; output size never exceeds the pool bound.
    import multi_scheduler as ms

    symbols = [f"S{i:02d}" for i in range(47)]
    core_n = ms._CANDIDATE_CORE_SLOTS
    pool_n = ms._CANDIDATE_POOL_SIZE
    rot = pool_n - core_n
    tail = symbols[core_n:]

    def window(bucket):
        offset = (bucket * rot) % len(tail)
        return symbols[:core_n] + [
            tail[(offset + i) % len(tail)] for i in range(rot)]

    seen_tail = set()
    prev = None
    for b in range(10):
        w = window(b)
        assert len(w) == pool_n
        assert w[:core_n] == symbols[:core_n], "core must never rotate"
        assert len(set(w)) == len(w), "window must not repeat a name"
        # deterministic: same bucket → same window
        assert window(b) == w
        if prev is not None:
            assert w != prev, "consecutive buckets must advance the window"
        prev = w
        seen_tail.update(w[core_n:])
    assert seen_tail == set(tail), (
        "every floor-passer beyond the core must be scanned within a "
        "few buckets — that is the whole point of the rotation")

    # And the shipped code implements exactly this shape.
    src = _read("multi_scheduler.py")
    assert "offset = (now_bucket * rot_slots) % len(tail)" in src
    assert "[tail[(offset + i) % len(tail)] for i in range(rot_slots)]" in src


def test_second_cut_at_screen_by_price_range_is_gone():
    # 2026-07-15 — limit=50 was a latent un-stratifying truncation
    # ahead of the candidate window; the pool bound must live in ONE
    # place (the core+rotating window).
    src = _read("multi_scheduler.py")
    assert "limit=len(universe) if universe else 50," in src, (
        "screen_by_price_range must pass every floor-passer through — "
        "no second cut before the candidate window")


def test_legacy_single_user_cuts_are_order_preserving():
    # Same class, other paths (2026-07-15): main.py and scheduler.py
    # carried the identical set()-before-[:30] shape the cc0e0c3 fix
    # killed in multi_scheduler.
    for fname in ("main.py", "scheduler.py"):
        src = _read(fname)
        assert "symbols = list(symbols)[:30]" not in src, (
            f"{fname}: hash-order cut must stay dead")
        assert 'for cat in ("candidates", "volume_surges",' in src, (
            f"{fname}: candidates (stratified order) must lead the union")


# ------------------------------------------------- round-2 review pins

def test_live_lookup_pass_is_time_bounded(fake_sectors, monkeypatch):
    # Review H1: the 150-CALL cap bounds count, not time — a slow
    # fundamentals day must not stall the trading cycle. Lookups stop
    # once the wall-clock budget is spent.
    clock = {"t": 0.0}
    calls = []

    def _slow_lookup(s, db_path=None):
        calls.append(s)
        clock["t"] += 31.0  # each lookup "takes" 31s
        return "unclassified"

    monkeypatch.setattr(sc, "get_sector", _slow_lookup)
    import time as _time_mod
    monkeypatch.setattr(_time_mod, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(screener, "_STRATIFY_LOOKUP_BUDGET_SEC", 60)
    spec = [(f"MISS{i}", 1e6 - i) for i in range(50)]
    _stratify_by_sector(_pool(spec), 20)
    assert len(calls) == 2, (
        "the wall-clock budget must stop live lookups (2 x 31s > 60s) "
        "— count-capping alone lets a rate-limited day stall PASS 2")


def test_universe_build_is_single_flight():
    # Review H3: 13 profile workers share one universe; on daily cache
    # expiry only one thread may build (N x 150 live lookups otherwise).
    src = _read("screener.py")
    assert "_dynamic_build_lock = threading.Lock()" in src
    entry = src.split("def screen_dynamic_universe(")[1].split(
        "\ndef ")[0]
    assert "with _dynamic_build_lock:" in entry
    # double-checked: the cache is re-read INSIDE the lock
    locked_region = entry.split("with _dynamic_build_lock:")[1]
    assert "_dynamic_cache.get(cache_key)" in locked_region
    # cache-key versioned so the pre-rework disk cache can't serve
    # the old ETF-laden universe for its first 24h (review L7)
    assert '_v2"' in entry


def test_wisdomtree_the_company_is_not_barred(fake_sectors):
    # Review M2: the WISDOMTREE issuer token must not bar the ISSUER'S
    # OWN listed stock (WT). Allowlist, because dropping the token
    # would reopen recall ("...Fund"-named products).
    assert _is_fund_name("WisdomTree, Inc.") is True  # token does match
    assert "WT" in screener._FUND_ISSUER_STOCK_ALLOWLIST  # ...so allowlist
    src = _read("screener.py")
    dyn = src.split("def _screen_dynamic_universe_locked(")[1]
    assert "_FUND_ISSUER_STOCK_ALLOWLIST" in dyn.split(
        "equity_symbols.append")[0]


def test_unclassified_never_counts_toward_concentration():
    # Review M3: two unknowns sharing 'unclassified' says nothing —
    # both concentration counters must skip non-sectors.
    tp = _read("trade_pipeline.py")
    assert "if s in INTERNAL_SECTORS)" in tp, (
        "held-sector counter must scope to real sectors")
    bf = _read("book_fit.py")
    assert "if sector in INTERNAL_SECTORS:" in bf, (
        "book_fit same-sector counter must scope to real sectors")


def test_relative_weakness_uses_internal_sector_etf_map():
    # Review M4: the strategy's local GICS-keyed ETF map never matched
    # get_sector()'s internal keys — every symbol compared against SPY
    # since inception. It must use market_data.SECTOR_ETFS.
    src = _read("strategies/relative_weakness_in_strong_sector.py")
    assert "SECTOR_ETFS.get(" in src
    assert '_SECTOR_ETF.get(sector or "", "SPY")' not in src
    # sanity: internal keys actually resolve to ETFs
    from market_data import SECTOR_ETFS
    assert SECTOR_ETFS.get("utilities") == "XLU"
    assert SECTOR_ETFS.get("tech") == "XLK"


def test_unclassified_is_negative_cached(fresh_db):
    # Review M5: an unmappable name must not burn a live lookup on
    # every refresh forever — the unclassified outcome is cached with
    # the normal TTL and served as a hit by BOTH readers.
    with patch("sector_classifier._yfinance_sector",
               return_value=None) as mock_yf:
        assert sc.get_sector("ZZZNOPE", db_path=fresh_db) == "unclassified"
        assert mock_yf.call_count == 1
        assert sc.get_sector("ZZZNOPE", db_path=fresh_db) == "unclassified"
        assert mock_yf.call_count == 1  # second call = cache hit
    bulk = sc.get_sectors_cached_bulk(["ZZZNOPE"], db_path=fresh_db)
    assert bulk.get("ZZZNOPE") == "unclassified"  # stratifier skips it


def test_guess_sector_exception_is_not_tech(monkeypatch):
    # Review L3: the silently-counted-as-tech behavior must not
    # survive on the market_data fallback path.
    import market_data
    def _boom(sym):
        raise RuntimeError("classifier down")
    monkeypatch.setattr("sector_classifier.get_sector", _boom)
    assert market_data._guess_sector("XYZ") == "unclassified"


# ------------------------------------------------- structural pins

def test_dynamic_universe_wired_to_stratifier():
    src = open(os.path.join(_ROOT, "screener.py")).read()
    dyn = src.split("def screen_dynamic_universe(")[1]
    assert "_stratify_by_sector(" in dyn, (
        "the universe cut must be stratified — a raw top-N regression "
        "recreates the 14-symbol/day growth monoculture")
    assert "x[1] * x[2]" in dyn, "ranking must be DOLLAR volume"
    assert "_is_fund_name(" in dyn, (
        "asset filter must exclude funds by registered name")
    assert "_ETF_SUFFIXES = " not in src, (
        "the dead single-letter suffix heuristic must stay deleted "
        "(it would kill NFLX/FDX-class tickers if ever wired; the "
        "name may appear in comments explaining why)")
