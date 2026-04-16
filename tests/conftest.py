"""Shared fixtures for QuantOpsAI test suite."""

import os
import sys
import sqlite3
import tempfile
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set minimal env vars so modules can import without .env
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("DB_PATH", ":memory:")


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database path."""
    return str(tmp_path / "test.db")


@pytest.fixture
def tmp_profile_db(tmp_path):
    """Create a temporary per-profile database with journal tables."""
    db_path = str(tmp_path / "profile_test.db")
    from journal import init_db
    init_db(db_path)
    return db_path


@pytest.fixture
def tmp_main_db(tmp_path):
    """Create a temporary main database with all user/profile tables."""
    db_path = str(tmp_path / "main_test.db")
    import config
    original = config.DB_PATH
    config.DB_PATH = db_path
    try:
        from models import init_user_db
        init_user_db(db_path)
        yield db_path
    finally:
        config.DB_PATH = original


@pytest.fixture
def tmp_strategies_dir(tmp_path, monkeypatch):
    """Redirect strategy_generator's STRATEGIES_DIR to a per-test directory.

    Tests that render auto-strategies via `write_strategy_module()` should
    request this fixture so generated .py files don't leak into the real
    `strategies/` package.
    """
    d = tmp_path / "strategies"
    d.mkdir()
    monkeypatch.setattr("strategy_generator.STRATEGIES_DIR", str(d))
    return str(d)


@pytest.fixture
def sample_ctx():
    """Create a minimal UserContext for testing (no real API keys)."""
    from user_context import UserContext
    return UserContext(
        user_id=1,
        segment="small",
        display_name="Test",
        alpaca_api_key="test_key",
        alpaca_secret_key="test_secret",
        ai_provider="anthropic",
        ai_model="claude-haiku-4-5-20251001",
        ai_api_key="test_key",
        db_path=":memory:",
    )


@pytest.fixture
def sample_df():
    """Create a minimal DataFrame with OHLCV data for strategy testing.

    Column names use lowercase to match yfinance output and add_indicators().
    Strategies call _prepare_df which calls add_indicators if 'rsi' not present.
    We pre-populate all indicator columns so strategies skip add_indicators
    (avoiding network calls).
    """
    import pandas as pd
    import numpy as np

    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=60, freq="B")
    base_price = 10.0
    prices = base_price + np.cumsum(np.random.randn(60) * 0.3)
    prices = np.maximum(prices, 1.0)  # Keep positive

    highs = prices + abs(np.random.randn(60) * 0.5)
    lows = prices - abs(np.random.randn(60) * 0.5)
    lows = np.maximum(lows, 0.5)
    volumes = np.random.randint(100_000, 5_000_000, 60).astype(float)

    df = pd.DataFrame({
        "open": prices + np.random.randn(60) * 0.1,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes,
    }, index=dates)

    # Indicators (lowercase, matching add_indicators output)
    df["sma_20"] = df["close"].rolling(20).mean()
    df["sma_50"] = df["close"].rolling(50).mean()
    df["ema_12"] = df["close"].ewm(span=12).mean()
    df["sma_10"] = df["close"].rolling(10).mean()

    # RSI
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["close"].ewm(span=12).mean()
    ema26 = df["close"].ewm(span=26).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9).mean()
    df["macd_histogram"] = df["macd"] - df["macd_signal"]

    # Bollinger Bands
    df["bb_upper"] = df["sma_20"] + 2 * df["close"].rolling(20).std()
    df["bb_lower"] = df["sma_20"] - 2 * df["close"].rolling(20).std()
    df["bb_middle"] = df["sma_20"]

    # Volume average
    df["volume_sma_20"] = df["volume"].rolling(20).mean()

    # Rolling highs/lows
    df["high_20"] = df["high"].rolling(20).max()
    df["high_10"] = df["high"].rolling(10).max()
    df["low_10"] = df["low"].rolling(10).min()
    df["low_5"] = df["low"].rolling(5).min()

    return df
