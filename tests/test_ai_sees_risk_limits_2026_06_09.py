"""2026-06-09 — surface system risk limits to the AI prompt.

Pre-fix: the AI didn't know about the doomsday gates' thresholds.
Confidence-tier sizing routinely produced proposals above 5× recent
average trade size — the Catastrophic Single Trade Gate caught them
silently. Burned Gemini cost on confidence/reasoning that died at
the gate; left operator with mysterious BLOCKED badges.

Post-fix: `_build_batch_prompt` builds a `risk_limits_block` showing:
  - Maximum single trade $ (= 5× recent avg trade size, from
    `single_trade_gate.recent_avg_position_value`)
  - Per-position cap (= profile's `max_position_pct`)
  - Book concentration cap (the 25% cross-profile floor)

The gates themselves are unchanged — same safety net. The AI just
gets the rules it has to size within so wasted cycles disappear.

Contract pinned:
  1. When the profile DB has enough history (≥5 prior stock trades),
     the block appears.
  2. The dollar threshold matches `single_trade_gate`'s computation
     exactly — drift between the two values would mean the AI sees
     one number and the gate enforces a different one.
  3. Empty / no-history profile → block is absent (no false floor
     surfaced before there's data).
  4. Structural pin on `risk_limits_block` being woven into the
     `portfolio_section` so a refactor can't silently drop it.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Test scaffolding — a minimal profile DB with N completed BUY trades
# ---------------------------------------------------------------------------

def _make_profile_db_with_history(tmp_path, avg_value: float, n: int = 10):
    """Create a profile DB with `n` BUY trades each ~`avg_value`
    dollars, using the canonical journal schema so downstream queries
    in `_build_batch_prompt` (options_roll_manager, etc.) don't break
    on missing columns."""
    from journal import init_db
    db = str(tmp_path / "p.db")
    init_db(db)
    conn = sqlite3.connect(db)
    for i in range(n):
        qty = 100
        price = round(avg_value / 100, 2)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "                    signal_type, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"2026-06-0{i % 9 + 1}T10:00", f"TICK{i}", "buy",
             qty, price, "BUY", "closed"),
        )
    conn.commit()
    conn.close()
    return db


def _ctx(db_path, max_position_pct=0.10, enable_options=False):
    return SimpleNamespace(
        db_path=db_path,
        max_position_pct=max_position_pct,
        max_total_positions=10,
        enable_options=enable_options,
        enable_short_selling=False,
        ai_confidence_threshold=50,
        segment="stocks",
        target_short_pct=0.0,
        short_max_position_pct=0.05,
    )


def _portfolio_state():
    return {
        "equity": 250_000,
        "cash": 100_000,
        "positions": [],
        "num_positions": 0,
        "drawdown_pct": 0.0,
        "account": {"equity": 250_000},
    }


def _market_context():
    return {"regime": "neutral", "vix": 18.0, "spy_trend": "flat"}


def _candidate(symbol="AAPL"):
    return {
        "symbol": symbol, "price": 150.0, "signal": "BUY",
        "score": 0.7,
        "rsi": 55, "volume_ratio": 1.0, "atr": 1.0, "adx": 20,
        "stoch_rsi": 50, "roc_10": 1.0, "pct_from_52w_high": 0.05,
        "mfi": 50, "cmf": 0, "squeeze": 0, "pct_from_vwap": 0,
        "nearest_fib_dist": 99, "gap_pct": 0,
    }


# ---------------------------------------------------------------------------
# Layer 1 — block appears when there's history
# ---------------------------------------------------------------------------

class TestRiskLimitsBlockAppears:

    def test_block_present_with_sufficient_history(self, tmp_path):
        """Profile with 10 prior BUYs averaging $5000 → block appears
        with the new position-cap-primary framing."""
        from ai_analyst import _build_batch_prompt
        db = _make_profile_db_with_history(tmp_path, avg_value=5000, n=10)
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db),
        )
        assert "System risk limits" in prompt, (
            "Prompt must include a 'System risk limits' header"
        )
        # Primary cap is position cap ($25K = 10% × $250K)
        assert "$25,000" in prompt
        assert "10.0% of equity" in prompt

    def test_block_includes_per_position_cap_with_custom_pct(self, tmp_path):
        from ai_analyst import _build_batch_prompt
        db = _make_profile_db_with_history(tmp_path, avg_value=5000)
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db, max_position_pct=0.07),
        )
        assert "7.0% of equity" in prompt, (
            "Per-position cap should reflect the profile's actual "
            "max_position_pct (here 7%)"
        )

    def test_prompt_has_no_cross_profile_concentration_leak(self, tmp_path):
        """The cross-profile book-concentration cap was REMOVED 2026-06-30.
        Profiles are independent virtual accounts — the AI prompt must NOT
        reference sibling/other profiles' holdings or any cross-profile
        aggregate. Per-profile concentration is conveyed only by the
        own-book PORTFOLIO FIT signal."""
        from ai_analyst import _build_batch_prompt
        db = _make_profile_db_with_history(tmp_path, avg_value=5000)
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db),
        )
        low = prompt.lower()
        for leak in ("sibling profile", "across sibling", "other profiles",
                     "across profiles", "total book exposure across"):
            assert leak not in low, (
                "AI prompt must not leak cross-profile concentration: %r" % leak)
        assert "Max position size" in prompt  # per-profile cap still shown


# ---------------------------------------------------------------------------
# Layer 2 — block omitted when no history (no false floor)
# ---------------------------------------------------------------------------

class TestRiskLimitsBlockBehaviorWithoutHistory:

    def test_empty_db_block_still_shows_position_cap(self, tmp_path):
        """Profile with zero trades → block still shows the position
        cap (it's operator-configured; doesn't need history). The
        anti-anomaly backstop line is omitted since recent_avg
        can't be computed."""
        from ai_analyst import _build_batch_prompt
        db = _make_profile_db_with_history(tmp_path, avg_value=5000, n=0)
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db),
        )
        # Position cap shown — it's always available
        assert "System risk limits" in prompt
        assert "Max position size" in prompt
        # Backstop line NOT shown (no avg to compute it from)
        assert "Anti-anomaly backstop" not in prompt, (
            "Without history the prompt must NOT invent a backstop "
            "threshold; the line is conditional on recent_avg"
        )

    def test_under_minimum_sample_omits_backstop_line(self, tmp_path):
        """Gate requires ≥5 prior trades for recent_avg; with 4 the
        position-cap line shows but the backstop line is absent."""
        from ai_analyst import _build_batch_prompt
        db = _make_profile_db_with_history(tmp_path, avg_value=5000, n=4)
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db),
        )
        assert "Max position size" in prompt
        assert "Anti-anomaly backstop" not in prompt


