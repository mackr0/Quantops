"""Virtual benchmarks — static null portfolios tracked without a broker.

docs/25 item 1.12 / decision D6 (2026-08-23). Buy-Hold-SPY and the
Random-pick null are FIRE-ONCE portfolios: they are selected on day one
and never trade again. Running them through Alpaca bought nothing but
exposure to the 2026-07-07 broker wipe, $750K of paper-account capacity,
reset-script purchase steps, and three more books for the integrity
gate to reconcile. A static portfolio marked to market from daily
closes IS its broker value, so they live here instead:

  * `virtual_benchmarks`           — one row per benchmark: kind, seed,
                                      holdings chosen once, cash.
  * `virtual_benchmark_snapshots`  — one equity row per benchmark per
                                      ET day (the same series shape as
                                      a profile's `daily_snapshots`).
  * `virtual_benchmark_dividends`  — cash dividends credited on their
                                      payable date, so the benchmark
                                      stays comparable to broker-held
                                      arms (Alpaca paper credits them).

Because virtual replicas are free, the Random null runs as TEN
replicas — a real variance band instead of two draws.

What is deliberately NOT modeled, stated so an auditor can weigh it:
entry slippage (a one-time few-bps cost on day one — broker-held arms
pay spread on every trade, the benchmark would have paid it once) and
splits/spin-offs (the universe is large-cap; a split would show as a
price halving — `mark_to_market` logs a loud warning when any holding
moves more than 40% in a day so it cannot pass silently).

Data: Alpaca bars via `market_data.get_bars` and Alpaca's corporate-
actions announcements endpoint (the Alpaca-first data rule); the
credentials are the data-account ones `market_data` already resolves.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import random
import sqlite3
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

KIND_BUY_HOLD = "buy_hold"
KIND_RANDOM = "random"
VALID_KINDS = (KIND_BUY_HOLD, KIND_RANDOM)

SPY_SYMBOL = "SPY"
RANDOM_PICK_COUNT = 5          # mirrors simple_strategies.RANDOM_PICK_COUNT
CASH_BUFFER = 0.05             # mirrors simple_strategies.CASH_BUFFER
DEFAULT_RANDOM_REPLICAS = 10
BUY_HOLD_NAME = "BENCH-BuyHoldSPY"
RANDOM_NAME_FMT = "BENCH-Random-{:02d}"
# A holding moving more than this in one day is almost certainly a
# corporate action (split) rather than a price move — never silent.
SUSPICIOUS_DAY_MOVE = 0.40

_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS virtual_benchmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            seed INTEGER NOT NULL,
            initial_capital REAL NOT NULL,
            start_date TEXT NOT NULL,
            holdings_json TEXT NOT NULL,
            cash REAL NOT NULL,
            dividends_to_date REAL NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS virtual_benchmark_snapshots (
            benchmark_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            equity REAL NOT NULL,
            cash REAL NOT NULL,
            positions_value REAL NOT NULL,
            dividends_to_date REAL NOT NULL,
            UNIQUE(benchmark_id, date)
        );
        CREATE TABLE IF NOT EXISTS virtual_benchmark_dividends (
            benchmark_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            ex_date TEXT NOT NULL,
            payable_date TEXT,
            rate REAL NOT NULL,
            qty REAL NOT NULL,
            amount REAL NOT NULL,
            credited_on TEXT NOT NULL,
            UNIQUE(benchmark_id, symbol, ex_date)
        );
    """)


def _conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    from models import _get_conn
    conn = _get_conn(db_path)
    ensure_tables(conn)
    return conn


# ---------------------------------------------------------------------------
# Selection — deterministic, replayable
# ---------------------------------------------------------------------------

def stable_seed(label: str) -> int:
    """A seed that is the same in every process. Python's hash() of a
    str is salted per interpreter run (PYTHONHASHSEED), so the
    `hash(("random_baseline_v2", pid))` the broker path used was only
    stable because its fire-once guard never let it re-run."""
    # 63 bits so the value fits SQLite's signed 64-bit INTEGER column.
    return int.from_bytes(
        hashlib.sha256(label.encode("utf-8")).digest()[:8], "big"
    ) & 0x7FFF_FFFF_FFFF_FFFF


