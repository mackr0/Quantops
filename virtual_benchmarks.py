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
    # 2026-08-23 — activation at the first session's OPEN (operator:
    # the comparable start is the moment the arms can first trade, not
    # the prior close). Additive migration for tables created earlier
    # the same day.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(virtual_benchmarks)")}
    if "activated" not in cols:
        conn.execute("ALTER TABLE virtual_benchmarks ADD COLUMN activated "
                     "INTEGER NOT NULL DEFAULT 1")
    if "activation_date" not in cols:
        conn.execute("ALTER TABLE virtual_benchmarks ADD COLUMN activation_date TEXT")
    conn.commit()


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


def day_open(symbol: str, date_str: str) -> Optional[float]:
    """The OPEN of `symbol`'s daily bar dated `date_str`, or None when
    that bar doesn't exist yet (logged). Used once, at activation."""
    try:
        from market_data import get_bars
        df = get_bars(symbol, limit=5)
    except Exception as exc:
        logger.warning("virtual_benchmarks: bars failed for %s: %s: %s",
                       symbol, type(exc).__name__, exc)
        return None
    try:
        if df is None or len(df) == 0:
            return None
        for idx, row in df.iterrows():
            d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            if d == date_str:
                px = float(row["open"])
                return px if px > 0 else None
        logger.info("virtual_benchmarks: no %s bar dated %s yet", symbol, date_str)
        return None
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
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


def _min_price_floor() -> float:
    """The operator's universe price floor (config.SCREEN_MIN_PRICE).
    The AI arms can only trade names at or above it, so the Random
    null must sample the same eligible population — a null drawn from
    sub-floor names (the first live draw on 2026-08-23 picked CGC at
    ~$1, LCID, PARA) measures a different universe than the arms."""
    try:
        import config
        return float(getattr(config, "SCREEN_MIN_PRICE", 0.0) or 0.0)
    except Exception as exc:
        logger.warning("virtual_benchmarks: price floor unavailable (%s) — "
                       "no floor applied", exc)
        return 0.0


