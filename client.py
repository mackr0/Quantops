"""Alpaca API client wrapper.

This module is the single interception point for the virtual-account
layer. When a profile has `is_virtual=True`, `get_positions()` and
`get_account_info()` return data from the internal trades ledger
instead of Alpaca. Orders still go through Alpaca normally.
"""

import alpaca_trade_api as tradeapi
import config


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


def _make_price_fetcher(api):
    """Return a callable that gets the current price for a symbol.
    Tries Alpaca bars first, then Alpaca last trade, then logs a
    warning instead of silently returning 0 (which would show
    phantom losses on virtual positions)."""
    _cache = {}

    def fetch(symbol):
        if symbol in _cache:
            return _cache[symbol]
        # Try 1: Alpaca bars (primary data path)
        try:
            from market_data import get_bars
            bars = get_bars(symbol, limit=1)
            if bars is not None and not bars.empty:
                price = float(bars.iloc[-1]["close"])
                if price > 0:
                    _cache[symbol] = price
                    return price
        except Exception:
            pass
        # Try 2: Alpaca last trade API
        try:
            trade = api.get_latest_trade(symbol)
            if trade and hasattr(trade, "price"):
                price = float(trade.price)
                if price > 0:
                    _cache[symbol] = price
                    return price
        except Exception:
            pass
        import logging
        logging.warning("Price fetch failed for %s — position will show stale price", symbol)
        return 0.0
    return fetch


def get_account_info(api=None, ctx=None):
    """Get account details: equity, buying power, etc.

    For virtual profiles, computes these from the internal trades ledger
    instead of calling Alpaca.
    """
    if ctx is not None and getattr(ctx, "is_virtual", False):
        from journal import get_virtual_account_info
        api = api or get_api(ctx)
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