def pick_random_symbols(seed: int, universe: List[str], n: int,
                        priced: Optional[Dict[str, float]] = None
                        ) -> List[str]:
    """Seeded sample from `universe`. When `priced` is given, symbols
    without a price are skipped and the NEXT symbols of the same
    seeded sequence are taken, so the pick is still a pure function of
    (seed, universe, which symbols had prices)."""
    rng = random.Random(seed)
    order = list(universe)
    rng.shuffle(order)
    picks: List[str] = []
    for sym in order:
        if priced is not None and not priced.get(sym):
            continue
        picks.append(sym)
        if len(picks) >= n:
            break
    return picks


# ---------------------------------------------------------------------------
# Prices and dividends (Alpaca-first)
# ---------------------------------------------------------------------------

def latest_close(symbol: str) -> Optional[float]:
    """Most recent daily close for `symbol`, or None (logged)."""
    try:
        from market_data import get_bars
        df = get_bars(symbol, limit=5)
    except Exception as exc:
        logger.warning("virtual_benchmarks: bars failed for %s: %s: %s",
                       symbol, type(exc).__name__, exc)
        return None
    try:
        if df is None or len(df) == 0:
            logger.warning("virtual_benchmarks: no bars for %s", symbol)
            return None
        px = float(df["close"].iloc[-1])
        return px if px > 0 else None
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        logger.warning("virtual_benchmarks: unreadable bars for %s: %s",
                       symbol, exc)
        return None


def fetch_cash_dividends(symbol: str, since: str, until: str
                         ) -> List[Dict[str, Any]]:
    """Cash-dividend announcements for `symbol` with ex_date in
    [since, until] from Alpaca's corporate-actions endpoint. Returns
    [{symbol, ex_date, payable_date, rate}]. The endpoint caps the
    window at 90 days; callers pass a rolling window. Any failure is
    logged and returns [] — a missed dividend is retried on the next
    daily run because crediting is keyed by ex_date."""
    try:
        from market_data import _resolve_alpaca_credentials
        key, secret, base_url = _resolve_alpaca_credentials()
    except Exception as exc:
        logger.warning("virtual_benchmarks: credential resolve failed: "
                       "%s: %s", type(exc).__name__, exc)
        return []
    if not key or not secret:
        logger.warning("virtual_benchmarks: no Alpaca data credentials — "
                       "dividends for %s not fetched", symbol)
        return []
    qs = urllib.parse.urlencode({
        "ca_types": "dividend", "since": since, "until": until,
        "symbol": symbol, "date_type": "ex_date",
    })
    url = f"{base_url.rstrip('/')}/v2/corporate_actions/announcements?{qs}"
    try:
        req = urllib.request.Request(
            url, headers={"APCA-API-KEY-ID": key,
                          "APCA-API-SECRET-KEY": secret})
        with urllib.request.urlopen(req, timeout=20) as r:
            items = json.loads(r.read())
    except Exception as exc:
        logger.warning("virtual_benchmarks: dividend fetch failed for %s "
                       "(%s..%s): %s: %s", symbol, since, until,
                       type(exc).__name__, exc)
        return []
    out: List[Dict[str, Any]] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        if (it.get("ca_type") or "").lower() != "dividend":
            continue
        if (it.get("ca_sub_type") or "cash").lower() != "cash":
            continue
        try:
            rate = float(it.get("cash") or 0)
        except (TypeError, ValueError):
            continue
        if rate <= 0 or not it.get("ex_date"):
            continue
        out.append({
            "symbol": (it.get("initiating_symbol") or symbol).upper(),
            "ex_date": str(it["ex_date"])[:10],
            "payable_date": (str(it.get("payable_date"))[:10]
                             if it.get("payable_date") else None),
            "rate": rate,
        })
    return out


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def _et_today() -> str:
    return _dt.datetime.now(_ET).date().isoformat()


