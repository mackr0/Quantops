"""2026-06-06 — two root-cause fixes paired in one suite because they
share the operator's "find the actual problem, don't paper over it"
instruction:

  1. Position-cap race in `trade_pipeline.run_trade_cycle`. The
     positions snapshot was taken at cycle start. `check_portfolio_
     constraints` was called against THAT snapshot for every trade
     in the same cycle. When the AI proposed multi-BUY cycles, each
     BUY saw the same 0-open snapshot and they all passed, even on
     profiles with `max_total_positions=5` — that's how pid 48 ended
     up with 6 open positions on a 5-cap profile.

     Fix: `_append_new_stock_position` mirrors the existing
     `_decrement_closed_stock_position` — it mutates the in-cycle
     positions_list after every successful BUY so the very next
     trade's cap check sees the higher count. Symmetric to the
     close-side decrement that was already there.

  2. `is_alpaca_active` permissive fallback in altdata cron processes.
     The active-symbol set was cached in a module-level Python dict.
     Fine in the scheduler process. Empty in every altdata cron
     process (separate Python interpreter, fresh import). Empty
     cache → permissive `return True` → yfinance ran on every
     delisted ticker → 40+ "possibly delisted" ERROR lines on
     /issues, exactly the same tickers the 2026-05-16 guard was
     supposed to suppress (CT, HN, NJ, OL, REV, SPYB, SQ, VA).

     Fix: mirror the freshly-fetched set to a master-DB table
     (`alpaca_active_symbols_cache`, single row) on every scheduler
     refresh. When `is_alpaca_active` finds an empty in-process
     cache, read from the DB table BEFORE falling through to
     permissive. Cron processes get the same view as the scheduler.

Both contracts are pinned behaviorally AND structurally.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Position-cap race
# ---------------------------------------------------------------------------

class TestPositionCapRaceFix:

    def test_append_new_position_helper_exists(self):
        """`_append_new_stock_position` must exist in trade_pipeline.
        Without it, the increment side of the race is unfixed."""
        from trade_pipeline import _append_new_stock_position
        assert callable(_append_new_stock_position)

    def test_buy_action_appends_to_positions_list(self):
        from trade_pipeline import _append_new_stock_position
        positions_list = []
        added = _append_new_stock_position(
            positions_list,
            {"action": "BUY", "symbol": "NVDA", "qty": 100},
        )
        assert added == 1
        assert len(positions_list) == 1
        assert positions_list[0]["symbol"] == "NVDA"
        assert positions_list[0]["_synthetic_in_cycle"] is True

    def test_short_action_appends(self):
        from trade_pipeline import _append_new_stock_position
        positions_list = []
        _append_new_stock_position(
            positions_list,
            {"action": "SHORT", "symbol": "TSLA", "qty": 50},
        )
        assert len(positions_list) == 1
        assert positions_list[0]["side"] == "short"

    def test_sell_action_does_not_append(self):
        """Closes are handled by the existing decrement helper, not
        this one. Avoid double-counting."""
        from trade_pipeline import _append_new_stock_position
        positions_list = [{"symbol": "NVDA"}]
        added = _append_new_stock_position(
            positions_list,
            {"action": "SELL", "symbol": "NVDA"},
        )
        assert added == 0
        assert len(positions_list) == 1  # unchanged

    def test_skip_blocked_actions_dont_append(self):
        from trade_pipeline import _append_new_stock_position
        for action in ("SKIP", "BLOCKED", "ERROR", "HOLD", "NONE"):
            positions_list = []
            _append_new_stock_position(
                positions_list,
                {"action": action, "symbol": "X"},
            )
            assert positions_list == [], (
                f"action={action} must not append a position"
            )

    def test_duplicate_symbol_not_double_appended(self):
        from trade_pipeline import _append_new_stock_position
        positions_list = [{"symbol": "NVDA"}]
        added = _append_new_stock_position(
            positions_list,
            {"action": "BUY", "symbol": "NVDA"},
        )
        assert added == 0
        assert len(positions_list) == 1

    def test_run_trade_cycle_calls_append_after_each_trade(self):
        """Structural: the call site must invoke the helper. Without
        wiring, the helper is dead code and the race persists."""
        src = (REPO_ROOT / "trade_pipeline.py").read_text()
        # The decrement helper has been there since 2026-05-21; the
        # append helper has to live RIGHT NEXT to it in the dispatch
        # loop so every trade_result is processed by both. Use rfind
        # so we anchor on the CALL SITE (later in the file) and not
        # the helper's def signature (which also contains the same
        # literal arg list).
        decrement_idx = src.rfind(
            "_decrement_closed_stock_position(positions_list, trade_result)"
        )
        append_idx = src.rfind(
            "_append_new_stock_position(positions_list, trade_result)"
        )
        assert decrement_idx > 0, "existing decrement call disappeared"
        assert append_idx > 0, (
            "trade_pipeline must call _append_new_stock_position "
            "alongside _decrement_closed_stock_position so the "
            "position-cap snapshot stays accurate within a cycle"
        )
        # And they must live within ~5 lines of each other in the
        # dispatch loop — separating them risks one being skipped
        # via an early-return / continue in between.
        between = src[
            min(decrement_idx, append_idx):max(decrement_idx, append_idx)
        ]
        assert between.count("\n") < 10, (
            "decrement and append calls drifted apart in the dispatch "
            "loop. Keep them adjacent so any future early-return "
            "between them is impossible to introduce silently."
        )


# ---------------------------------------------------------------------------
# Persistent cache for is_alpaca_active across processes
# ---------------------------------------------------------------------------

class TestAlpacaActiveCrossProcessCache:

    def _isolate_cache(self, tmp_path, monkeypatch):
        """Redirect the persistent cache path to a temp file and reset
        the in-process cache so each test gets a clean slate."""
        import screener
        cache_db = tmp_path / "test_master.db"
        monkeypatch.setattr(screener, "_PERSISTED_CACHE_PATH",
                            str(cache_db))
        screener._active_symbols_cache["symbols"] = set()
        screener._active_symbols_cache["timestamp"] = 0.0
        screener._active_symbols_cache["last_failure_ts"] = 0.0
        return cache_db

    def test_write_then_read_round_trip(self, tmp_path, monkeypatch):
        from screener import (
            _write_persisted_active_symbols,
            _read_persisted_active_symbols,
        )
        self._isolate_cache(tmp_path, monkeypatch)
        symbols = {"AAPL", "NVDA", "MSFT"}
        _write_persisted_active_symbols(symbols)
        out = _read_persisted_active_symbols()
        assert out == symbols

    def test_stale_cache_returns_empty(self, tmp_path, monkeypatch):
        from screener import (
            _write_persisted_active_symbols,
            _read_persisted_active_symbols,
        )
        self._isolate_cache(tmp_path, monkeypatch)
        _write_persisted_active_symbols({"AAPL"})
        # Pass tiny max_age to simulate stale
        out = _read_persisted_active_symbols(max_age_seconds=0)
        assert out == set(), (
            "Stale cache (older than max_age) must return empty so "
            "the caller doesn't act on outdated data"
        )

    def test_is_alpaca_active_reads_persistent_cache_when_inproc_empty(
            self, tmp_path, monkeypatch,
    ):
        """The core regression. The scheduler process populates the
        persistent cache. A separate process (simulated here by
        clearing the in-process cache + monkeypatching
        get_active_alpaca_symbols to return empty as it would on a
        cold start) must STILL correctly answer False for delisted
        tickers — by reading from the DB cache."""
        from screener import (
            _write_persisted_active_symbols,
            is_alpaca_active,
        )
        import screener
        self._isolate_cache(tmp_path, monkeypatch)
        # Persist a realistic set without the delisted tickers
        _write_persisted_active_symbols({"AAPL", "NVDA", "MSFT"})
        # Simulate cron-process cold start: no in-process cache AND
        # the broker fetch returns empty (cron has no credentials).
        monkeypatch.setattr(
            screener, "get_active_alpaca_symbols",
            lambda ctx=None, ttl=None: set(),
        )
        # Known-delisted tickers from the 2026-05-16 incident
        for delisted in ["CT", "HN", "NJ", "OL", "REV", "SPYB",
                          "SQ", "VA"]:
            assert is_alpaca_active(delisted) is False, (
                f"is_alpaca_active({delisted!r}) returned True from "
                f"a cold cron process — the persistent cache must "
                f"close this gap"
            )
        # And it correctly accepts known-active tickers
        for active in ["AAPL", "NVDA", "MSFT"]:
            assert is_alpaca_active(active) is True

    def test_true_cold_start_still_permissive(
            self, tmp_path, monkeypatch,
    ):
        """Defensive contract: ONLY when BOTH the in-process cache AND
        the persistent cache are empty does is_alpaca_active fall
        through to permissive True. This preserves the original
        cold-start guarantee (don't block trades on a fresh boot
        when no cache has been populated yet)."""
        import screener
        from screener import is_alpaca_active
        self._isolate_cache(tmp_path, monkeypatch)  # empty DB
        monkeypatch.setattr(
            screener, "get_active_alpaca_symbols",
            lambda ctx=None, ttl=None: set(),
        )
        # Both caches empty + no broker access → permissive
        assert is_alpaca_active("ANY-SYMBOL") is True

    def test_get_active_alpaca_symbols_writes_persistent_cache(self):
        """Structural pin: the fetch path must invoke
        _write_persisted_active_symbols on success. Without it the
        cross-process cache is never populated and the fix is dead
        code."""
        src = (REPO_ROOT / "screener.py").read_text()
        assert "_write_persisted_active_symbols(active)" in src, (
            "screener.py get_active_alpaca_symbols must call "
            "_write_persisted_active_symbols(active) immediately "
            "after a successful Alpaca fetch. Without that call, "
            "altdata cron processes never see the active-symbol "
            "set and is_alpaca_active falls through to permissive."
        )
