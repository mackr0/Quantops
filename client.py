"""Alpaca API client wrapper."""

import alpaca_trade_api as tradeapi
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL


def get_api():
    """Create and return an authenticated Alpaca API client."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise ValueError(
            "Missing API credentials. Copy .env.example to .env and add your keys."
        )
    return tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")


def get_account_info(api=None):
    """Get account details: equity, buying power, etc."""
    api = api or get_api()
    account = api.get_account()
    return {
        "equity": float(account.equity),
        "buying_power": float(account.buying_power),
        "cash": float(account.cash),
        "portfolio_value": float(account.portfolio_value),
        "status": account.status,
    }


def get_positions(api=None):
    """Get all current positions."""
    api = api or get_api()
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
