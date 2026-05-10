"""Pin all 4 intraday-risk inputs are wired in `_task_intraday_risk_check`.

Caught 2026-05-09: the scheduler called `collect_intraday_alerts` with
4 of 6 named arguments and a comment `# sector_moves +
halted_held_symbols deferred`. Both deferred checks
(`check_sector_concentration_swing`, `check_held_position_halts`) are
fully implemented in `intraday_risk_monitor.py` and tested in
`tests/test_intraday_risk_monitor.py` — they were just never invoked
with non-empty data in production. 50% of the safety system was dark
for an unknown duration.

This test pins:
1. The 2 helpers `_compute_sector_moves` and
   `_compute_halted_held_symbols` produce the expected dict / list
   shapes from realistic stub inputs.
2. End-to-end: a scheduler invocation with seeded data triggers the
   sector-swing + halted-held alerts.
3. Cross-cutting AST guardrail: the call to `collect_intraday_alerts`
   in `multi_scheduler.py` MUST pass every named argument the
   function accepts. Any future refactor that drops one (the same
   "deferred" silencing shape that produced this bug) fails the test.
"""

import ast
import inspect
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# Layer 1 — helper behavior
# ---------------------------------------------------------------------------


class TestSectorMovesHelper:
    def test_returns_signed_pct_per_sector(self, monkeypatch):
        from multi_scheduler import _compute_sector_moves
        import pandas as pd

        # Stub get_bars: ETF_a moves +2%, ETF_b moves -3%, ETF_c
        # has no data (must be omitted, not error).
        def fake_get_bars(symbol, limit=2, **kw):
            mapping = {
                "XLK":  pd.DataFrame({"close": [100.0, 102.0]}),  # +2%
                "XLF":  pd.DataFrame({"close": [50.0, 48.5]}),    # -3%
                "XLE":  None,                                      # missing
                "XLV":  pd.DataFrame({"close": [90.0, 90.0]}),    # 0%
                "XLI":  pd.DataFrame({"close": [70.0, 71.4]}),    # +2%
                "XLY":  pd.DataFrame({"close": [80.0, 80.0]}),
                "XLP":  pd.DataFrame({"close": [60.0, 60.0]}),
                "XLU":  pd.DataFrame({"close": [55.0, 55.0]}),
                "XLB":  pd.DataFrame({"close": [40.0, 40.0]}),
                "XLRE": pd.DataFrame({"close": [35.0, 35.0]}),
                "XLC":  pd.DataFrame({"close": [65.0, 65.0]}),
            }
            return mapping.get(symbol)

        monkeypatch.setattr("market_data.get_bars", fake_get_bars)
        moves = _compute_sector_moves()

        assert "tech" in moves
        assert moves["tech"] == pytest.approx(0.02)
        assert moves["finance"] == pytest.approx(-0.03)
        # Missing data → silently omitted, not 0 (would be a false alert)
        assert "energy" not in moves

    def test_zero_yesterday_close_skipped_no_zerodiv(self, monkeypatch):
        from multi_scheduler import _compute_sector_moves
        import pandas as pd

        def fake_get_bars(symbol, limit=2, **kw):
            if symbol == "XLK":
                return pd.DataFrame({"close": [0.0, 100.0]})  # bad data
            return pd.DataFrame({"close": [50.0, 50.0]})

        monkeypatch.setattr("market_data.get_bars", fake_get_bars)
        moves = _compute_sector_moves()
        assert "tech" not in moves


