"""Smoke test for /api/options-backtest — every strategy in the UI
dropdown must execute end-to-end without erroring.

The original endpoint shipped broken (wrong import names, wrong module,
unsupported kwarg) and the bug only surfaced when the user clicked the
button in the UI. This test exercises every dropdown option through
the Flask test client, mocking only the historical-pricing layer (so
we don't hit Alpaca), and asserts each strategy returns HTTP 200 with
the expected response shape.

If this test had existed, the original "Error: cannot import name
'long_put' from 'options_multileg'" would have been caught in CI before
deploy.
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _td
from unittest.mock import patch

import pytest


# Strategies that the UI dropdown offers. Must stay in sync with
# templates/ai.html → ob-strategy <select>.
DROPDOWN_STRATEGIES = [
    "long_put",
    "long_call",
    "bull_call_spread",
    "bear_put_spread",
    "iron_condor",
]


@pytest.fixture
def client(tmp_main_db):
    import config
    config.DB_PATH = tmp_main_db
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        yield client


@pytest.fixture
def logged_in_client(client, tmp_main_db):
    import config
    config.DB_PATH = tmp_main_db
    from models import create_user
    create_user("test@test.com", "password123", "Test", is_admin=True)
    client.post("/login", data={
        "email": "test@test.com", "password": "password123",
    }, follow_redirects=True)
    return client


def _mock_historical_spot(symbol, as_of):
    """Constant 100 across the period — keeps strikes deterministic."""
    return 100.0


def _mock_price_option_at_date(symbol, as_of, strike, expiry, is_call,
                                 iv_override=None, bars_provider=None):
    """Constant pricing — caller doesn't care about realism here.
    Just needs `simulate_single_leg` and `simulate_multileg_strategy`
    to not return None due to missing data.
    """
    # Simple intrinsic + small time value
    spot = 100.0
    intrinsic = max(spot - strike, 0) if is_call else max(strike - spot, 0)
    days = max((expiry - as_of).days, 1)
    time_value = 1.0 * (days / 30.0)
    return {"price": intrinsic + time_value, "iv": 0.25, "delta": 0.3}


@pytest.mark.parametrize("strategy", DROPDOWN_STRATEGIES)
def test_each_strategy_returns_200_and_valid_shape(
    logged_in_client, strategy
):
    """Every option in the UI dropdown must hit the endpoint without
    erroring. Validates response shape, not P&L correctness."""
    with patch(
        "options_backtester.historical_spot",
        side_effect=_mock_historical_spot,
    ), patch(
        "options_backtester.price_option_at_date",
        side_effect=_mock_price_option_at_date,
    ):
        resp = logged_in_client.post(
            "/api/options-backtest",
            json={
                "symbol": "SPY",
                "strategy": strategy,
                "lookback_days": 60,    # short for fast test
                "otm_pct": 0.05,
                "target_dte": 30,
                "cycle_days": 7,
            },
        )

    assert resp.status_code == 200, (
        f"strategy={strategy} returned {resp.status_code}: "
        f"{resp.data.decode('utf-8', 'ignore')[:500]}"
    )
    data = resp.get_json()
    assert "error" not in data, f"endpoint returned error: {data}"
    assert "n_trades" in data
    assert "trades" in data
    assert "equity_curve" in data
    assert data["params"]["strategy"] == strategy


def test_unknown_strategy_returns_400(logged_in_client):
    """Strategies outside the supported set must reject cleanly with
    400, not crash with 500."""
    resp = logged_in_client.post(
        "/api/options-backtest",
        json={"symbol": "SPY", "strategy": "moon_shot"},
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data
    assert "moon_shot" in data["error"]


def test_missing_required_fields_returns_400(logged_in_client):
    resp = logged_in_client.post(
        "/api/options-backtest",
        json={"symbol": "SPY"},  # no strategy
    )
    assert resp.status_code == 400


def test_dropdown_options_match_endpoint_supported_strategies(logged_in_client):
    """Static check: every <option value="X"> in the synthetic options
    backtester dropdown must be a strategy the endpoint accepts.
    Prevents the failure mode where a new dropdown option is added but
    the backend handler is forgotten (or vice versa).
    """
    import os
    import re
    template = os.path.join(
        os.path.dirname(__file__), "..", "templates", "ai.html",
    )
    with open(template, encoding="utf-8") as f:
        html = f.read()
    select_match = re.search(
        r'<select[^>]*id="ob-strategy"[^>]*>([\s\S]*?)</select>', html,
    )
    assert select_match, "ob-strategy <select> not found in ai.html"
    options = re.findall(
        r'<option\s+value="([^"]+)"', select_match.group(1),
    )
    assert set(options) == set(DROPDOWN_STRATEGIES), (
        f"Dropdown options {sorted(options)} drifted from the test's "
        f"DROPDOWN_STRATEGIES list {sorted(DROPDOWN_STRATEGIES)}. "
        f"If you added a new dropdown option, also add it to "
        f"DROPDOWN_STRATEGIES so the smoke test covers it."
    )