# ---------------------------------------------------------------------------
# Layer 3 — cap value matches the gate's own computation
# ---------------------------------------------------------------------------

class TestPromptUsesFlooredGateThreshold:

    def test_prompt_backstop_threshold_matches_floored_gate(self, tmp_path):
        """The number the AI sees in the anti-anomaly backstop line
        MUST equal `max(5 × recent_avg, position_cap × equity)` —
        i.e., what the gate actually enforces with the floor."""
        from ai_analyst import _build_batch_prompt
        from single_trade_gate import (
            recent_avg_position_value, CATASTROPHIC_MULT,
        )
        # avg = $8000 → 5× = $40K. position cap = 10% × $250K = $25K.
        # max($40K, $25K) = $40K. Backstop fires at $40K.
        db = _make_profile_db_with_history(tmp_path, avg_value=8000)
        gate_avg = recent_avg_position_value(db)
        assert gate_avg is not None
        raw_threshold = round(gate_avg * CATASTROPHIC_MULT)
        position_cap = 25_000  # 10% × $250K
        gate_threshold = max(raw_threshold, position_cap)

        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db),
        )
        m = re.search(r"single trade > \$([\d,]+)", prompt)
        assert m is not None, "backstop line missing"
        prompt_threshold = int(m.group(1).replace(",", ""))
        assert prompt_threshold == gate_threshold, (
            f"Prompt shows ${prompt_threshold:,} but gate enforces "
            f"${gate_threshold:,}. The two must match exactly."
        )


# ---------------------------------------------------------------------------
# Layer 4 — structural pin (refactor protection)
# ---------------------------------------------------------------------------

def test_risk_limits_block_woven_into_portfolio_section():
    """`risk_limits_block` MUST appear inside `portfolio_section`'s
    interpolation. A refactor that drops the variable from the
    f-string silently disables the whole feature.

    The f-string has many nested parens (function calls) so we
    don't try to delimit it with a regex. Instead: find the
    `portfolio_section = (` opener and confirm `{risk_limits_block}`
    appears within the next ~50 lines."""
    src = (REPO_ROOT / "ai_analyst.py").read_text()
    opener_idx = src.find("portfolio_section = (")
    assert opener_idx > 0, (
        "Couldn't find `portfolio_section = (` opener — test anchor broke"
    )
    # Take the next ~50 lines of source and look for the interpolation
    window = src[opener_idx:opener_idx + 4000]
    assert "{risk_limits_block}" in window, (
        "risk_limits_block must be interpolated into portfolio_section "
        "within ~50 lines of the opener. Without this f-string entry "
        "the variable is built but never reaches the prompt — silent "
        "disable."
    )


def test_risk_limits_block_built_from_single_trade_gate():
    """Source code pin — risk_limits_block must derive its number
    from single_trade_gate.recent_avg_position_value. A refactor
    that hardcodes a number, or computes its own average with a
    different filter, breaks the "AI sees what the gate enforces"
    contract."""
    src = (REPO_ROOT / "ai_analyst.py").read_text()
    assert "recent_avg_position_value" in src, (
        "ai_analyst.py must import recent_avg_position_value from "
        "single_trade_gate so the prompt's cap and the gate's "
        "computation can't drift"
    )
    assert "CATASTROPHIC_MULT" in src, (
        "ai_analyst.py must reference CATASTROPHIC_MULT (the 5× "
        "multiplier) by name — a hardcoded 5 in the prompt would "
        "drift the day the gate's multiplier changes"
    )
