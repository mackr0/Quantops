"""Alpaca API client wrapper.

This module is the single interception point for the virtual-account
layer. When a profile has `is_virtual=True`, `get_positions()` and
`get_account_info()` return data from the internal trades ledger
instead of Alpaca. Orders still go through Alpaca normally.
"""

import logging
import threading
import time
from typing import Dict, Tuple

import alpaca_trade_api as tradeapi
import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-wide price cache shared across web workers.
#
# Why: every dashboard render needs current prices for virtual positions.
# Without a shared cache, each gunicorn worker × each profile × each held
# symbol fired its own Alpaca call, hammered the rate limit, and timed
# out at 120s. Now: one batched snapshots() call per render, results
# cached for _PRICE_CACHE_TTL seconds and shared across all workers in
# the same process.
# ---------------------------------------------------------------------------
_PRICE_CACHE_TTL = 30.0  # seconds
_price_cache: dict = {}  # symbol -> (epoch_seconds, price)
_price_cache_lock = threading.Lock()

# Broker position marks (Alpaca's `current_price` per symbol) cached with a
# TTL so marking reads the broker's real account value without re-polling
# list_positions on every dashboard render. One list_positions call per
# account per TTL — keyed by the account's API key id so profiles sharing
# an account share the cache (Alpaca has no streaming entitlement, so we
# REST-poll once and reuse; never poll the same state twice).
_BROKER_MARK_TTL = 30.0  # seconds
_broker_mark_cache: dict = {}  # account_key -> (epoch_seconds, {symbol: price})
_broker_mark_lock = threading.Lock()


def _broker_marks(api) -> dict:
    """Return {SYMBOL: broker current_price} for the account behind `api`,
    cached for `_BROKER_MARK_TTL`s. The broker's mark IS the account's real
    value (a long marked at the bid, a short at the ask); marking at the
    data-snapshot mid instead overstates option-heavy books by ~half the
    bid/ask spread per leg. `current_price` is market-wide per symbol, so
    sharing across profiles on the same account is correct. Any failure
    returns {} so the caller falls back to the snapshot path (no break)."""
    key = str(getattr(api, "_key_id", "") or "") or "default"
    now = time.time()
    with _broker_mark_lock:
        entry = _broker_mark_cache.get(key)
        if entry is not None and (now - entry[0]) < _BROKER_MARK_TTL:
            return entry[1]
    marks: dict = {}
    try:
        for p in (api.list_positions() or []):
            sym = (getattr(p, "symbol", "") or "").upper().replace(" ", "")
            try:
                cp = float(getattr(p, "current_price", 0) or 0)
            except (TypeError, ValueError):
                cp = 0.0
            if sym and cp > 0:
                marks[sym] = cp
    except Exception:
        return {}
    with _broker_mark_lock:
        _broker_mark_cache[key] = (now, marks)
    return marks


def get_api(ctx=None):
    """Create and return an authenticated Alpaca API client.

    Parameters
    ----------
    ctx : UserContext, optional
        If provided, credentials are taken from the context (the
        per-profile key for that user's trading account).
        When *ctx* is None, credentials come from
        `market_data._resolve_alpaca_credentials()`, which sources
        from the `alpaca_accounts` master DB table. The env-var
        ("master key") path was removed 2026-05-19 — operator
        feedback was that env-level Alpaca keys repeatedly caused
        outages (stale/wrong/limited) and the per-account keys are
        the canonical source kept fresh by the trading workflow.
    """
    if ctx is not None:
        return ctx.get_alpaca_api()

    from market_data import _resolve_alpaca_credentials
    api_key, secret_key, base_url = _resolve_alpaca_credentials()
    if not api_key or not secret_key:
        raise ValueError(
            "No Alpaca credentials available. Add a paper account "
            "via the Settings page (writes to alpaca_accounts), or "
            "pass an explicit ctx with per-profile credentials."
        )
    # Wrap even the no-ctx client in the oversell door. This path is for
    # read-only data calls (no profile journal to check against), so its
    # submit_order refuses outright — an order can't be oversell-checked
    # without a per-profile ctx, and a naked sell must never slip through
    # the unguarded no-ctx door.
    from order_guard import guarded_api
    return guarded_api(
        tradeapi.REST(api_key, secret_key, base_url, api_version="v2"), None)


