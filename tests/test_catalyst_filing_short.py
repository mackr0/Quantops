"""P3.2 of LONG_SHORT_PLAN.md — catalyst_filing_short strategy."""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_profile_db(tmp_path):
    db = str(tmp_path / "p.db")
    from journal import init_db
    init_db(db)
    return db


def _bars_with_drop(prices):
    """Build a DataFrame matching get_bars output."""
    n = len(prices)
    today = datetime.utcnow().date()
    timestamps = [(today - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [1_000_000] * n,
    })


def _insert_filing(db_path, symbol, days_ago, **flags):
    filed_date = (datetime.utcnow().date() - timedelta(days=days_ago)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sec_filings_history "
        "(symbol, accession_number, form_type, filed_date, "
        " going_concern_flag, material_weakness_flag, "
        " alert_severity, alert_signal) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            symbol,
            f"acc-{symbol}-{days_ago}",
            flags.get("form_type", "8-K"),
            filed_date,
            1 if flags.get("going_concern") else 0,
            1 if flags.get("material_weakness") else 0,
            flags.get("severity", None),
            flags.get("signal", None),
        ),
    )
    conn.commit()
    conn.close()


def test_module_has_required_interface():
    from strategies import catalyst_filing_short as m
    assert m.NAME == "catalyst_filing_short"
    assert callable(m.find_candidates)


def test_in_strategy_registry():
    from strategies import STRATEGY_MODULES
    assert "strategies.catalyst_filing_short" in STRATEGY_MODULES


def test_in_catalyst_short_set():
    from trade_pipeline import _CATALYST_SHORT_STRATEGIES
    assert "catalyst_filing_short" in _CATALYST_SHORT_STRATEGIES


def test_no_candidates_when_no_filings(tmp_profile_db):
    from strategies.catalyst_filing_short import find_candidates

    class Ctx:
        db_path = tmp_profile_db

    results = find_candidates(Ctx(), ["AAPL", "TSLA"])
    assert results == []


def test_no_candidates_when_filing_too_old(tmp_profile_db):
    """Filings older than 30 days fall outside the continuation window."""
    from strategies.catalyst_filing_short import find_candidates

    _insert_filing(tmp_profile_db, "TSLA", days_ago=60, going_concern=True)

    class Ctx:
        db_path = tmp_profile_db

    bars = _bars_with_drop([100, 95, 90, 85, 80])
    with patch("market_data.get_bars", return_value=bars):
        results = find_candidates(Ctx(), ["TSLA"])
    assert results == []


def test_candidate_emitted_for_going_concern_with_price_drop(tmp_profile_db):
    from strategies.catalyst_filing_short import find_candidates

    _insert_filing(tmp_profile_db, "TSLA", days_ago=10, going_concern=True)

    class Ctx:
        db_path = tmp_profile_db

    # 30 bars trending around 100, then a clear drop to 85 (15% below
    # filing-day reference close)
    prices = [100] * 25 + [95, 90, 87, 85, 84]
    bars = _bars_with_drop(prices)
    with patch("market_data.get_bars", return_value=bars):
        results = find_candidates(Ctx(), ["TSLA"])
    assert len(results) == 1
    r = results[0]
    assert r["symbol"] == "TSLA"
    assert r["signal"] == "SHORT"
    assert r["score"] == 3
    assert "going-concern" in r["reason"]


def test_no_candidate_when_price_rallied_after_filing(tmp_profile_db):
    """Filing was concerning but stock rallied → market shrugged it off,
    don't short."""
    from strategies.catalyst_filing_short import find_candidates

    _insert_filing(tmp_profile_db, "TSLA", days_ago=10,
                    severity="high", signal="concerning")

    class Ctx:
        db_path = tmp_profile_db

    prices = [100] * 25 + [101, 103, 105, 107, 110]  # rallied
    bars = _bars_with_drop(prices)
    with patch("market_data.get_bars", return_value=bars):
        results = find_candidates(Ctx(), ["TSLA"])
    assert results == []


def test_universe_filter_applied(tmp_profile_db):
    """Strategy should respect the passed universe (skip symbols not in it)."""
    from strategies.catalyst_filing_short import find_candidates

    _insert_filing(tmp_profile_db, "TSLA", days_ago=5, going_concern=True)
    _insert_filing(tmp_profile_db, "GME", days_ago=5, going_concern=True)

    class Ctx:
        db_path = tmp_profile_db

    bars = _bars_with_drop([100] * 25 + [85, 84, 83, 82, 81])
    with patch("market_data.get_bars", return_value=bars):
        results = find_candidates(Ctx(), ["TSLA"])  # only TSLA in universe
    syms = {r["symbol"] for r in results}
    assert "TSLA" in syms
    assert "GME" not in syms


def test_no_db_path_returns_empty():
    """Without a db_path, can't read the filings table — return [] gracefully."""
    from strategies.catalyst_filing_short import find_candidates

    class Ctx:
        db_path = None

    results = find_candidates(Ctx(), ["AAPL"])
    assert results == []


def test_high_severity_concerning_triggers(tmp_profile_db):
    """severity=high + signal=concerning → catalyst triggers."""
    from strategies.catalyst_filing_short import find_candidates

    _insert_filing(tmp_profile_db, "META", days_ago=7,
                    severity="high", signal="concerning")

    class Ctx:
        db_path = tmp_profile_db

    prices = [100] * 25 + [92, 90, 88, 86, 85]
    bars = _bars_with_drop(prices)
    with patch("market_data.get_bars", return_value=bars):
        results = find_candidates(Ctx(), ["META"])
    assert len(results) == 1
    assert "high-severity adverse change" in results[0]["reason"]
