"""Alpaca API client wrapper.

This module is the single interception point for the virtual-account
layer. When a profile has `is_virtual=True`, `get_positions()` and
`get_account_info()` return data from the internal trades ledger
instead of Alpaca. Orders still go through Alpaca normally.
"""

import threading
import time

import alpaca_trade_api as tradeapi
import config

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


def get_api(ctx=None):
    """Create and return an authenticated Alpaca API client.

    Parameters
    ----------
    ctx : UserContext, optional
        If provided, credentials are taken from the context instead of
        module-level config globals.  When *ctx* is None the existing
        config.* behaviour is preserved (backward compat for CLI).
    """
    if ctx is not None:
        return ctx.get_alpaca_api()

    api_key = config.ALPACA_API_KEY
    secret_key = config.ALPACA_SECRET_KEY
    if not api_key or not secret_key:
        raise ValueError(
            "Missing API credentials. Copy .env.example to .env and add your keys."
        )
    return tradeapi.REST(api_key, secret_key, config.ALPACA_BASE_URL, api_version="v2")


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
            except Exception:
                # Per-chunk failure is non-fatal; we'll fall back to
                # last-known cached price (stale ok) for those symbols.
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
    except Exception:
        # If the data client is wedged, leave the cache untouched —
        # callers will fall back to stale prices, not break the page.
        pass


def _make_price_fetcher(api):
    """Return a callable that gets the current price for a symbol,
    backed by a process-wide TTL cache populated by `_prefetch_prices`.

    Per-symbol fallback to `api.get_latest_trade` only fires if the
    batched snapshot path didn't yield a price for that symbol — in
    practice, only when Alpaca itself returns a `None` snapshot for
    a delisted/halted ticker.
    """
    def fetch(symbol):
        now = time.time()
        with _price_cache_lock:
            entry = _price_cache.get(symbol)
            if entry is not None and (now - entry[0]) < _PRICE_CACHE_TTL:
                return entry[1]
        # Cache miss / stale: try a one-symbol latest_trade call.
        # `_make_price_fetcher` callers should call `_prefetch_prices`
        # with the full symbol list FIRST so this path is rare.
        try:
            trade = api.get_latest_trade(symbol)
            if trade and hasattr(trade, "price"):
                price = float(trade.price)
                if price > 0:
                    with _price_cache_lock:
                        _price_cache[symbol] = (now, price)
                    return price
        except Exception:
            pass
        import logging
        logging.warning("Price fetch failed for %s — position will show stale price", symbol)
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
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM trades WHERE symbol IS NOT NULL"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows if r and r[0]]
    except Exception:
        return []


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
    account = api.get_account()
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
    positions = api.list_positions()
    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc),
            "current_price": float(p.current_price),
            "avg_entry_price": float(p.avg_entry_price),
        }
        for p in positions
    ]