def create_benchmark(user_id: int, name: str, kind: str,
                     initial_capital: float, *,
                     seed: Optional[int] = None,
                     start_date: Optional[str] = None,
                     universe: Optional[List[str]] = None,
                     price_fn=latest_close,
                     min_price: Optional[float] = None,
                     activate_at_open: bool = False,
                     db_path: Optional[str] = None) -> int:
    """Create one benchmark and choose its SYMBOLS once. Idempotent on
    `name` (returns the existing id). Raises on a kind it doesn't know
    or when no holding could be priced — a benchmark with nothing in
    it would be a silent zero.

    `activate_at_open=False`: shares and entry prices are set now, at
    the latest closes (the series starts at the capital today).
    `activate_at_open=True`: symbols are fixed now (eligibility by the
    latest close), but shares and entry prices are set by
    `mark_to_market` from the OPEN of the first session on/after
    `start_date` — the same moment the arms can first trade — with the
    whole capital held as cash until then. Operator ruling 2026-08-23.

    `min_price` (default: the operator's universe floor) excludes
    sub-floor names from the Random draw deterministically — the next
    symbols of the same seeded sequence are taken instead."""
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
            floor = _min_price_floor() if min_price is None else float(min_price)
            rng = random.Random(seed)
            order = list(universe)
            rng.shuffle(order)
            priced: Dict[str, float] = {}
            for sym in order:
                if len(priced) >= RANDOM_PICK_COUNT:
                    break
                px = price_fn(sym)
                if not px:
                    continue
                if px < floor:
                    logger.info("virtual_benchmarks: %s skipped %s at %.2f "
                                "(below the $%.2f universe floor)",
                                name, sym, px, floor)
                    continue
                priced[sym] = px
            if len(priced) < RANDOM_PICK_COUNT:
                raise RuntimeError(
                    f"only {len(priced)} of {RANDOM_PICK_COUNT} random "
                    f"picks could be priced — benchmark not created")
            per_pick = investable / RANDOM_PICK_COUNT
            for sym, px in priced.items():
                holdings.append({"symbol": sym, "qty": int(per_pick // px),
                                 "entry_price": px})
        start = start_date or _et_today()
        if activate_at_open:
            # Symbols fixed; shares/entry set at the first session's open.
            for h in holdings:
                h["qty"] = 0
                h["entry_price"] = None
            spent = 0.0
            cash = float(initial_capital)
        else:
            spent = sum(h["qty"] * h["entry_price"] for h in holdings)
            cash = round(initial_capital - spent, 2)
        cur = conn.execute(
            "INSERT INTO virtual_benchmarks (user_id, name, kind, seed, "
            "initial_capital, start_date, holdings_json, cash, "
            "dividends_to_date, enabled, created_at, activated, "
            "activation_date) VALUES (?,?,?,?,?,?,?,?,0,1,?,?,?)",
            (user_id, name, kind, int(seed), float(initial_capital), start,
             json.dumps(holdings), cash,
             _dt.datetime.now(_dt.timezone.utc).isoformat(),
             0 if activate_at_open else 1,
             None if activate_at_open else start))
        bid = int(cur.lastrowid)
        if not activate_at_open:
            # Day-zero snapshot so the series starts at exactly 0.00%.
            conn.execute(
                "INSERT OR REPLACE INTO virtual_benchmark_snapshots "
                "(benchmark_id, date, equity, cash, positions_value, "
                "dividends_to_date) VALUES (?,?,?,?,?,0)",
                (bid, start, float(initial_capital), cash, spent))
        conn.commit()
        logger.info("virtual_benchmarks: created %s (%s) id=%d seed=%d "
                    "holdings=%s cash=%.2f%s", name, kind, bid, seed,
                    [(h["symbol"], h["qty"]) for h in holdings], cash,
                    f" — pending activation at the {start} open"
                    if activate_at_open else "")
        return bid
    finally:
        conn.close()


def create_standard_set(user_id: int, initial_capital: float, *,
                        random_replicas: int = DEFAULT_RANDOM_REPLICAS,
                        start_date: Optional[str] = None,
                        price_fn=latest_close,
                        universe: Optional[List[str]] = None,
                        min_price: Optional[float] = None,
                        activate_at_open: bool = False,
                        db_path: Optional[str] = None) -> List[int]:
    """Buy-Hold-SPY plus `random_replicas` Random-pick benchmarks, all at
    the same capital. Idempotent by name."""
    ids = [create_benchmark(user_id, BUY_HOLD_NAME, KIND_BUY_HOLD,
                            initial_capital, start_date=start_date,
                            price_fn=price_fn,
                            activate_at_open=activate_at_open,
                            db_path=db_path)]
    for i in range(1, random_replicas + 1):
        ids.append(create_benchmark(
            user_id, RANDOM_NAME_FMT.format(i), KIND_RANDOM,
            initial_capital, start_date=start_date, price_fn=price_fn,
            universe=universe, min_price=min_price,
            activate_at_open=activate_at_open, db_path=db_path))
    return ids


def standard_set_names(random_replicas: int = DEFAULT_RANDOM_REPLICAS) -> List[str]:
    return [BUY_HOLD_NAME] + [RANDOM_NAME_FMT.format(i)
                              for i in range(1, random_replicas + 1)]


def _activate(conn: sqlite3.Connection, bench: sqlite3.Row,
              holdings: List[Dict[str, Any]], as_of: str,
              open_fn) -> Optional[List[Dict[str, Any]]]:
    """Set shares and entry prices from `as_of`'s OPEN. Returns the
    activated holdings, or None (nothing written) when any open is
    missing — activation then retries on the next session, logged."""
    opens: Dict[str, float] = {}
    for h in holdings:
        px = open_fn(h["symbol"], as_of)
        if not px:
            logger.error("virtual_benchmarks: %s: no %s open for %s — "
                         "activation deferred to the next session",
                         bench["name"], as_of, h["symbol"])
            return None
        opens[h["symbol"]] = px
    capital = float(bench["initial_capital"])
    investable = capital * (1.0 - CASH_BUFFER)
    per = investable if bench["kind"] == KIND_BUY_HOLD else investable / len(holdings)
    for h in holdings:
        h["entry_price"] = opens[h["symbol"]]
        h["qty"] = int(per // opens[h["symbol"]])
    spent = sum(h["qty"] * h["entry_price"] for h in holdings)
    cash = round(capital - spent, 2)
    conn.execute(
        "UPDATE virtual_benchmarks SET holdings_json=?, cash=?, activated=1, "
        "activation_date=? WHERE id=?",
        (json.dumps(holdings), cash, as_of, bench["id"]))
    logger.info("virtual_benchmarks: %s ACTIVATED at the %s open: %s "
                "cash=%.2f", bench["name"], as_of,
                [(h["symbol"], h["qty"], h["entry_price"]) for h in holdings],
                cash)
    return holdings


def delete_benchmarks(names: List[str], db_path: Optional[str] = None) -> int:
    """Remove benchmarks (and their snapshots / dividend rows) by name.
    Used only on day zero to redraw a flawed set; returns rows removed."""
    if not names:
        return 0
    conn = _conn(db_path)
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM virtual_benchmarks WHERE name IN (%s)"
            % ",".join("?" * len(names)), names)]
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM virtual_benchmark_snapshots WHERE benchmark_id IN ({q})", ids)
        conn.execute(f"DELETE FROM virtual_benchmark_dividends WHERE benchmark_id IN ({q})", ids)
        conn.execute(f"DELETE FROM virtual_benchmarks WHERE id IN ({q})", ids)
        conn.commit()
        logger.warning("virtual_benchmarks: deleted %d benchmark(s): %s",
                       len(ids), names)
        return len(ids)
    finally:
        conn.close()


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
                   open_fn=day_open,
                   db_path: Optional[str] = None) -> Dict[str, Any]:
    """Write today's equity snapshot for every enabled benchmark.
    Returns {"marked": n, "failed": n, "pending": n, "details": [...]}.
    A benchmark with an unpriceable holding is NOT written (a
    fabricated equity is worse than a gap) and is counted as failed —
    loudly. A not-yet-activated benchmark is activated from today's
    OPEN when today is on/after its start date (then marked at the
    close like any other); before its start date it is counted as
    pending and nothing is written."""
    as_of = as_of or _et_today()
    conn = _conn(db_path)
    conn.row_factory = sqlite3.Row
    out: Dict[str, Any] = {"marked": 0, "failed": 0, "pending": 0,
                           "details": []}
    try:
        q = "SELECT * FROM virtual_benchmarks WHERE enabled=1"
        args: Tuple = ()
        if user_id is not None:
            q += " AND user_id=?"
            args = (user_id,)
        for bench in conn.execute(q, args).fetchall():
            holdings = json.loads(bench["holdings_json"])
            if not int(bench["activated"] or 0):
                if as_of < str(bench["start_date"]):
                    out["pending"] += 1
                    out["details"].append({"name": bench["name"],
                                           "ok": True, "pending": True})
                    continue
                activated = _activate(conn, bench, holdings, as_of, open_fn)
                if activated is None:
                    out["failed"] += 1
                    out["details"].append({"name": bench["name"], "ok": False,
                                           "reason": "open unavailable"})
                    continue
                holdings = activated
                bench = conn.execute("SELECT * FROM virtual_benchmarks WHERE id=?",
                                     (bench["id"],)).fetchone()
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