def create_benchmark(user_id: int, name: str, kind: str,
                     initial_capital: float, *,
                     seed: Optional[int] = None,
                     start_date: Optional[str] = None,
                     universe: Optional[List[str]] = None,
                     price_fn=latest_close,
                     db_path: Optional[str] = None) -> int:
    """Create one benchmark and choose its holdings ONCE at today's
    closes. Idempotent on `name` (returns the existing id). Raises on
    a kind it doesn't know or when no holding could be priced — a
    benchmark with nothing in it would be a silent zero."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown benchmark kind {kind!r}")
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM virtual_benchmarks WHERE name=?", (name,)
        ).fetchone()
        if row:
            logger.info("virtual_benchmarks: %s exists (id=%s)", name, row[0])
            return int(row[0])

        investable = initial_capital * (1.0 - CASH_BUFFER)
        holdings: List[Dict[str, Any]] = []
        if seed is None:
            seed = stable_seed(name)
        if kind == KIND_BUY_HOLD:
            px = price_fn(SPY_SYMBOL)
            if not px:
                raise RuntimeError("cannot price SPY — benchmark not created")
            qty = int(investable // px)
            holdings.append({"symbol": SPY_SYMBOL, "qty": qty,
                             "entry_price": px})
        else:
            if universe is None:
                from segments import STOCK_UNIVERSE
                universe = list(STOCK_UNIVERSE)
            # Price the seeded candidates in sequence; a symbol with no
            # bars is skipped deterministically (see pick_random_symbols).
            rng = random.Random(seed)
            order = list(universe)
            rng.shuffle(order)
            priced: Dict[str, float] = {}
            for sym in order:
                if len(priced) >= RANDOM_PICK_COUNT:
                    break
                px = price_fn(sym)
                if px:
                    priced[sym] = px
            if len(priced) < RANDOM_PICK_COUNT:
                raise RuntimeError(
                    f"only {len(priced)} of {RANDOM_PICK_COUNT} random "
                    f"picks could be priced — benchmark not created")
            per_pick = investable / RANDOM_PICK_COUNT
            for sym, px in priced.items():
                holdings.append({"symbol": sym, "qty": int(per_pick // px),
                                 "entry_price": px})
        spent = sum(h["qty"] * h["entry_price"] for h in holdings)
        cash = round(initial_capital - spent, 2)
        start = start_date or _et_today()
        cur = conn.execute(
            "INSERT INTO virtual_benchmarks (user_id, name, kind, seed, "
            "initial_capital, start_date, holdings_json, cash, "
            "dividends_to_date, enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,0,1,?)",
            (user_id, name, kind, int(seed), float(initial_capital), start,
             json.dumps(holdings), cash,
             _dt.datetime.now(_dt.timezone.utc).isoformat()))
        bid = int(cur.lastrowid)
        # Day-zero snapshot so the series starts at exactly 0.00%.
        conn.execute(
            "INSERT OR REPLACE INTO virtual_benchmark_snapshots "
            "(benchmark_id, date, equity, cash, positions_value, "
            "dividends_to_date) VALUES (?,?,?,?,?,0)",
            (bid, start, float(initial_capital), cash, spent))
        conn.commit()
        logger.info("virtual_benchmarks: created %s (%s) id=%d seed=%d "
                    "holdings=%s cash=%.2f", name, kind, bid, seed,
                    [(h["symbol"], h["qty"]) for h in holdings], cash)
        return bid
    finally:
        conn.close()


def create_standard_set(user_id: int, initial_capital: float, *,
                        random_replicas: int = DEFAULT_RANDOM_REPLICAS,
                        start_date: Optional[str] = None,
                        price_fn=latest_close,
                        universe: Optional[List[str]] = None,
                        db_path: Optional[str] = None) -> List[int]:
    """Buy-Hold-SPY plus `random_replicas` Random-pick benchmarks, all at
    the same capital. Idempotent by name."""
    ids = [create_benchmark(user_id, BUY_HOLD_NAME, KIND_BUY_HOLD,
                            initial_capital, start_date=start_date,
                            price_fn=price_fn, db_path=db_path)]
    for i in range(1, random_replicas + 1):
        ids.append(create_benchmark(
            user_id, RANDOM_NAME_FMT.format(i), KIND_RANDOM,
            initial_capital, start_date=start_date, price_fn=price_fn,
            universe=universe, db_path=db_path))
    return ids


# ---------------------------------------------------------------------------
# Daily mark-to-market
# ---------------------------------------------------------------------------

def _credit_dividends(conn: sqlite3.Connection, bench: sqlite3.Row,
                      holdings: List[Dict[str, Any]], as_of: str,
                      dividend_fn=fetch_cash_dividends) -> float:
    """Credit every cash dividend whose ex_date is on/after the
    benchmark's start and whose payable date is on/before `as_of`,
    once. Returns the amount credited this run."""
    start = bench["start_date"]
    since = max(start, (_dt.date.fromisoformat(as_of)
                        - _dt.timedelta(days=89)).isoformat())
    credited = 0.0
    for h in holdings:
        for d in dividend_fn(h["symbol"], since, as_of):
            if d["ex_date"] < start:
                continue
            pay = d.get("payable_date") or d["ex_date"]
            if pay > as_of:
                continue
            amount = round(d["rate"] * float(h["qty"]), 2)
            try:
                conn.execute(
                    "INSERT INTO virtual_benchmark_dividends (benchmark_id, "
                    "symbol, ex_date, payable_date, rate, qty, amount, "
                    "credited_on) VALUES (?,?,?,?,?,?,?,?)",
                    (bench["id"], d["symbol"], d["ex_date"],
                     d.get("payable_date"), d["rate"], h["qty"], amount,
                     as_of))
            except sqlite3.IntegrityError:
                continue  # already credited (UNIQUE on benchmark/symbol/ex)
            credited += amount
    return credited


def mark_to_market(*, as_of: Optional[str] = None,
                   user_id: Optional[int] = None,
                   price_fn=latest_close,
                   dividend_fn=fetch_cash_dividends,
                   db_path: Optional[str] = None) -> Dict[str, Any]:
    """Write today's equity snapshot for every enabled benchmark.
    Returns {"marked": n, "failed": n, "details": [...]}. A benchmark
    with an unpriceable holding is NOT written (a fabricated equity is
    worse than a gap) and is counted as failed — loudly."""
    as_of = as_of or _et_today()
    conn = _conn(db_path)
    conn.row_factory = sqlite3.Row
    out: Dict[str, Any] = {"marked": 0, "failed": 0, "details": []}
    try:
        q = "SELECT * FROM virtual_benchmarks WHERE enabled=1"
        args: Tuple = ()
        if user_id is not None:
            q += " AND user_id=?"
            args = (user_id,)
        for bench in conn.execute(q, args).fetchall():
            holdings = json.loads(bench["holdings_json"])
            prev = conn.execute(
                "SELECT equity, positions_value FROM "
                "virtual_benchmark_snapshots WHERE benchmark_id=? AND "
                "date<? ORDER BY date DESC LIMIT 1",
                (bench["id"], as_of)).fetchone()
            positions_value = 0.0
            failed = False
            for h in holdings:
                px = price_fn(h["symbol"])
                if not px:
                    logger.error("virtual_benchmarks: %s: no price for %s "
                                 "— snapshot for %s NOT written",
                                 bench["name"], h["symbol"], as_of)
                    failed = True
                    break
                if h.get("entry_price") and prev is not None:
                    # Per-symbol jump check against entry (cheap proxy
                    # when per-symbol history isn't kept): flags splits.
                    ref = float(h["entry_price"])
                    if ref > 0 and abs(px / ref - 1.0) > SUSPICIOUS_DAY_MOVE \
                            and as_of <= (
                                _dt.date.fromisoformat(bench["start_date"])
                                + _dt.timedelta(days=10)).isoformat():
                        logger.warning(
                            "virtual_benchmarks: %s: %s moved %.0f%% from "
                            "entry within 10 days — check for a corporate "
                            "action", bench["name"], h["symbol"],
                            (px / ref - 1.0) * 100)
                positions_value += px * float(h["qty"])
            if failed:
                out["failed"] += 1
                out["details"].append({"name": bench["name"], "ok": False})
                continue
            credited = _credit_dividends(conn, bench, holdings, as_of,
                                         dividend_fn=dividend_fn)
            cash = float(bench["cash"]) + credited
            divs = float(bench["dividends_to_date"]) + credited
            equity = round(cash + positions_value, 2)
            if prev is not None and float(prev["equity"]) > 0:
                jump = equity / float(prev["equity"]) - 1.0
                if abs(jump) > SUSPICIOUS_DAY_MOVE:
                    logger.warning(
                        "virtual_benchmarks: %s equity moved %.0f%% in one "
                        "mark — check holdings for a corporate action",
                        bench["name"], jump * 100)
            conn.execute(
                "INSERT OR REPLACE INTO virtual_benchmark_snapshots "
                "(benchmark_id, date, equity, cash, positions_value, "
                "dividends_to_date) VALUES (?,?,?,?,?,?)",
                (bench["id"], as_of, equity, round(cash, 2),
                 round(positions_value, 2), round(divs, 2)))
            if credited:
                conn.execute(
                    "UPDATE virtual_benchmarks SET cash=?, "
                    "dividends_to_date=? WHERE id=?",
                    (round(cash, 2), round(divs, 2), bench["id"]))
            out["marked"] += 1
            out["details"].append({"name": bench["name"], "ok": True,
                                   "equity": equity, "credited": credited})
        conn.commit()
    finally:
        conn.close()
    if out["failed"]:
        logger.error("virtual_benchmarks: %d benchmark(s) NOT marked on %s",
                     out["failed"], as_of)
    return out


def run_daily_if_due(user_id: Optional[int] = None, *,
                     as_of: Optional[str] = None,
                     db_path: Optional[str] = None, **kw) -> Optional[Dict[str, Any]]:
    """Idempotent daily entry point for the scheduler: marks only when
    some enabled benchmark lacks today's snapshot. Returns the
    mark_to_market summary, or None when nothing was due."""
    as_of = as_of or _et_today()
    conn = _conn(db_path)
    try:
        q = ("SELECT COUNT(*) FROM virtual_benchmarks b WHERE b.enabled=1 "
             "AND NOT EXISTS (SELECT 1 FROM virtual_benchmark_snapshots s "
             "WHERE s.benchmark_id=b.id AND s.date=?)")
        args: Tuple = (as_of,)
        if user_id is not None:
            q += " AND b.user_id=?"
            args = (as_of, user_id)
        due = conn.execute(q, args).fetchone()[0]
    finally:
        conn.close()
    if not due:
        return None
    return mark_to_market(as_of=as_of, user_id=user_id, db_path=db_path, **kw)


# ---------------------------------------------------------------------------
# Read path — series for the comparative chart and the scoreboard
# ---------------------------------------------------------------------------

def list_benchmarks(user_id: Optional[int] = None,
                    db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _conn(db_path)
    conn.row_factory = sqlite3.Row
    try:
        q = "SELECT * FROM virtual_benchmarks WHERE enabled=1"
        args: Tuple = ()
        if user_id is not None:
            q += " AND user_id=?"
            args = (user_id,)
        q += " ORDER BY id"
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
        for r in rows:
            r["holdings"] = json.loads(r.pop("holdings_json"))
        return rows
    finally:
        conn.close()


def equity_series(benchmark_id: int,
                  db_path: Optional[str] = None) -> List[Tuple[str, float]]:
    conn = _conn(db_path)
    try:
        return [(r[0], float(r[1])) for r in conn.execute(
            "SELECT date, equity FROM virtual_benchmark_snapshots "
            "WHERE benchmark_id=? ORDER BY date", (benchmark_id,))]
    finally:
        conn.close()


def list_series(user_id: Optional[int] = None,
                db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Series in the exact shape `comparative_returns.build_payload`
    emits for profiles, so the dashboard chart renders benchmarks with
    no new display code. `profile_id` is the NEGATIVE benchmark id so
    it can never collide with a trading profile; `virtual` is True."""
    out: List[Dict[str, Any]] = []
    for b in list_benchmarks(user_id, db_path=db_path):
        series = equity_series(b["id"], db_path=db_path)
        base = series[0][1] if series else 0.0
        points = ([{"date": d, "return_pct": round((e / base - 1.0) * 100, 4)}
                   for d, e in series] if base > 0 else [])
        out.append({
            "profile_id": -int(b["id"]),
            "profile_name": b["name"],
            "strategy_type": b["kind"],
            "initial_capital": float(b["initial_capital"]),
            "points": points,
            "virtual": True,
        })
    return out
