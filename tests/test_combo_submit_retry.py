"""Pin the combo-path 5xx retry in `_combo_submit_with_retry`.

Caught 2026-05-10: Alpaca's paper MLEG endpoint returns transient
500s on ~30% of multileg submissions. Without retry, every 500 falls
through to the sequential path, which is non-atomic and can leave
the AI with naked single-leg positions when one leg later expires
unfilled (the 3-orphan incident on profiles 6 + 7).

This test pins:
1. 5xx errors are retried (up to max_retries times) with backoff.
2. 4xx errors are NOT retried (client error — won't help).
3. Network/timeout errors are retried.
4. After max retries, the final exception re-raises so the caller's
   sequential fallback still fires.
5. A successful response on retry returns normally; subsequent
   attempts are skipped.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Patch time.sleep so retries are instant in tests. The retry
    helper imports `time` inside the function body, so patching the
    builtin module globally is the right scope."""
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *a, **kw: None)


class TestComboSubmitWithRetry:
    def test_500_retries_then_succeeds(self):
        """First call raises 500, second call succeeds. Function
        returns the success value without re-raising."""
        from options_multileg import _combo_submit_with_retry

        success = MagicMock()
        success.id = "combo-1"
        call_count = {"n": 0}

        def fake_submit(api, payload):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError(
                    'Alpaca order rejected (500): {"code":50010000}'
                )
            return success

        with patch("options_multileg._submit_alpaca_order_raw",
                   side_effect=fake_submit):
            result = _combo_submit_with_retry(MagicMock(), {})

        assert result is success
        assert call_count["n"] == 2

    def test_4xx_does_not_retry(self):
        """A 400/401/403/422 response is a client error — no point
        retrying. Must raise immediately after one attempt."""
        from options_multileg import _combo_submit_with_retry

        call_count = {"n": 0}

        def fake_submit(api, payload):
            call_count["n"] += 1
            raise RuntimeError(
                'Alpaca order rejected (422): bad symbol'
            )

        with patch("options_multileg._submit_alpaca_order_raw",
                   side_effect=fake_submit):
            with pytest.raises(RuntimeError, match="422"):
                _combo_submit_with_retry(MagicMock(), {})

        assert call_count["n"] == 1, (
            f"4xx must not retry — got {call_count['n']} attempts"
        )

    def test_max_retries_then_reraises(self):
        """If every attempt 500s, the final exception re-raises so
        the caller's sequential fallback can still fire."""
        from options_multileg import _combo_submit_with_retry

        call_count = {"n": 0}

        def fake_submit(api, payload):
            call_count["n"] += 1
            raise RuntimeError(
                'Alpaca order rejected (503): service unavailable'
            )

        with patch("options_multileg._submit_alpaca_order_raw",
                   side_effect=fake_submit):
            with pytest.raises(RuntimeError, match="503"):
                _combo_submit_with_retry(
                    MagicMock(), {}, max_retries=2,
                )

        # max_retries=2 means initial + 2 retries = 3 attempts total
        assert call_count["n"] == 3, (
            f"Expected 3 attempts (initial + 2 retries), got "
            f"{call_count['n']}"
        )

    def test_requests_network_error_is_retried(self):
        """`requests.exceptions.ConnectionError`/`Timeout` (what
        `_submit_alpaca_order_raw`'s `requests.post` actually raises
        on a real network failure) are transient and should retry."""
        import requests
        from options_multileg import _combo_submit_with_retry

        call_count = {"n": 0}
        success = MagicMock()
        success.id = "combo-after-network-retry"

        def fake_submit(api, payload):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise requests.exceptions.ConnectionError("DNS failed")
            return success

        with patch("options_multileg._submit_alpaca_order_raw",
                   side_effect=fake_submit):
            result = _combo_submit_with_retry(MagicMock(), {})

        assert result is success
        assert call_count["n"] == 2

    def test_bare_exception_is_not_retried(self):
        """A bare `Exception("MLEG not supported on this account")`
        (permanent account-config issue) or unexpected error type
        like `KeyError` must fail fast — retrying wastes time and
        could mask a real bug. The caller's outer try/except logs
        it and falls through to sequential."""
        from options_multileg import _combo_submit_with_retry

        call_count = {"n": 0}

        def fake_submit(api, payload):
            call_count["n"] += 1
            raise Exception("MLEG not supported on this account")

        with patch("options_multileg._submit_alpaca_order_raw",
                   side_effect=fake_submit):
            with pytest.raises(Exception, match="MLEG not supported"):
                _combo_submit_with_retry(MagicMock(), {})

        assert call_count["n"] == 1, (
            f"Bare Exception must not retry — got {call_count['n']} "
            f"attempts"
        )

    def test_first_attempt_success_no_retry(self):
        """If the first attempt succeeds, we don't retry — returns
        immediately."""
        from options_multileg import _combo_submit_with_retry

        success = MagicMock()
        call_count = {"n": 0}

        def fake_submit(api, payload):
            call_count["n"] += 1
            return success

        with patch("options_multileg._submit_alpaca_order_raw",
                   side_effect=fake_submit):
            result = _combo_submit_with_retry(MagicMock(), {})

        assert result is success
        assert call_count["n"] == 1


class TestComboPathFallthroughOnRetryExhaustion:
    """Verify the existing sequential fallback still fires when the
    combo path exhausts retries — Mack's safety net behavior must
    survive the retry layer addition."""

    def test_combo_500_exhausted_falls_through_to_sequential(self):
        """The combo path's outer try/except catches any exception
        from `_combo_submit_with_retry` and falls through to
        sequential. Verify by mocking `_submit_alpaca_order_raw` to
        always 500 and confirming the sequential path is reached
        (which itself calls `_submit_alpaca_order_raw` with the
        single-leg payload shape — no `legs` key)."""
        from options_multileg import (
            execute_multileg_strategy, build_bull_call_spread,
        )
        from datetime import date

        strategy = build_bull_call_spread(
            underlying="CWAN",
            expiry=date(2026, 6, 12),
            lower_strike=26.0,
            upper_strike=27.0,
            qty=3,
        )

        sequential_calls = []
        attempt_log = []

        def fake_submit(api, payload):
            attempt_log.append(payload)
            if "legs" in payload:
                # Combo path — always 500
                raise RuntimeError(
                    'Alpaca order rejected (500): {"code":50010000}'
                )
            # Sequential leg path — succeeds, returns a fake order
            sequential_calls.append(payload)
            o = MagicMock()
            o.id = f"seq-leg-{len(sequential_calls)}"
            return o

        ctx = MagicMock()
        ctx.db_path = None  # skip journal logging
        ctx.segment = "test"

        with patch("options_multileg._submit_alpaca_order_raw",
                   side_effect=fake_submit):
            # Skip the contract-snap / dup-guard so we can exercise
            # the submit path cleanly.
            with patch("options_chain_alpaca.list_available_contracts",
                       return_value=[]):
                result = execute_multileg_strategy(
                    api=MagicMock(), strategy=strategy, ctx=ctx,
                    log=False,
                )

        # Combo: 1 initial + 2 retries = 3 attempts (all 500)
        combo_attempts = [p for p in attempt_log if "legs" in p]
        assert len(combo_attempts) == 3, (
            f"Expected 3 combo attempts (initial+2 retries), got "
            f"{len(combo_attempts)}"
        )
        # Sequential then ran for each leg
        assert len(sequential_calls) == len(strategy.legs)
        assert result["action"] == "MULTILEG_OPEN"
        assert "sequentially" in result["reason"]
