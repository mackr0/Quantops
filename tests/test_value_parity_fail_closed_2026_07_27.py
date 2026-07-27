"""Value-parity audit fails CLOSED on broker read failure
(2026-07-27, the broker=$0.00 / −$481,798 fake-phantom incident).

`_broker_positions_value` returned 0.0 when `list_positions` failed,
and the caller compared that fabricated zero against $481K of journal
value — a transient API failure rendered a screaming half-million-
dollar journal_value_phantom ERROR on the issues page while the books
were penny-clean. Same disease as the orphan-closer incident the same
day: broker-state inference treated as truth when the read didn't
actually succeed.

Pinned: failed read → account reported UNVERIFIABLE (no drift
finding, no fabricated $0); working reads still audit normally; the
issues page renders unverifiable at WARNING, never as a drift ERROR.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import aggregate_audit as agg  # noqa: E402


class TestBrokerReadFailure:
    def test_failed_read_returns_none_not_zero(self):
        api = MagicMock()
        api.list_positions.side_effect = RuntimeError("rate limited")
        assert agg._broker_positions_value(api) is None, (
            "a failed broker read reported as $0.00 again — the "
            "-$481,798 fake-phantom class."
        )

    def test_working_read_sums_stock_values(self):
        api = MagicMock()
        p = MagicMock()
        p.symbol = "AAPL"
        p.market_value = "1000.50"
        api.list_positions.return_value = [p]
        assert agg._broker_positions_value(api) == 1000.50


class TestAuditMarksUnverifiable:
    def _run(self, broker_value):
        ctx = MagicMock()
        ctx.alpaca_account_id = 56
        ctx.db_path = "x.db"
        with patch.object(agg, "_broker_positions_value",
                          return_value=broker_value), \
             patch.object(agg, "_journal_positions_value",
                          return_value=481798.07), \
             patch("models.build_user_context_from_profile",
                   return_value=ctx), \
             patch("client._make_price_fetcher", return_value=None):
            return agg.audit_account_value_parity([211])

    def test_unverifiable_account_produces_no_drift_finding(self):
        out = self._run(broker_value=None)
        assert out["drift"] == [], (
            "an unverifiable broker read produced a drift finding — "
            "the issues page screams a fake phantom again."
        )
        assert len(out["unverifiable"]) == 1
        row = out["unverifiable"][0]
        assert row["kind"] == "unverifiable"
        assert row["broker_value"] is None

    def test_real_zero_broker_still_audits(self):
        """An account that GENUINELY holds nothing (read succeeded,
        empty list) must still drift against a nonzero journal —
        fail-closed must not hide real phantoms."""
        out = self._run(broker_value=0.0)
        assert len(out["drift"]) == 1
        assert out["drift"][0]["kind"] == "journal_value_phantom"

    def test_summary_renders_unverifiable_honestly(self):
        out = self._run(broker_value=None)
        text = agg.format_value_drift_summary(out)
        assert "UNVERIFIABLE" in text
        assert "$0.00" not in text
