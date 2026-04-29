"""Regression tests for LONG_SHORT_PLAN.md Phase 1.

Each test pins a specific behavior introduced in P1.0 through P1.14
so future refactors can't silently break them. Specifically guards
the MFE side mismatch (P1.10) — that bug existed for months because
nothing tested it.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# P1.0 — prediction_type semantics
# ---------------------------------------------------------------------------

def test_prediction_type_column_exists_after_init(tmp_path):
    """The schema migration adds prediction_type to ai_predictions."""
    from journal import init_db
    db = str(tmp_path / "p.db")
    init_db(db)
    conn = sqlite3.connect(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_predictions)")}
    conn.close()
    assert "prediction_type" in cols


def test_backfill_classifies_buy_as_directional_long(tmp_path):
    from ai_tracker import backfill_prediction_type, init_tracker_db
    db = str(tmp_path / "p.db")
    init_tracker_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO ai_predictions "
        "(timestamp, symbol, predicted_signal, price_at_prediction, status, "
        " reasoning) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-04-01", "AAPL", "BUY", 100.0, "resolved", ""),
    )
    conn.commit()
    conn.close()
    backfill_prediction_type(db)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT prediction_type FROM ai_predictions WHERE symbol='AAPL'"
    ).fetchone()
    conn.close()
    assert row[0] == "directional_long"


def test_backfill_classifies_short_as_directional_short(tmp_path):
    from ai_tracker import backfill_prediction_type, init_tracker_db
    db = str(tmp_path / "p.db")
    init_tracker_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO ai_predictions "
        "(timestamp, symbol, predicted_signal, price_at_prediction, status, "
        " reasoning) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-04-01", "TSLA", "SHORT", 200.0, "resolved", "Breaking down"),
    )
    conn.commit()
    conn.close()
    backfill_prediction_type(db)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT prediction_type FROM ai_predictions WHERE symbol='TSLA'"
    ).fetchone()
    conn.close()
    assert row[0] == "directional_short"


def test_backfill_classifies_sell_with_exit_reasoning_as_exit_long(tmp_path):
    from ai_tracker import backfill_prediction_type, init_tracker_db
    db = str(tmp_path / "p.db")
    init_tracker_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO ai_predictions "
        "(timestamp, symbol, predicted_signal, price_at_prediction, status, "
        " reasoning) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-04-01", "IONQ", "SELL", 30.0, "resolved",
         "Exit existing 19-share position. Extreme overbought."),
    )
    conn.commit()
    conn.close()
    backfill_prediction_type(db)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT prediction_type FROM ai_predictions WHERE symbol='IONQ'"
    ).fetchone()
    conn.close()
    assert row[0] == "exit_long"


def test_resolver_exit_long_loses_when_price_runs_up():
    from ai_tracker import _resolve_one, EXIT_BUFFER_PCT
    from datetime import datetime, timedelta
    pred = {
        "predicted_signal": "SELL",
        "prediction_type": "exit_long",
        "price_at_prediction": 100.0,
        "timestamp": (datetime.utcnow() - timedelta(days=10)).isoformat(),
    }
    # Price went up 5% > EXIT_BUFFER_PCT → exit was wrong (left gains on table)
    result = _resolve_one(pred, current_price=100.0 * (1 + (EXIT_BUFFER_PCT + 3) / 100))
    assert result is not None
    outcome, _, _ = result
    assert outcome == "loss"


def test_resolver_exit_long_wins_when_price_stays_flat():
    from ai_tracker import _resolve_one
    from datetime import datetime, timedelta
    pred = {
        "predicted_signal": "SELL",
        "prediction_type": "exit_long",
        "price_at_prediction": 100.0,
        "timestamp": (datetime.utcnow() - timedelta(days=10)).isoformat(),
    }
    result = _resolve_one(pred, current_price=99.5)
    assert result is not None
    outcome, _, _ = result
    assert outcome == "win"


# ---------------------------------------------------------------------------
# P1.1 — Bearish strategies registered
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_name", [
    "strategies.breakdown_support",
    "strategies.distribution_at_highs",
    "strategies.failed_breakout",
    "strategies.parabolic_exhaustion",
    "strategies.relative_weakness_in_strong_sector",
])
def test_bearish_strategy_module_has_required_interface(module_name):
    import importlib
    mod = importlib.import_module(module_name)
    assert hasattr(mod, "NAME")
    assert hasattr(mod, "APPLICABLE_MARKETS")
    assert callable(getattr(mod, "find_candidates", None))


def test_bearish_strategies_in_strategy_registry():
    from strategies import STRATEGY_MODULES
    for required in (
        "strategies.breakdown_support",
        "strategies.distribution_at_highs",
        "strategies.failed_breakout",
        "strategies.parabolic_exhaustion",
        "strategies.relative_weakness_in_strong_sector",
    ):
        assert required in STRATEGY_MODULES, (
            f"{required} missing from STRATEGY_MODULES — discover_strategies "
            f"won't pick it up.")


# ---------------------------------------------------------------------------
# P1.7 — Two shortlists with reserved slots
# ---------------------------------------------------------------------------

def _signal(symbol, action, score=2):
    return {
        "symbol": symbol,
        "signal": action,
        "score": score,
        "votes": {"strat": action},
    }


def test_rank_candidates_long_only_when_shorts_disabled():
    from trade_pipeline import _rank_candidates
    inputs = [_signal(f"L{i}", "BUY", score=2) for i in range(20)] + \
             [_signal(f"S{i}", "SHORT", score=3) for i in range(5)]
    out = _rank_candidates(inputs, held_symbols=set(), enable_shorts=False)
    # Shorts should be filtered out entirely (non-held + shorts disabled)
    actions = {c["signal"] for c in out}
    assert "SHORT" not in actions
    assert all(a == "BUY" for a in actions)


def test_rank_candidates_reserves_short_slots_when_shorts_enabled(monkeypatch):
    """Shorts-enabled view should give bearish candidates dedicated slots
    even when bullish candidates have higher abs(score)."""
    # Stub out the regime gate / borrow / squeeze checks so we test the
    # slot-reservation logic only.
    monkeypatch.setattr("trade_pipeline._classify_market_regime", lambda: "neutral")
    monkeypatch.setattr("trade_pipeline._squeeze_risk", lambda s: "LOW")
    monkeypatch.setattr(
        "client.get_borrow_info",
        lambda symbol, api=None, ctx=None: {"shortable": True, "easy_to_borrow": True},
    )
    from trade_pipeline import _rank_candidates
    # 20 high-scoring long candidates + 5 short candidates with lower scores
    inputs = [_signal(f"L{i}", "BUY", score=4) for i in range(20)] + \
             [_signal(f"S{i}", "SHORT", score=1) for i in range(5)]
    out = _rank_candidates(inputs, held_symbols=set(), enable_shorts=True)
    short_count = sum(1 for c in out if c["signal"] == "SHORT")
    assert short_count >= 1, "shorts crowded out by higher-scoring longs"
    assert short_count <= 5, "exceeded reserved short slots"


# ---------------------------------------------------------------------------
# P1.6 — Asymmetric position sizing
# ---------------------------------------------------------------------------

def test_short_position_cap_defaults_to_half_long():
    from ai_analyst import _validate_ai_trades
    class Ctx:
        max_position_pct = 0.10
        short_max_position_pct = None  # None → derive
        enable_short_selling = True
    candidates = [{"symbol": "TSLA", "signal": "SHORT"}]
    ai_response = {"trades": [{
        "symbol": "TSLA", "action": "SHORT",
        "size_pct": 50.0,  # AI tries oversize
        "confidence": 80,
    }]}
    out = _validate_ai_trades(ai_response, candidates, ctx=Ctx())
    assert len(out["trades"]) == 1
    # Expected cap = 0.10 / 2 = 0.05 → 5%
    assert out["trades"][0]["size_pct"] <= 5.0


def test_long_position_cap_unaffected_by_short_settings():
    from ai_analyst import _validate_ai_trades
    class Ctx:
        max_position_pct = 0.10
        short_max_position_pct = 0.02
        enable_short_selling = True
    candidates = [{"symbol": "AAPL", "signal": "BUY"}]
    ai_response = {"trades": [{
        "symbol": "AAPL", "action": "BUY",
        "size_pct": 50.0,
        "confidence": 80,
    }]}
    out = _validate_ai_trades(ai_response, candidates, ctx=Ctx())
    # Long cap is still 10% — short cap doesn't constrain it
    assert out["trades"][0]["size_pct"] <= 10.0
    assert out["trades"][0]["size_pct"] > 5.0


# ---------------------------------------------------------------------------
# P1.14 — HTB shorts get sizing penalty
# ---------------------------------------------------------------------------

def test_htb_short_gets_size_halved_again():
    from ai_analyst import _validate_ai_trades
    class Ctx:
        max_position_pct = 0.10
        short_max_position_pct = 0.05
        enable_short_selling = True
    candidates = [{"symbol": "GME", "signal": "SHORT", "_borrow_cost": "high"}]
    ai_response = {"trades": [{
        "symbol": "GME", "action": "SHORT",
        "size_pct": 50.0,
        "confidence": 80,
    }]}
    out = _validate_ai_trades(ai_response, candidates, ctx=Ctx())
    # short cap 5% halved by HTB penalty → 2.5%
    assert out["trades"][0]["size_pct"] <= 2.5


# ---------------------------------------------------------------------------
# P1.10 — MFE side-mismatch guard
# ---------------------------------------------------------------------------

def test_mfe_updater_uses_short_side_not_sell_short():
    """The fix that should have happened months ago. log_trade writes
    side='short' for new short positions; the MFE updater must match.
    """
    import re
    with open(os.path.join(os.path.dirname(__file__), "..", "trader.py")) as fh:
        source = fh.read()
    # The MFE updater should query side='short' (not 'sell_short') for
    # the short branch.
    short_branch_match = re.search(
        r"if float\(p\.get\(\"qty\", 0\)\) < 0:.*?WHERE symbol = \? AND side = '([^']+)'",
        source,
        re.DOTALL,
    )
    assert short_branch_match, "MFE updater short-branch query not found"
    assert short_branch_match.group(1) == "short", (
        f"MFE updater queries side='{short_branch_match.group(1)}' but log_trade "
        f"writes side='short'. This silently broke MFE for shorts before P1.10. "
        f"See LONG_SHORT_PLAN.md."
    )


# ---------------------------------------------------------------------------
# P1.11 — Direction-aware calibrator paths
# ---------------------------------------------------------------------------

def test_calibrator_path_includes_direction(tmp_path):
    from specialist_calibration import _calibrator_path
    db = str(tmp_path / "p.db")
    long_path = _calibrator_path(db, "trend_specialist", direction="long")
    short_path = _calibrator_path(db, "trend_specialist", direction="short")
    legacy_path = _calibrator_path(db, "trend_specialist")
    assert long_path != short_path != legacy_path
    assert "_long.pkl" in long_path
    assert "_short.pkl" in short_path
    assert "_long" not in legacy_path and "_short" not in legacy_path


# ---------------------------------------------------------------------------
# P1.12 — Meta-model with prediction_type feature
# ---------------------------------------------------------------------------

def test_prediction_type_in_meta_model_categorical_features():
    from meta_model import CATEGORICAL_FEATURES
    assert "prediction_type" in CATEGORICAL_FEATURES, (
        "meta_model.CATEGORICAL_FEATURES is missing prediction_type — "
        "models trained without it can't differentiate long/short edges."
    )
    expected_values = {"directional_long", "directional_short",
                       "exit_long", "exit_short"}
    assert set(CATEGORICAL_FEATURES["prediction_type"]) == expected_values


def test_short_in_meta_model_signal_categories():
    from meta_model import CATEGORICAL_FEATURES
    sig_values = set(CATEGORICAL_FEATURES["signal"])
    assert "SHORT" in sig_values
    assert "STRONG_SHORT" in sig_values


# ---------------------------------------------------------------------------
# P1.13 — Strategy generator direction_mix
# ---------------------------------------------------------------------------

def test_strategy_proposer_embeds_direction_mix_in_prompt():
    from strategy_proposer import _build_prompt
    prompt = _build_prompt(
        ctx_summary="test",
        recent_performance=[],
        n_proposals=2,
        market_types=["small"],
        direction_mix={"BUY": 1, "SELL": 1},
    )
    # Direction mix should be embedded as a constraint
    assert "1 BUY" in prompt or "1 SELL" in prompt
    assert "Direction mix required" in prompt


def test_strategy_proposer_omits_mix_when_not_requested():
    from strategy_proposer import _build_prompt
    prompt = _build_prompt(
        ctx_summary="test",
        recent_performance=[],
        n_proposals=2,
        market_types=["small"],
    )
    assert "Direction mix required" not in prompt
