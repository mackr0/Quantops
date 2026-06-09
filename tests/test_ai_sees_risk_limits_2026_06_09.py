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
        with cap $25,000 (5×) and avg $5,000."""
        from ai_analyst import _build_batch_prompt
        db = _make_profile_db_with_history(tmp_path, avg_value=5000, n=10)
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db),
        )
        assert "System risk limits" in prompt, (
            "Prompt must include a 'System risk limits' header when "
            "the profile has enough history to compute the cap"
        )
        # The cap value (5 × 5000 = 25000) must appear with the
        # formatted thousands separator the operator + AI both read.
        assert "$25,000" in prompt, (
            "Max single trade $ should be 5× recent avg $5,000 = "
            "$25,000. Without this exact number, the AI sees a "
            "different floor than the gate enforces."
        )
        assert "$5,000" in prompt, (
            "Recent avg of $5,000 must be cited so the AI can sanity-"
            "check the cap against its own intuition"
        )

    def test_block_includes_per_position_cap(self, tmp_path):
        from ai_analyst import _build_batch_prompt
        db = _make_profile_db_with_history(tmp_path, avg_value=5000)
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db, max_position_pct=0.07),
        )
        assert "7% of equity" in prompt, (
            "Per-position cap should reflect the profile's actual "
            "max_position_pct (here 7%)"
        )

    def test_block_includes_book_concentration_cap(self, tmp_path):
        from ai_analyst import _build_batch_prompt
        db = _make_profile_db_with_history(tmp_path, avg_value=5000)
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db),
        )
        assert "25%" in prompt, (
            "25% book-concentration cap must be visible to the AI; "
            "this prevents the AI from proposing into a name already "
            "concentrated across sibling profiles"
        )


# ---------------------------------------------------------------------------
# Layer 2 — block omitted when no history (no false floor)
# ---------------------------------------------------------------------------

class TestRiskLimitsBlockOmittedWhenNoData:

    def test_empty_db_no_block(self, tmp_path):
        """Profile with zero trades → no risk_limits_block.
        The gate itself returns False ("no baseline") in this state;
        the prompt shouldn't fabricate a number."""
        from ai_analyst import _build_batch_prompt
        db = _make_profile_db_with_history(tmp_path, avg_value=5000, n=0)
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db),
        )
        assert "System risk limits" not in prompt, (
            "On an empty profile the gate has no baseline and the "
            "prompt must not surface a fabricated cap"
        )

    def test_under_minimum_sample_no_block(self, tmp_path):
        """The gate requires ≥5 prior trades. With 4 the prompt
        must NOT surface a cap (gate would return None)."""
        from ai_analyst import _build_batch_prompt
        db = _make_profile_db_with_history(tmp_path, avg_value=5000, n=4)
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db),
        )
        assert "System risk limits" not in prompt, (
            "Under-minimum sample (gate returns None) means no cap "
            "is computable; the prompt must not invent one"
        )


# ---------------------------------------------------------------------------
# Layer 3 — cap value matches the gate's own computation
# ---------------------------------------------------------------------------

class TestCapMatchesGateComputation:

    def test_prompt_cap_equals_gate_cap(self, tmp_path):
        """The number the AI sees MUST equal the number the gate
        enforces, byte-for-byte. Drift here means the AI sizes to
        what it thinks the cap is, the gate sees a different number,
        and trades die just over an invisible line."""
        from ai_analyst import _build_batch_prompt
        from single_trade_gate import (
            recent_avg_position_value, CATASTROPHIC_MULT,
        )
        db = _make_profile_db_with_history(tmp_path, avg_value=8000)

        # What the gate would compute
        gate_avg = recent_avg_position_value(db)
        assert gate_avg is not None
        gate_cap = round(gate_avg * CATASTROPHIC_MULT)

        # What the prompt surfaces
        prompt = _build_batch_prompt(
            [_candidate()], _portfolio_state(), _market_context(),
            ctx=_ctx(db),
        )
        # Extract the cap value from "Max single trade $: $40,000 ..."
        m = re.search(
            r"Max single trade \$:\s*\$([\d,]+)",
            prompt,
        )
        assert m is not None, (
            "Cap line missing from prompt — operator can't verify "
            "the contract holds"
        )
        prompt_cap = int(m.group(1).replace(",", ""))
        assert prompt_cap == gate_cap, (
            f"Prompt cap ({prompt_cap}) MUST equal gate cap "
            f"({gate_cap}). Drift here means AI sizes to one floor "
            f"while the gate enforces another."
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