def _prefetch_prices(symbols):
    """Fetch latest prices for many symbols in a single Alpaca call.

    Uses Alpaca's batched `get_snapshots(symbols)` endpoint — one HTTP
    round trip returns the latest daily bar for every symbol — and
    populates the process-wide TTL cache. This is the only call site
    that should be making bar/snapshot requests on the web path.
    """
    if not symbols:
        return
    now = time.time()
    # Filter to symbols that aren't already cached (cuts payload size)
    needed = []
    with _price_cache_lock:
        for sym in symbols:
            entry = _price_cache.get(sym)
            if entry is None or (now - entry[0]) >= _PRICE_CACHE_TTL:
                needed.append(sym)
    if not needed:
        return
    try:
        from market_data import _get_alpaca_data_client
        data_client = _get_alpaca_data_client()
        if data_client is None:
            return
        # Chunk to be safe on payload size; Alpaca handles 1000+ at once.
        snaps = {}
        for i in range(0, len(needed), 200):
            chunk = needed[i:i + 200]
            try:
                snaps.update(data_client.get_snapshots(chunk))
            except (AttributeError, ValueError, TypeError, OSError,
                    ConnectionError, TimeoutError) as _ch_exc:
                # Per-chunk snapshot fetch loop; non-fatal per-chunk
                # — caller falls back to last-known cached price
                # (stale ok). Surface for follow-up.
                logger.debug(
                    "snapshot fetch failed for chunk (%d syms): %s: %s",
                    len(chunk), type(_ch_exc).__name__, _ch_exc,
                )
                continue
        with _price_cache_lock:
            for sym, snap in snaps.items():
                if snap is None:
                    continue
                daily = getattr(snap, "daily_bar", None)
                if daily is None:
                    continue
                try:
                    price = float(daily.c)
                except (TypeError, ValueError):
                    continue
                if price > 0:
                    _price_cache[sym] = (now, price)
    except (AttributeError, ValueError, TypeError, OSError,
            ConnectionError, TimeoutError) as _dc_exc:
        # Data-client wedge fallback. If wedged, leave the cache
        # untouched — callers fall back to stale prices, not break
        # the page. Surface for follow-up.
        logger.warning(
            "price-cache refresh wedged: %s: %s "
            "(callers will use stale cached prices)",
            type(_dc_exc).__name__, _dc_exc,
        )


def _is_occ_symbol(s):
    """Heuristic: an OCC option symbol is the underlying root (1-6
    chars) + YYMMDD (6 digits) + C/P + strike×1000 (8 digits).
    The padded form (`MSFT  261219P00395000`, 21 chars total) and
    the unpadded form (`MSFT261219P00395000`, 14-21 chars) both
    appear in the system: Alpaca's API accepts/returns unpadded;
    some internal builders pad to 21. Distinguishes either flavor
    from a stock ticker (`MSFT`, `BRK.B`)."""
    if not s or not isinstance(s, str):
        return False
    if len(s) < 14 or len(s) > 21:
        return False
    # Trailing 8 chars must be the strike (digits)
    if not s[-8:].isdigit():
        return False
    # Char at index -9 (just before the strike) must be C or P
    if s[-9] not in ("C", "P"):
        return False
    # The 6 chars before C/P must be YYMMDD (digits). Strip any
    # internal whitespace (padded form has spaces between root and
    # date) before checking.
    head = s[:-9].rstrip()  # root + (maybe trailing spaces) + YYMMDD
    if len(head) < 7:
        return False
    if not head[-6:].isdigit():
        return False
    return True


