"""Broker-funding guard (2026-06-12).

The dead-day class: the 6-12 accounts were verified at $1M at
03:15 UTC and were $0 at the broker by the open. Every order was
rejected 'insufficient buying power' for six hours; warnings went
to logs and ERROR badges only — no halt, no banner. The operator
paid for a full trading day of silence.

Pins:
  1. funding_status flags equity below 50% of combined profile
     capital; passes when funded; defers on broker-unreachable.
  2. enforce_funding halts (dashboard banner path) on missing
     funding and self-clears its own halt when funding returns.
  3. The scan task calls the guard BEFORE the simple-strategy
     dispatch (baselines must be protected too).
  4. The pre-market smoke test registers broker_accounts_funded.
  5. certify_books runs the funding check FIRST — books that
     reconcile over an unfunded account certify nothing.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent


def _ctx(account_id=7, equity=1_000_000.0, api_fails=False):
    ctx = MagicMock()
    ctx.alpaca_account_id = account_id
    ctx.profile_id = 42
    ctx.db_path = None
    api = MagicMock()
    if api_fails:
        api.get_account.side_effect = Exception("api down")
    else:
        api.get_account.return_value = MagicMock(equity=str(equity))
    ctx.get_alpaca_api.return_value = api
    return ctx


def _clear_cache():
    import account_funding_guard as g
    with g._cache_lock:
        g._equity_cache.clear()


class TestFundingStatus:

    def test_vanished_funding_flagged(self):
        from account_funding_guard import funding_status
        _clear_cache()
        with patch("account_funding_guard._expected_capital",
                   return_value=1_000_000.0):
            funded, detail = funding_status(_ctx(equity=0.0))
        assert funded is False
        assert "funding is missing" in detail

    def test_normal_pnl_swings_pass(self):
        from account_funding_guard import funding_status
        _clear_cache()
        with patch("account_funding_guard._expected_capital",
                   return_value=1_000_000.0):
            funded, _ = funding_status(_ctx(equity=820_000.0))
        assert funded is True

    def test_broker_unreachable_defers(self):
        from account_funding_guard import funding_status
        _clear_cache()
        with patch("account_funding_guard._expected_capital",
                   return_value=1_000_000.0):
            funded, detail = funding_status(_ctx(api_fails=True))
        assert funded is True
        assert "broker_health" in detail


class TestEnforce:

    def test_missing_funding_halts_and_blocks(self):
        import account_funding_guard as g
        _clear_cache()
        calls = {}
        with patch("account_funding_guard._expected_capital",
                   return_value=1_000_000.0), \
             patch("halt_helpers.halt_and_alert",
                   side_effect=lambda **kw: calls.update(kw)), \
             patch("halt_helpers.is_halted", return_value=(False, None)):
            ok = g.enforce_funding(_ctx(equity=0.0))
        assert ok is False
        assert calls.get("alert_type") == "broker_funding_missing"
        assert g.HALT_REASON_PREFIX in calls.get("title", "")

    def test_funding_restored_clears_own_halt(self):
        import account_funding_guard as g
        _clear_cache()
        cleared = {}
        with patch("account_funding_guard._expected_capital",
                   return_value=1_000_000.0), \
             patch("halt_helpers.is_halted",
                   return_value=(True, g.HALT_REASON_PREFIX + " x")), \
             patch("halt_helpers.clear_halt",
                   side_effect=lambda pid, source=None:
                   cleared.update({"pid": pid, "source": source})):
            ok = g.enforce_funding(_ctx(equity=1_000_000.0))
        assert ok is True
        assert cleared.get("source") == "funding_restored"

    def test_does_not_clear_other_halts(self):
        import account_funding_guard as g
        _clear_cache()
        with patch("account_funding_guard._expected_capital",
                   return_value=1_000_000.0), \
             patch("halt_helpers.is_halted",
                   return_value=(True, "Reconciler safety net: x")), \
             patch("halt_helpers.clear_halt") as ch:
            ok = g.enforce_funding(_ctx(equity=1_000_000.0))
        assert ok is True
        ch.assert_not_called()


# ---------------------------------------------------------------------------
# Wiring pins
# ---------------------------------------------------------------------------

def test_scan_task_guards_before_simple_dispatch():
    src = (REPO / "multi_scheduler.py").read_text()
    start = src.index("def _task_scan_and_trade")
    guard_idx = src.index("enforce_funding(ctx)", start)
    simple_idx = src.index("_simple_dispatch(ctx)", start)
    assert guard_idx < simple_idx, (
        "Funding guard must run BEFORE the baseline dispatch — "
        "SPY/Random profiles burned the dead day too."
    )


def test_premarket_smoke_registers_funding_check():
    src = (REPO / "premarket_smoke_test.py").read_text()
    assert '("broker_accounts_funded",' in src, (
        "Pre-market smoke no longer checks account funding — the "
        "2026-06-12 dead day would pass the morning gate again."
    )


def test_certify_books_runs_funding_first():
    src = (REPO / "certify_books.py").read_text()
    fund_idx = src.index('("0. BROKER FUNDING", check_funding())')
    drift_idx = src.index('("1. BROKER DRIFT"')
    assert fund_idx < drift_idx, (
        "certify_books must check funding before anything else — "
        "books over an unfunded account certify nothing."
    )