def activate_pending(user_id: Optional[int] = None, *,
                     as_of: Optional[str] = None,
                     open_fn=day_open,
                     db_path: Optional[str] = None) -> int:
    """Activate pending benchmarks whose start date has arrived, WITHOUT
    writing a snapshot (2026-08-24): activation belongs minutes after
    the open — the shares come from the opening print — while the daily
    equity row belongs to the end-of-day snapshot task. Hooking both
    into the snapshot task left benchmarks 'Pending' all session on day
    one. Cheap no-op when nothing is pending; returns the number
    activated."""
    as_of = as_of or _et_today()
    conn = _conn(db_path)
    conn.row_factory = sqlite3.Row
    activated = 0
    try:
        q = ("SELECT * FROM virtual_benchmarks WHERE enabled=1 AND "
             "activated=0 AND start_date <= ?")
        args: Tuple = (as_of,)
        if user_id is not None:
            q += " AND user_id=?"
            args = (as_of, user_id)
        for bench in conn.execute(q, args).fetchall():
            holdings = json.loads(bench["holdings_json"])
            if _activate(conn, bench, holdings, as_of, open_fn) is not None:
                activated += 1
        conn.commit()
    finally:
        conn.close()
    return activated


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


# Live-price cache: one bulk market-data read at most every TTL
# seconds regardless of how often the dashboard refreshes. This is
# pure display plumbing — Alpaca market data, the same feed every
# profile mark uses; no AI call is ever involved in benchmark values.
_LIVE_PRICE_TTL_SECONDS = 60
_live_price_cache: Dict[str, Any] = {"at": 0.0, "key": None, "prices": {}}


