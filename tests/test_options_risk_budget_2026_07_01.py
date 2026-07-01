"""Fund-grade options CAPITAL-AT-RISK budget replaces the options-delta gate
(2026-07-01).

Capping options-delta at 5% of equity is the wrong metric for defined-risk
spreads (their risk is max-loss, not delta) and made options untradeable on
small accounts. The delta gate is retired to a wide runaway backstop (1.50),
and the real control is `max_options_risk_pct`: aggregate open option
max-loss + the proposed trade's max-loss must stay <= X% of NAV — the way a
real fund sizes a defined-risk book. AI-tunable within param_bounds.

This file pins:
- STORAGE: log_trade persists spread_max_loss; open_options_capital_at_risk
  sums it over OPEN option rows only (ignores stock + closed).
- BUDGET: _options_budget_exhausted True iff open CaR >= budget; fail-open.
- RETIRED DELTA GATE: dataclass + ctx + param_bounds all reflect the backstop.
- TUNING: the budget optimizer is registered; the delta optimizer is retired.
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import journal


def _tmpdb():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    journal.init_db(path)
    return path


# ---------------------------------------------------------------------------
# Storage + aggregation
# ---------------------------------------------------------------------------

def test_capital_at_risk_sums_open_option_legs_only():
    db = _tmpdb()
    try:
        # An open 2-leg spread: $500 total max-loss, $250 stamped per leg.
        journal.log_trade("AMZN", "sell", 1, price=2.0, order_id="o1",
                          occ_symbol="AMZN  260807P00230000", status="open",
                          spread_max_loss=250.0, db_path=db)
        journal.log_trade("AMZN", "buy", 1, price=1.0, order_id="o1",
                          occ_symbol="AMZN  260807P00225000", status="open",
                          spread_max_loss=250.0, db_path=db)
        # A CLOSED option leg — must NOT count.
        journal.log_trade("NVDA", "buy", 1, price=1.0, order_id="o2",
                          occ_symbol="NVDA  260807C00120000", status="closed",
                          spread_max_loss=999.0, db_path=db)
        # A stock row (no occ) — must NOT count.
        journal.log_trade("AAPL", "buy", 10, price=150.0, order_id="o3",
                          status="open", db_path=db)

        assert journal.open_options_capital_at_risk(db) == 500.0
    finally:
        os.remove(db)


def test_capital_at_risk_empty_book_is_zero():
    db = _tmpdb()
    try:
        assert journal.open_options_capital_at_risk(db) == 0.0
    finally:
        os.remove(db)


# ---------------------------------------------------------------------------
# Budget-exhaustion helper (mirrors the execution gate's comparison)
# ---------------------------------------------------------------------------

def test_budget_exhausted_true_when_open_meets_budget(monkeypatch):
    from options_strategy_advisor import _options_budget_exhausted
    monkeypatch.setattr("client.get_account_info",
                        lambda ctx=None: {"equity": 100_000.0})
    # 20% of $100k = $20k budget; open $20k -> exhausted.
    monkeypatch.setattr("journal.open_options_capital_at_risk",
                        lambda db=None: 20_000.0)
    ctx = SimpleNamespace(max_options_risk_pct=0.20, db_path="x",
                          initial_capital=100_000.0)
    assert _options_budget_exhausted(ctx) is True


def test_budget_not_exhausted_with_headroom(monkeypatch):
    from options_strategy_advisor import _options_budget_exhausted
    monkeypatch.setattr("client.get_account_info",
                        lambda ctx=None: {"equity": 100_000.0})
    monkeypatch.setattr("journal.open_options_capital_at_risk",
                        lambda db=None: 5_000.0)
    ctx = SimpleNamespace(max_options_risk_pct=0.20, db_path="x",
                          initial_capital=100_000.0)
    assert _options_budget_exhausted(ctx) is False


def test_budget_helper_failopen(monkeypatch):
    from options_strategy_advisor import _options_budget_exhausted

    def boom(ctx=None):
        raise RuntimeError("broker down")

    monkeypatch.setattr("client.get_account_info", boom)
    ctx = SimpleNamespace(max_options_risk_pct=0.20, db_path="x")
    # fail-open: never block proposals on an error
    assert _options_budget_exhausted(ctx) is False


def test_budget_gate_off_when_pct_none(monkeypatch):
    from options_strategy_advisor import _options_budget_exhausted
    ctx = SimpleNamespace(max_options_risk_pct=None, db_path="x")
    assert _options_budget_exhausted(ctx) is False


# ---------------------------------------------------------------------------
# Retired delta gate + budget defaults/bounds
# ---------------------------------------------------------------------------

def test_delta_gate_retired_to_backstop():
    from user_context import UserContext
    f = UserContext.__dataclass_fields__
    assert f["max_net_options_delta_pct"].default == 1.50   # wide backstop
    assert f["max_options_risk_pct"].default == 0.20         # real control


def test_param_bounds_reflect_retirement_and_budget():
    from param_bounds import PARAM_BOUNDS
    # delta cap can no longer be tuned back into the binding range
    assert PARAM_BOUNDS["max_net_options_delta_pct"][0] >= 1.0
    # budget is AI-tunable within a sane band
    assert PARAM_BOUNDS["max_options_risk_pct"] == (0.10, 0.40)


def test_budget_tuner_registered_delta_tuner_retired():
    import self_tuning
    src = self_tuning  # module
    # the budget optimizer exists and the delta optimizer is gone
    assert hasattr(src, "_optimize_max_options_risk_pct")
    assert not hasattr(src, "_optimize_max_net_options_delta_pct")


# ---------------------------------------------------------------------------
# HIGH #1 regression: the EXECUTION spec has no premiums, so the budget must
# use a conservative fallback (not silently count $0). This drives the REAL
# builder path — the original tests missed the no-op by hand-setting max-loss.
# ---------------------------------------------------------------------------

def test_execution_path_spec_has_conservative_max_loss():
    from datetime import date
    from options_multileg import build_bull_put_spread
    # Built the way pipelines/option._build_multileg_strategy builds it:
    # strikes + qty, NO premiums.
    spec = build_bull_put_spread("AAPL", date(2026, 8, 21), 145.0, 150.0, qty=2)
    assert spec.max_loss_per_contract is None, (
        "execution spec is premium-less — if it ever gets priced, revisit "
        "the fallback")
    # width 5pts x $100 x 2 spreads = $1000 conservative (>= true max-loss)
    assert spec.total_max_loss() == 1000.0


def test_total_max_loss_fallbacks():
    from options_multileg import OptionStrategy, OptionLeg
    leg = OptionLeg(occ_symbol="AAPL  260821P00145000", underlying="AAPL",
                    expiry="2026-08-21", strike=145.0, right="P",
                    side="sell", qty=1)
    # priced spec -> uses max_loss_per_contract
    priced = OptionStrategy(
        name="bull_put_spread", underlying="AAPL", expiry="2026-08-21",
        legs=[leg], qty=3, spread_width_points=5.0, is_credit=True,
        thesis="", max_loss_per_contract=200.0)
    assert priced.total_max_loss() == 600.0
    # width-less debit spec (strangle) -> uses |limit_price| x 100
    strangle = OptionStrategy(
        name="long_strangle", underlying="AAPL", expiry="2026-08-21",
        legs=[leg], qty=2, spread_width_points=0.0, is_credit=False,
        thesis="")
    assert strangle.total_max_loss(fallback_limit_price=3.0) == 600.0
    # nothing knowable -> 0.0 (fail-open)
    assert strangle.total_max_loss() == 0.0


def test_short_straddle_is_unsizeable():
    """A short straddle has no defined width (uncapped loss) — total_max_loss
    is 0 on the premium-less exec spec. The budget gate treats total_max_loss
    <= 0 as 'cannot size -> refuse' rather than admitting uncapped risk at $0
    (see pipelines/option.py budget gate)."""
    from datetime import date
    from options_multileg import build_short_straddle
    spec = build_short_straddle("AAPL", date(2026, 8, 21), 150.0, qty=1)
    assert spec.spread_width_points == 0
    assert spec.total_max_loss() == 0.0  # gate refuses on <= 0
