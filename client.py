"""Alpaca API client wrapper."""

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


def get_account_info(api=None, ctx=None):
    """Get account details: equity, buying power, etc."""
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
    """Get all current positions."""
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
