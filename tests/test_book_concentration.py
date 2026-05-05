"""Cross-profile concentration cap tests.

Per-profile max_position_pct limits one profile's equity. But 10
profiles all long AAPL = aggregate exposure can exceed the intended
single-name limit. This module computes book-wide $ exposure per
symbol and rejects new entries that would push past
max_book_exposure_pct_per_symbol (default 25%).
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _seed_profile_db(path, equity, positions):
    """positions = list of (symbol, qty, current_price) tuples."""
    from journal import init_db
    init_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO daily_snapshots (date, equity, cash, "
        "portfolio_value, num_positions) VALUES "
        "(date('now'), ?, 0, 0, 0)",
        (equity,),
    )
    for sym, qty, price in positions:
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, status) "
            "VALUES (?, 'buy', ?, ?, 'open')",
            (sym, qty, price),
        )
    conn.commit()
    conn.close()


def test_no_profiles_returns_zero(tmp_path):
    from book_concentration import get_book_exposure_to_symbol
    exposure, equity = get_book_exposure_to_symbol(
        "AAPL", db_paths=[],
    )
    assert exposure == 0.0
    assert equity == 0.0


def test_single_profile_aggregates(tmp_path):
    from book_concentration import get_book_exposure_to_symbol
    p = str(tmp_path / "p1.db")
    _seed_profile_db(p, equity=100_000, positions=[("AAPL", 10, 200)])
    exposure, equity = get_book_exposure_to_symbol(
        "AAPL", db_paths=[p],
    )
    assert exposure == 2_000.0
    assert equity == 100_000.0


def test_multi_profile_aggregates(tmp_path):
    from book_concentration import get_book_exposure_to_symbol
    p1 = str(tmp_path / "p1.db")
    p2 = str(tmp_path / "p2.db")
    p3 = str(tmp_path / "p3.db")
    _seed_profile_db(p1, 100_000, [("AAPL", 10, 200), ("MSFT", 5, 400)])
    _seed_profile_db(p2, 200_000, [("AAPL", 50, 200)])  # $10K
    _seed_profile_db(p3, 50_000, [("AAPL", 20, 200)])   # $4K
    exposure, equity = get_book_exposure_to_symbol(
        "AAPL", db_paths=[p1, p2, p3],
    )
    # 10*200 + 50*200 + 20*200 = 16,000
    assert exposure == 16_000.0
    assert equity == 350_000.0


def test_symbol_match_is_case_insensitive(tmp_path):
    from book_concentration import get_book_exposure_to_symbol
    p = str(tmp_path / "p1.db")
    _seed_profile_db(p, 100_000, [("aapl", 10, 200)])
    exposure, _ = get_book_exposure_to_symbol("AAPL", db_paths=[p])
    assert exposure == 2_000.0


def test_other_symbols_excluded(tmp_path):
    from book_concentration import get_book_exposure_to_symbol
    p = str(tmp_path / "p1.db")
    _seed_profile_db(p, 100_000, [
        ("AAPL", 10, 200),
        ("MSFT", 5, 400),
        ("TSLA", 2, 300),
    ])
    exposure, _ = get_book_exposure_to_symbol("AAPL", db_paths=[p])
    assert exposure == 2_000.0


def test_would_breach_within_cap(tmp_path):
    from book_concentration import would_breach
    p = str(tmp_path / "p1.db")
    # 100K book, 5K existing AAPL = 5%, adding 10K → 15% (under 25%)
    _seed_profile_db(p, 100_000, [("AAPL", 25, 200)])
    breached, reason, detail = would_breach(
        "AAPL", proposed_trade_value=10_000,
        max_book_pct=0.25, db_paths=[p],
    )
    assert breached is False
    assert detail["prospective_pct"] == 0.15


def test_would_breach_at_cap(tmp_path):
    from book_concentration import would_breach
    p = str(tmp_path / "p1.db")
    # 100K book, 20K existing → 20%, adding 10K → 30% (over 25%)
    _seed_profile_db(p, 100_000, [("AAPL", 100, 200)])
    breached, reason, detail = would_breach(
        "AAPL", proposed_trade_value=10_000,
        max_book_pct=0.25, db_paths=[p],
    )
    assert breached is True
    assert "AAPL" in reason
    assert "30" in reason or detail["prospective_pct"] == 0.30


def test_would_breach_with_zero_equity_returns_no_constraint(tmp_path):
    """Defense against bad data — if no profile has equity reported,
    don't accidentally block all trades."""
    from book_concentration import would_breach
    breached, reason, detail = would_breach(
        "AAPL", proposed_trade_value=10_000,
        max_book_pct=0.25, db_paths=[],
    )
    assert breached is False


def test_breach_detail_carries_diagnostic_numbers(tmp_path):
    from book_concentration import would_breach
    p = str(tmp_path / "p1.db")
    _seed_profile_db(p, 100_000, [("AAPL", 100, 200)])  # $20K
    _, _, detail = would_breach(
        "AAPL", proposed_trade_value=10_000,
        max_book_pct=0.25, db_paths=[p],
    )
    assert detail["existing_book_exposure_dollars"] == 20_000.0
    assert detail["prospective_book_exposure_dollars"] == 30_000.0
    assert detail["total_book_equity"] == 100_000.0
    assert detail["cap_pct"] == 0.25
    assert detail["prospective_pct"] == 0.30
