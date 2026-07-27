"""Portfolio risk snapshots finally produce (P4.1, 2026-07-27).

portfolio_risk_snapshots had NEVER written a row — the 713-line
factor model ran on every profile and returned None every time. Two
stacked causes:

1. The snapshot task fed the effective-positions converter dicts of
   {symbol, market_value} only, and the converter SKIPS any position
   with qty == 0 — every position was dropped, exposures came back
   empty, and the task logged "insufficient factor data" forever.
2. Ken French factors publish with a lag that reaches months, and
   the inner join truncated the ENTIRE factor matrix to FF's last
   date (observed: 56 days stale), leaving stale covariance.

Pinned here: stock positions without qty contribute their signed
market_value (options still require qty for delta math); the task
payload carries qty + current_price; the FF-staleness guard prefers
the fresh ETF-only factor set over a stale complete one.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

from pipelines.risk import exposure as exp  # noqa: E402


class TestConverterMinimalDicts:
    def test_stock_market_value_only_contributes(self):
        """The exact payload shape the snapshot task used to send —
        every position was skipped and the model starved."""
        out = exp.effective_positions_for_risk_model(
            [{"symbol": "AAPL", "market_value": 30000.0},
             {"symbol": "XOM", "market_value": -15000.0}],
            use_live_iv=False,
        )
        by_sym = {p["symbol"]: p["market_value"] for p in out}
        assert by_sym == {"AAPL": 30000.0, "XOM": -15000.0}, (
            "qty-less stock positions were dropped again — "
            "portfolio_risk_snapshots goes back to never writing."
        )

    def test_option_without_qty_still_skipped(self):
        """Options price from qty x delta x spot — a qty-less option
        row has no honest exposure statement."""
        out = exp.effective_positions_for_risk_model(
            [{"symbol": "AAPL  260821C00230000", "market_value": 500.0}],
            use_live_iv=False,
        )
        assert out == []

    def test_qty_path_unchanged_for_stocks(self):
        out = exp.signed_portfolio_delta_exposure(
            [{"symbol": "JPM", "qty": 100, "current_price": 200.0,
              "market_value": 20000.0}],
        )
        assert out.get("JPM") == 20000.0


class TestTaskPayload:
    def test_snapshot_task_sends_qty_and_price(self):
        src = open(os.path.join(REPO, "multi_scheduler.py")).read()
        i = src.find("def _task_portfolio_risk_snapshot")
        block = src[i:i + 3000]
        assert '"qty"' in block and '"current_price"' in block, (
            "the risk-snapshot task stopped passing qty/current_price "
            "— the converter needs them (and their absence is exactly "
            "how the table stayed empty for months)."
        )


class TestFrenchStalenessGuard:
    def _frames(self, ff_end):
        import pandas as pd
        etf_idx = pd.date_range("2026-05-01", "2026-07-24", freq="B")
        etf = pd.DataFrame({"sector_tech": 0.001, "quality": 0.0005},
                            index=etf_idx)
        ff_idx = pd.date_range("2026-05-01", ff_end, freq="B")
        ff = pd.DataFrame({"Mkt-RF": 0.001, "SMB": 0.0}, index=ff_idx)
        return etf, ff

    def _run(self, ff_end):
        import pandas as pd
        import portfolio_risk_model as prm
        etf, ff = self._frames(ff_end)

        def fake_etf_returns(etf_map, lookback):
            # first call returns the full frame, second returns empty
            # (sector vs style split doesn't matter for the guard)
            if fake_etf_returns.called:
                return pd.DataFrame()
            fake_etf_returns.called = True
            return etf
        fake_etf_returns.called = False
        with patch.object(prm, "_etf_returns", side_effect=fake_etf_returns), \
             patch.object(prm, "fetch_french_factors", return_value=ff):
            return prm.compute_factor_returns(252)

    def test_stale_french_dropped_for_fresh_etf_set(self):
        out = self._run(ff_end="2026-05-29")  # 56 days behind
        assert "Mkt-RF" not in out.columns, (
            "stale Ken French columns re-joined — the whole factor "
            "matrix truncates to their last publication again."
        )
        assert str(out.index.max().date()) == "2026-07-24"

    def test_fresh_french_still_joins(self):
        out = self._run(ff_end="2026-07-23")  # 1 day behind
        assert "Mkt-RF" in out.columns
        assert "sector_tech" in out.columns