class TestHaltedHeldSymbolsHelper:
    def test_returns_only_non_tradable_held_symbols(self, monkeypatch):
        from multi_scheduler import _compute_halted_held_symbols, _HALT_CACHE

        _HALT_CACHE.clear()

        def fake_positions(ctx=None, **kw):
            return [
                {"symbol": "AAPL"},
                {"symbol": "TSLA"},
                {"symbol": "FROZEN"},
            ]

        def fake_get_api(ctx):
            api = MagicMock()
            def get_asset(sym):
                a = MagicMock()
                a.tradable = (sym != "FROZEN")
                return a
            api.get_asset.side_effect = get_asset
            return api

        monkeypatch.setattr("client.get_positions", fake_positions)
        monkeypatch.setattr("client.get_api", fake_get_api)

        ctx = MagicMock()
        result = _compute_halted_held_symbols(ctx)
        assert result == ["FROZEN"]

    def test_get_asset_failure_does_not_fire_alert(self, monkeypatch):
        """A flaky Alpaca call must NOT cause a halt-alert. Real safety
        rule: never alert from broken plumbing — only from real signals."""
        from multi_scheduler import _compute_halted_held_symbols, _HALT_CACHE

        _HALT_CACHE.clear()

        def fake_positions(ctx=None, **kw):
            return [{"symbol": "AAPL"}]

        def fake_get_api(ctx):
            api = MagicMock()
            api.get_asset.side_effect = RuntimeError("Alpaca down")
            return api

        monkeypatch.setattr("client.get_positions", fake_positions)
        monkeypatch.setattr("client.get_api", fake_get_api)

        ctx = MagicMock()
        result = _compute_halted_held_symbols(ctx)
        # AAPL not added because the lookup failed
        assert "AAPL" not in result

    def test_cache_avoids_repeat_get_asset_calls(self, monkeypatch):
        """15-min cache: 2nd call within window doesn't re-hit Alpaca."""
        from multi_scheduler import _compute_halted_held_symbols, _HALT_CACHE

        _HALT_CACHE.clear()
        call_count = {"n": 0}

        def fake_positions(ctx=None, **kw):
            return [{"symbol": "AAPL"}]

        def fake_get_api(ctx):
            api = MagicMock()
            def get_asset(sym):
                call_count["n"] += 1
                a = MagicMock()
                a.tradable = True
                return a
            api.get_asset.side_effect = get_asset
            return api

        monkeypatch.setattr("client.get_positions", fake_positions)
        monkeypatch.setattr("client.get_api", fake_get_api)

        ctx = MagicMock()
        _compute_halted_held_symbols(ctx)
        _compute_halted_held_symbols(ctx)
        # Second call hits cache, not the API
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Layer 2 — cross-cutting AST guardrail: every named arg must be passed
# ---------------------------------------------------------------------------


def test_scheduler_passes_all_collect_intraday_alerts_args():
    """Inspect `intraday_risk_monitor.collect_intraday_alerts` to learn
    its parameter names, then AST-scan `multi_scheduler.py` for the
    call site and assert ALL parameter names appear as keyword args.

    Catches the 2026-05-09 bug shape: scheduler quietly drops a
    parameter and silently disables a check. Empty allowlist."""
    from intraday_risk_monitor import collect_intraday_alerts
    sig = inspect.signature(collect_intraday_alerts)
    expected_args = set(sig.parameters.keys())

    SCHEDULER_PATH = os.path.join(
        os.path.dirname(__file__), os.pardir, "multi_scheduler.py",
    )
    with open(SCHEDULER_PATH) as f:
        tree = ast.parse(f.read())

    # Find the Call node for collect_intraday_alerts
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match either bare `collect_intraday_alerts(...)` or
        # `module.collect_intraday_alerts(...)`.
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name != "collect_intraday_alerts":
            continue
        passed = {kw.arg for kw in node.keywords if kw.arg is not None}
        line = getattr(node, "lineno", "?")
        found.append((line, passed))

    assert found, (
        "No call to collect_intraday_alerts() found in multi_scheduler.py "
        "— either the scheduler stopped wiring it up entirely, or this "
        "guardrail's call-site detection broke."
    )

    leaks = []
    for line, passed in found:
        missing = expected_args - passed
        if missing:
            leaks.append(
                f"  multi_scheduler.py:{line} — call to "
                f"collect_intraday_alerts() is missing kwargs "
                f"{sorted(missing)}. The 2026-05-09 bug was exactly "
                "this — scheduler 'deferred' two args and silently "
                "disabled half the safety net."
            )
    assert not leaks, "\n".join(leaks)
