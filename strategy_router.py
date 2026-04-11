"""Strategy router -- routes to the correct strategy engine based on market type.

Each market type has its own tuned strategy engine:
  - micro:    strategy_micro.py   (Micro Cap $1-$5)
  - small:    strategy_small.py   (Small Cap $5-$20)
  - midcap:   strategy_mid.py     (Mid Cap $20-$100)
  - largecap: strategy_large.py   (Large Cap $50-$500)
  - crypto:   strategy_crypto.py  (Crypto)

Falls back to aggressive_combined_strategy for unknown market types.
"""


def run_strategy(symbol, market_type, ctx=None, df=None, params=None):
    """Route to the correct strategy engine based on market type.

    Parameters
    ----------
    symbol : str
        Ticker symbol (e.g. "AAPL", "BTC/USD").
    market_type : str
        One of "micro", "small", "midcap", "largecap", "crypto".
    ctx : UserContext, optional
        User context for credentials and parameters.
    df : DataFrame, optional
        Pre-fetched price data. If None, the strategy will fetch its own.
    params : dict, optional
        User-configurable strategy parameters (RSI thresholds, volume
        multipliers, strategy toggles, etc.).  When provided these override
        the hardcoded defaults inside each strategy engine.

    Returns
    -------
    dict
        Strategy signal dict with keys: symbol, signal, reason, price,
        score, votes, strategy_results.
    """
    if market_type == "micro":
        from strategy_micro import micro_combined_strategy
        return micro_combined_strategy(symbol, ctx=ctx, df=df, params=params)

    elif market_type == "small":
        from strategy_small import small_combined_strategy
        return small_combined_strategy(symbol, ctx=ctx, df=df, params=params)

    elif market_type == "midcap":
        from strategy_mid import mid_combined_strategy
        return mid_combined_strategy(symbol, ctx=ctx, df=df, params=params)

    elif market_type == "largecap":
        from strategy_large import large_combined_strategy
        return large_combined_strategy(symbol, ctx=ctx, df=df, params=params)

    elif market_type == "crypto":
        from strategy_crypto import crypto_combined_strategy
        return crypto_combined_strategy(symbol, ctx=ctx, df=df, params=params)

    else:
        # Fallback for unknown market types (including legacy "microsmall")
        from aggressive_strategy import aggressive_combined_strategy
        return aggressive_combined_strategy(symbol, df=df, params=params)
