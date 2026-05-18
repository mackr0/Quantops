"""Guardrail: _submit_and_log must NOT propagate per-symbol broker
errors. One stale ticker or one rejected order can't be allowed to
fail the entire scan loop.

Caught 2026-05-18 17:28 ET when GPS (Gap Inc, renamed in 2025) was
in LARGE_CAP_UNIVERSE and got picked by random for P13. Alpaca's
`submit_order` raised `alpaca_trade_api.rest.APIError: asset "GPS"
not found`, which the bare `except (AttributeError, ValueError,
TypeError, OSError)` in `_submit_and_log` didn't catch, so it
propagated up through `run_random_stock_of_day` and failed the
entire Scan & Trade task — only the picks BEFORE GPS in the
iteration order even got attempted.

After the fix, `_submit_and_log` catches any broker exception, logs
a warning, returns False, and the caller's loop continues.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _fake_ctx(tmp_path):
    """Minimal stub for the context arg — only db_path is touched by
    _submit_and_log when log_trade fires, and we won't reach log_trade
    in the rejection cases."""
    ctx = MagicMock()
    ctx.db_path = str(tmp_path / "j.db")
    return ctx


class _FakeAPIError(Exception):
    """Stand-in for alpaca_trade_api.rest.APIError so the test
    doesn't have to import the real Alpaca SDK."""


class TestSubmitAndLogGracefulRejection:
    def test_asset_not_found_returns_false_no_raise(self, tmp_path):
        """The exact 2026-05-18 P13 scenario: stale ticker → broker
        rejects → caller's loop must continue."""
        from simple_strategies import _submit_and_log
        api = MagicMock()
        api.submit_order.side_effect = _FakeAPIError('asset "GPS" not found')
        result = _submit_and_log(
            api, _fake_ctx(tmp_path),
            symbol="GPS", side="buy", qty=10, price=20.0,
            strategy_name="random_stock_of_day",
            reason="day 1 pick",
        )
        assert result is False, (
            "_submit_and_log must return False on broker rejection, "
            "not propagate. 2026-05-18 regression: GPS APIError tore "
            "down the entire P13 random scan."
        )

    def test_insufficient_buying_power_returns_false_no_raise(self, tmp_path):
        from simple_strategies import _submit_and_log
        api = MagicMock()
        api.submit_order.side_effect = _FakeAPIError(
            "insufficient buying power"
        )
        assert _submit_and_log(
            api, _fake_ctx(tmp_path), symbol="AAPL", side="buy",
            qty=10000, price=200.0,
            strategy_name="random_stock_of_day",
            reason="huge buy",
        ) is False

    def test_random_strategy_continues_past_bad_ticker(self, tmp_path):
        """End-to-end: run_random_stock_of_day with a 5-pick list where
        one ticker is stale. The strategy should buy the other 4 and
        report errors=1, not crash with an unhandled exception."""
        import simple_strategies as ss
        # Mock the universe + api + price fetch so we can drive it
        # deterministically.
        api = MagicMock()

        def fake_submit(**kwargs):
            sym = kwargs["symbol"]
            if sym == "BAD":
                raise _FakeAPIError(f'asset "{sym}" not found')
            order = MagicMock()
            order.id = f"order-{sym}"
            return order
        api.submit_order.side_effect = fake_submit
        api.get_latest_trade.side_effect = lambda s: MagicMock(price=100.0)
        api.list_positions.return_value = []

        ctx = _fake_ctx(tmp_path)
        ctx.profile_id = 99
        ctx.segment = "largecap"
        ctx.display_name = "TEST"

        # Mock get_account_info to return $250K equity (so cash_per_pick
        # is enough to buy at $100/sh) — patched at the call-site path
        # the strategy uses.
        with patch("client.get_api", return_value=api), \
                patch("client.get_account_info", return_value={"equity": 250_000}), \
                patch("client.get_positions", return_value=[]), \
                patch("simple_strategies._pick_random_symbols",
                      return_value=["GOOD1", "BAD", "GOOD2", "GOOD3", "GOOD4"]), \
                patch("journal.log_trade"):
            summary = ss.run_random_stock_of_day(ctx)

        assert summary["buys"] == 4, (
            f"Expected 4 buys (1 bad ticker skipped). Got: {summary}"
        )
        assert summary["errors"] == 1, (
            f"Expected errors=1 from the bad ticker. Got: {summary}"
        )
