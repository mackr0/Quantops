"""Guardrail: `_save_cycle_data` must successfully write the JSON
file even when a candidate's `alt_data` is None (NoAltData ablation
profiles set it that way).

Caught 2026-05-18: EXP-A2-NoAltData and EXP-A2-NoAltData-NoMetaModel
profiles showed "Loading... Waiting for first cycle" on the dashboard
AI Brain widget. They had 15 ai_predictions each in the journal but
no `cycle_data_*.json` file. Root cause: the shortlist dict
comprehension did `c.get("alt_data", {}).get("insider", {})...` —
but `c.get("alt_data", {})` returns `None` (not `{}`) when the key
exists with value None. `None.get(...)` raised AttributeError, the
exception was swallowed at debug level, the file never wrote.

Fix #1: alt_data default is `{}` not `None` when enable_alt_data=False.
Fix #2: shortlist comprehension uses `(c.get("X") or {})` defensively.
Fix #3: failure is logged at WARNING (not debug) so the next time
       a different field trips it, /issues surfaces it.

This test calls _save_cycle_data with the exact malformed input the
NoAltData profiles produced and asserts the file gets written.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock


def test_save_cycle_data_with_alt_data_none(tmp_path, monkeypatch):
    """Direct regression: candidate dict has `alt_data=None`. Pre-fix
    code crashed silently here; post-fix must write a valid JSON."""
    monkeypatch.chdir(tmp_path)
    from trade_pipeline import _save_cycle_data

    ctx = MagicMock()
    ctx.profile_id = 99
    ctx.display_name = "EXP-A2-NoAltData-Test"

    candidates_data = [
        {
            "symbol": "AAPL",
            "signal": "buy",
            "score": 0.7,
            "rsi": 55, "adx": 22, "mfi": 60,
            "volume_ratio": 1.2,
            "pct_from_52w_high": -0.05,
            "squeeze": False,
            "track_record": "n/a",
            "news": [],
            "alt_data": None,    # ← the failure trigger
            "social": None,
            "sec_alert": None,
        },
        {
            "symbol": "MSFT",
            "signal": "buy",
            "alt_data": None,
            "social": None,
            "sec_alert": None,
        },
    ]
    shortlist = []
    ai_trades = []
    portfolio_reasoning = "test reasoning"
    market_ctx = {"sector_rotation": {}, "learned_patterns": []}
    regime_info = {"regime": "bull", "vix": 14}

    _save_cycle_data(
        ctx, candidates_data, shortlist, ai_trades,
        portfolio_reasoning, market_ctx, regime_info,
    )

    out_path = tmp_path / "cycle_data_99.json"
    assert out_path.exists(), (
        "cycle_data_99.json was not written — _save_cycle_data is "
        "silently failing again on alt_data=None"
    )
    with open(out_path) as f:
        data = json.load(f)
    assert data["profile_id"] == 99
    assert len(data["shortlist"]) == 2
    # Defaults applied per the `or {}` chain
    assert data["shortlist"][0]["insider"] == "neutral"
    assert data["shortlist"][0]["short_pct"] == 0
    assert data["shortlist"][0]["reddit_mentions"] == 0


def test_save_cycle_data_with_alt_data_dict(tmp_path, monkeypatch):
    """Sanity: when alt_data IS a dict (normal AI profile path), the
    real values propagate correctly."""
    monkeypatch.chdir(tmp_path)
    from trade_pipeline import _save_cycle_data

    ctx = MagicMock()
    ctx.profile_id = 100
    ctx.display_name = "Test-Standard"

    candidates_data = [{
        "symbol": "AAPL",
        "signal": "buy",
        "alt_data": {
            "insider": {"net_direction": "bullish"},
            "short": {"short_pct_float": 3.5},
            "options": {"signal": "bullish"},
        },
        "social": {"mentions": 25},
        "sec_alert": {"severity": "high"},
    }]
    _save_cycle_data(
        ctx, candidates_data, [], [],
        "reasoning", {"sector_rotation": {}, "learned_patterns": []},
        {"regime": "bull", "vix": 14},
    )
    with open(tmp_path / "cycle_data_100.json") as f:
        data = json.load(f)
    assert data["shortlist"][0]["insider"] == "bullish"
    assert data["shortlist"][0]["short_pct"] == 3.5
    assert data["shortlist"][0]["options_signal"] == "bullish"
    assert data["shortlist"][0]["reddit_mentions"] == 25
    assert data["shortlist"][0]["sec_alert_severity"] == "high"


def test_save_cycle_data_failure_surfaces_at_warning(tmp_path, monkeypatch, caplog):
    """If something exotic does crash _save_cycle_data, it must log
    at WARNING (not DEBUG) so the audit catches it. Test by forcing a
    JSON-serialization failure."""
    import logging
    monkeypatch.chdir(tmp_path)
    from trade_pipeline import _save_cycle_data

    ctx = MagicMock()
    ctx.profile_id = 101
    ctx.display_name = "Test-WarnPath"

    class _NotJSON:
        pass
    # Inject an un-serializable object into market_ctx
    market_ctx = {"sector_rotation": _NotJSON(),  # not JSON-encodable
                  "learned_patterns": []}

    with caplog.at_level(logging.WARNING):
        _save_cycle_data(
            ctx, [], [], [], "reason",
            market_ctx, {"regime": "bull", "vix": 14},
        )
    assert any(
        "Failed to save cycle data" in r.message
        for r in caplog.records
    ), "Cycle-data write failure must log at WARNING level"