def _fetch_option_premium(occ_symbol, side="buy"):
    """Latest premium for an option contract by OCC symbol.

    `side` is the holder's position direction (`buy` = long,
    `sell` = short). It controls the one-sided-market fallback:
    a LONG position is valued at the bid (what the holder would
    receive selling to close), a SHORT position at the ask (what
    the holder would pay buying to close). Using the wrong side
    on an illiquid contract inflates the mark (e.g., bid=$0
    ask=$0.77 — a long holder cannot sell at $0.77, so marking
    the position at $0.77 fakes a gain that doesn't exist).

    Uses Alpaca's per-symbol snapshots endpoint
    (`/v1beta1/options/snapshots?symbols=<occ>`), which returns
    quote + last trade + daily bar in one request. Preference:
      1. Mid of bid/ask when both > 0 and ask >= bid (real market).
      2. Latest trade if available (representative recent fill).
      3. Daily bar close.
      4. Conservative side per position direction:
         - long  → bid  (holder's exit price)
         - short → ask  (holder's exit price)
      5. 0.0 — caller's FIFO falls back to entry price (current
         shows = entry, 0% unrealized; less misleading than a
         fake gain from the offer side of a one-sided market).

    OCC normalization: the journal stores the padded 21-char form
    (`WMT   260612P00117000`); Alpaca returns the unpadded form
    (`WMT260612P00117000`). Strip whitespace before sending.
    """
    import requests
    if not occ_symbol:
        return 0.0
    try:
        from options_chain_alpaca import _alpaca_headers, _ALPACA_DATA_BASE
    except Exception:
        return 0.0
    occ_unpadded = occ_symbol.replace(" ", "")
    if not occ_unpadded:
        return 0.0
    try:
        r = requests.get(
            f"{_ALPACA_DATA_BASE}/v1beta1/options/snapshots",
            headers=_alpaca_headers(),
            params={"symbols": occ_unpadded, "feed": "indicative"},
            timeout=10,
        )
        if r.status_code != 200:
            return 0.0
        snaps = (r.json() or {}).get("snapshots") or {}
        snap = snaps.get(occ_unpadded)
        if not snap:
            return 0.0
        q = snap.get("latestQuote") or {}
        ap = float(q.get("ap") or 0)
        bp = float(q.get("bp") or 0)
        if ap > 0 and bp > 0 and ap >= bp:
            return (ap + bp) / 2
        # Last trade — best single estimate when the quote is
        # one-sided / inverted / empty.
        t = snap.get("latestTrade") or {}
        tp = float(t.get("p") or 0)
        if tp > 0:
            return tp
        # Daily bar close — second fallback for off-hours / illiquid.
        bar = snap.get("dailyBar") or {}
        cp = float(bar.get("c") or 0)
        if cp > 0:
            return cp
        # Conservative side: use the holder's exit-side. A long
        # would receive the bid; a short would pay the ask.
        # Returning the OFFER side on a long position fakes a gain.
        if side == "buy":
            return bp if bp > 0 else 0.0
        if side == "sell":
            return ap if ap > 0 else 0.0
        return 0.0
    except Exception:
        return 0.0