def live_prices(symbols: List[str]) -> Dict[str, float]:
    """Latest bar close per symbol in ONE bulk market-data read (minute
    bars — fresh enough for a display mark), cached for
    _LIVE_PRICE_TTL_SECONDS. {} on any failure, logged — the caller
    renders absence, never a fabricated number."""
    import time as _time
    symbols = sorted({s for s in symbols if s})
    if not symbols:
        return {}
    key = ",".join(symbols)
    now = _time.time()
    if (_live_price_cache["key"] == key
            and now - _live_price_cache["at"] < _LIVE_PRICE_TTL_SECONDS
            and _live_price_cache["prices"]):
        return dict(_live_price_cache["prices"])
    try:
        from market_data import _resolve_alpaca_credentials
        import alpaca_trade_api as tradeapi
        k, s, base = _resolve_alpaca_credentials()
        if not k or not s:
            logger.warning("virtual_benchmarks: no data credentials for "
                           "live prices")
            return {}
        api = tradeapi.REST(k, s, base, api_version="v2")
        bars = api.get_latest_bars(symbols)
        prices = {sym: float(bar.c) for sym, bar in bars.items()
                  if getattr(bar, "c", None)}
        _live_price_cache.update({"at": now, "key": key, "prices": prices})
        return dict(prices)
    except Exception as exc:
        logger.warning("virtual_benchmarks: live prices failed for %d "
                       "symbols: %s: %s", len(symbols),
                       type(exc).__name__, exc)
        return {}


def dashboard_rows(user_id: Optional[int] = None,
                   db_path: Optional[str] = None,
                   price_map: Optional[Dict[str, float]] = None
                   ) -> List[Dict[str, Any]]:
    """Rows for the dashboard's Reference Benchmarks section: name,
    kind, status, holdings, start capital, value, return, last mark.

    Value (2026-08-24): a LIVE mark — cash + Σ qty × latest bar close,
    one bulk quote call — so the benchmarks move on the dashboard the
    way profile equity does (`value_kind: "live"`); when live prices
    are unavailable, the latest end-of-day snapshot is shown
    (`value_kind: "close"`); with neither, absent — never fabricated.
    The persisted daily series is untouched (the evening mark stays
    the close). `price_map` injects prices for tests."""
    benches = list_benchmarks(user_id, db_path=db_path)
    if price_map is None:
        price_map = live_prices([
            h["symbol"] for b in benches if b.get("activated", 1)
            for h in b["holdings"]
        ])
    rows: List[Dict[str, Any]] = []
    for b in benches:
        series = equity_series(b["id"], db_path=db_path)
        latest = series[-1] if series else None
        cap = float(b["initial_capital"])
        activated = bool(b.get("activated", 1))
        if activated:
            status = f"Active since the {b.get('activation_date') or b['start_date']} open"
            holdings = ", ".join(f"{h['symbol']} ×{h['qty']}" for h in b["holdings"])
        else:
            status = f"Pending — shares set at the {b['start_date']} open"
            holdings = ", ".join(h["symbol"] for h in b["holdings"])
        value = None
        value_kind = None
        if activated and b["holdings"] and all(
                price_map.get(h["symbol"]) for h in b["holdings"]):
            value = round(float(b["cash"]) + sum(
                float(h["qty"]) * price_map[h["symbol"]]
                for h in b["holdings"]), 2)
            value_kind = "live"
        elif latest:
            value = latest[1]
            value_kind = "close"
        rows.append({
            "name": b["name"],
            "kind": "Buy & hold SPY" if b["kind"] == KIND_BUY_HOLD else "Random 5-pick",
            "status": status,
            "holdings": holdings,
            "initial_capital": cap,
            "value": value,
            "value_kind": value_kind,
            "return_pct": (round((value / cap - 1.0) * 100, 2)
                           if value is not None and cap > 0 else None),
            "last_mark": latest[0] if latest else None,
            "dividends_to_date": float(b.get("dividends_to_date") or 0.0),
        })
    return rows


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