def _fetch_option_quote(occ_symbol):
    """Latest (bid, ask) for an option contract by OCC symbol, or None when a
    two-sided market isn't available. Powers the REAL per-leg half-spread the
    selection engine charges as an option's transaction cost (so a spread ranks
    apples-to-apples with the cost-charged stock, not under-charged). Same
    snapshot endpoint as `_fetch_option_premium`; market data only, own-book
    safe. None on any failure — the caller falls back to a conservative fixed
    per-leg cost, never zero."""
    import requests
    if not occ_symbol:
        return None
    try:
        from options_chain_alpaca import _alpaca_headers, _ALPACA_DATA_BASE
    except Exception:
        return None
    occ_unpadded = occ_symbol.replace(" ", "")
    if not occ_unpadded:
        return None
    try:
        r = requests.get(
            f"{_ALPACA_DATA_BASE}/v1beta1/options/snapshots",
            headers=_alpaca_headers(),
            params={"symbols": occ_unpadded, "feed": "indicative"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        snap = ((r.json() or {}).get("snapshots") or {}).get(occ_unpadded)
        if not snap:
            return None
        q = snap.get("latestQuote") or {}
        ap = float(q.get("ap") or 0)
        bp = float(q.get("bp") or 0)
        if not (ap > 0 and bp > 0 and ap >= bp):
            return None         # not a real two-sided market
        # Sanity/staleness guard: a pathologically WIDE market (ask more than
        # ~3× bid, i.e. spread > ~100% of mid) is stale/illiquid, not a real
        # cost — reject it so a bad overnight quote can't crater a spread's RAR.
        # The caller then falls back to the conservative fixed per-leg cost.
        if ap > 3.0 * bp:
            return None
        return (bp, ap)         # (bid, ask)
    except Exception:
        return None


def _make_price_fetcher(api):
    """Return a callable that gets the current price for a symbol,
    backed by a process-wide TTL cache populated by `_prefetch_prices`.

    HELD positions are marked at the BROKER's reported `current_price`
    (`api.list_positions`) — the account's actual mark, which values a
    long at what you could sell it for and a short at what you'd pay to
    close it (the realizable side). This makes the journal's reported
    value equal the real Alpaca account. Marking at the data-snapshot MID
    instead overstated option-heavy books by ~half the bid/ask spread per
    leg (the dashboard read higher than the account is actually worth).

    The data-snapshot path (option-premium mid for OCC symbols, latest
    trade for stocks) is the FALLBACK — used only for a symbol the broker
    doesn't (yet) hold, e.g. a just-opened leg awaiting settlement, or
    when the broker mark is unavailable.
    """
    # The broker's own marks for currently-held positions — the
    # authoritative account valuation, cached with a TTL so we read
    # list_positions at most once per account per `_BROKER_MARK_TTL`s
    # (no per-render hammering of Alpaca; no UI jank). On any failure
    # `_broker_marks` returns {} and we fall back to the data snapshot.
    broker_marks = _broker_marks(api)

    def fetch(symbol, side="buy"):
        # Prefer the broker's mark for a held position — the real account
        # value (realizable side), so journal value == broker value and the
        # dashboard equals the actual account. Side-independent: the
        # broker's current_price IS the account mark.
        _bm = broker_marks.get((symbol or "").upper().replace(" ", ""))
        if _bm is not None and _bm > 0:
            return _bm
        now = time.time()
        # OCC option symbol: route to option-snapshot path. Cached
        # the same way as stocks but per-(symbol, side) since long
        # and short positions on the same contract take different
        # fallback marks (bid vs ask) when the market is one-sided.
        if _is_occ_symbol(symbol):
            cache_key = (symbol, side)
            with _price_cache_lock:
                entry = _price_cache.get(cache_key)
                if entry is not None and (now - entry[0]) < _PRICE_CACHE_TTL:
                    return entry[1]
            premium = _fetch_option_premium(symbol, side=side)
            if premium > 0:
                with _price_cache_lock:
                    _price_cache[cache_key] = (now, premium)
                return premium
            import logging
            logging.warning(
                "Option-premium fetch returned 0 for %s (%s) — leg "
                "will show entry as current; market may be one-sided",
                symbol, side,
            )
            return 0.0
        # Stock path
        with _price_cache_lock:
            entry = _price_cache.get(symbol)
            if entry is not None and (now - entry[0]) < _PRICE_CACHE_TTL:
                return entry[1]
        try:
            trade = api.get_latest_trade(symbol)
            if trade and hasattr(trade, "price"):
                price = float(trade.price)
                if price > 0:
                    with _price_cache_lock:
                        _price_cache[symbol] = (now, price)
                    return price
        except (AttributeError, ValueError, TypeError, OSError,
                ConnectionError, TimeoutError) as _lt_exc:
            # Live-trade price fetch fallback; warning logged below
            # before the stale-price fallback. Surface for follow-up.
            logger.debug(
                "live-trade price fetch failed for %s: %s: %s",
                symbol, type(_lt_exc).__name__, _lt_exc,
            )
        logger.warning("Price fetch failed for %s — position will show stale price", symbol)
        return 0.0
    return fetch


def _held_symbols_from_journal(db_path):
    """Return the set of symbols with currently-held lots in the journal.

    Used to batch-prefetch prices BEFORE calling into journal helpers
    that pass a price_fetcher per-symbol. One snapshots() call per page
    render instead of N bar() calls.
    """
    if not db_path:
        return []
    try:
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM trades WHERE symbol IS NOT NULL"
            ).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except Exception:
        return []


# P1.2 of LONG_SHORT_PLAN.md — borrow / shortable check.
# Cached in-memory because Alpaca's asset endpoint doesn't change
# often within a session and we hit it for every SHORT candidate.
_BORROW_CACHE: Dict[str, Tuple[float, Dict[str, bool]]] = {}
_BORROW_CACHE_TTL = 86400  # 24h


def get_borrow_info(symbol: str, api=None, ctx=None) -> Dict[str, bool]:
    """Return {'shortable': bool, 'easy_to_borrow': bool} for a symbol.

    Best-effort: Alpaca paper may report shortable=True for names that
    a real broker would refuse. Treat as a coarse filter, not ground
    truth. On any error returns shortable=True / easy_to_borrow=False
    so we don't accidentally block all shorts when the API hiccups.
    """
    import time
    cached = _BORROW_CACHE.get(symbol.upper())
    if cached and (time.time() - cached[0]) < _BORROW_CACHE_TTL:
        return cached[1]
    try:
        api = api or get_api(ctx)
        asset = api.get_asset(symbol)
        info = {
            "shortable": bool(getattr(asset, "shortable", True)),
            "easy_to_borrow": bool(getattr(asset, "easy_to_borrow", False)),
        }
    except Exception:
        info = {"shortable": True, "easy_to_borrow": False}
    _BORROW_CACHE[symbol.upper()] = (time.time(), info)
    return info


def get_account_info(api=None, ctx=None):
    """Get account details: equity, buying power, etc.

    For virtual profiles, computes these from the internal trades ledger
    instead of calling Alpaca.
    """
    if ctx is not None and getattr(ctx, "is_virtual", False):
        from journal import get_virtual_account_info
        api = api or get_api(ctx)
        # Batch-prefetch all symbols we might need so the per-symbol
        # fetcher only ever serves cache hits.
        _prefetch_prices(_held_symbols_from_journal(ctx.db_path))
        return get_virtual_account_info(
            db_path=ctx.db_path,
            initial_capital=getattr(ctx, "initial_capital", 100000.0),
            price_fetcher=_make_price_fetcher(api),
        )

    api = api or get_api(ctx)
    from broker_health import call_with_health_tracking
    account = call_with_health_tracking(api.get_account)
    return {
        "equity": float(account.equity),
        "buying_power": float(account.buying_power),
        "cash": float(account.cash),
        "portfolio_value": float(account.portfolio_value),
        "status": account.status,
    }


def get_positions(api=None, ctx=None):
    """Get all current positions.

    For virtual profiles, computes these from the internal trades ledger
    instead of calling Alpaca.
    """
    if ctx is not None and getattr(ctx, "is_virtual", False):
        from journal import get_virtual_positions
        api = api or get_api(ctx)
        _prefetch_prices(_held_symbols_from_journal(ctx.db_path))
        return get_virtual_positions(
            db_path=ctx.db_path,
            price_fetcher=_make_price_fetcher(api),
        )

    api = api or get_api(ctx)
    from broker_health import call_with_health_tracking
    from position import Position
    positions = call_with_health_tracking(api.list_positions)
    # Phase 1 of Position class refactor: returns List[Position]
    # instead of List[dict]. Position has a back-compat shim
    # (__getitem__ / .get / "in"), so every existing consumer that
    # does pos["symbol"] / pos.get("qty") keeps working unchanged.
    # New code uses pos.broker_symbol / pos.is_option / etc.
    # Phase 2+ migrates consumers off the dict shim.
    return [Position.from_alpaca(p) for p in positions]
