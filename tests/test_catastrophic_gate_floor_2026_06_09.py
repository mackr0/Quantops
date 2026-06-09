"""2026-06-09 — catastrophic single-trade gate floor.

Pre-fix: `is_catastrophic` compared `proposed > 5 × recent_avg`. For
young profiles with small recent_avg, this threshold sat BELOW the
operator-configured `max_position_pct × equity`. Every within-
position-cap trade got blocked → only smaller trades got accepted
→ recent_avg stayed small → cap stayed small → death spiral.
Pid 47/48 ($25K, recent avg $1K, gate cap $5K = position cap) and
pid 41 ($250K, recent avg $4,713, gate cap $23K vs position cap
$25K) both demonstrated the pattern this morning.

Post-fix: `threshold = max(5 × recent_avg, max_position_dollars)`.
The gate cannot tighten below the operator's per-trade ceiling.
For mature profiles (large recent_avg), the 5× anomaly catch still
fires. For young profiles, the position cap binds first — same
behavior as if the gate weren't in scope. No death spiral.

Contract pinned at three levels:

  1. `is_catastrophic` accepts and honors `max_position_dollars`.
  2. `trade_pipeline` passes `equity × max_position_pct` as the
     floor.
  3. AI prompt's risk_limits_block describes the gate as a
     "backstop above position cap" with the correct math.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Layer 1 — gate honors the floor
# ---------------------------------------------------------------------------

def _make_db_with_avg(tmp_path, avg_dollars: float, n: int = 10):
    """Profile DB whose recent stock-trade avg is approximately
    `avg_dollars`."""
    from journal import init_db
    db = str(tmp_path / "p.db")
    init_db(db)
    conn = sqlite3.connect(db)
    qty = 100
    price = round(avg_dollars / qty, 4)
    for i in range(n):
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "                    signal_type, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"2026-06-0{i % 9 + 1}T10:00", f"T{i}", "buy",
             qty, price, "BUY", "closed"),
        )
    conn.commit()
    conn.close()
    return db


class TestGateFloorBehavior:

    def test_floor_lifts_threshold_when_avg_is_small(self, tmp_path):
        """The pid 41 reproduction. recent avg $5K → 5× = $25K. With
        a position-cap floor of $30K (e.g., 10% × $300K equity), the
        gate should NOT fire on a $28K trade."""
        from single_trade_gate import is_catastrophic
        db = _make_db_with_avg(tmp_path, 5_000)
        # $28K is < $30K position cap floor, > $25K raw 5× threshold
        cat, reason, detail = is_catastrophic(
            28_000, db_path=db, max_position_dollars=30_000,
        )
        assert cat is False, (
            f"With position-cap floor at $30K, a $28K trade must "
            f"NOT fire the gate. Got fire with reason: {reason}"
        )
        assert detail["floor_applied"] is True, (
            "When max_position_dollars > raw 5× threshold, "
            "floor_applied MUST be True so the operator can see "
            "the gate's actual behavior"
        )

    def test_floor_does_not_loosen_when_avg_is_large(self, tmp_path):
        """Mature profile: recent avg $50K → 5× = $250K. With a
        position-cap floor of $30K, the gate's threshold stays at
        $250K (the floor doesn't pull threshold DOWN; it only
        prevents the threshold from dropping below the floor)."""
        from single_trade_gate import is_catastrophic
        db = _make_db_with_avg(tmp_path, 50_000)
        cat, reason, detail = is_catastrophic(
            240_000, db_path=db, max_position_dollars=30_000,
        )
        # $240K < $250K threshold; gate doesn't fire
        assert cat is False
        assert detail["floor_applied"] is False, (
            "When raw 5× threshold ($250K) > floor ($30K), "
            "floor_applied must be False — the floor wasn't the "
            "binding number"
        )
        # And a $300K trade DOES fire (the anomaly catch works)
        cat2, _, _ = is_catastrophic(
            300_000, db_path=db, max_position_dollars=30_000,
        )
        assert cat2 is True, (
            "The 5× anomaly catch must still fire on mature profiles. "
            "The floor only RAISES the threshold; it never lowers it."
        )

    def test_floor_optional_back_compat(self, tmp_path):
        """`max_position_dollars` is optional. When None or 0, gate
        behaves as before (raw 5× threshold)."""
        from single_trade_gate import is_catastrophic
        db = _make_db_with_avg(tmp_path, 5_000)
        cat, reason, detail = is_catastrophic(28_000, db_path=db)
        # Raw threshold $25K → $28K fires
        assert cat is True, (
            "Without a floor passed, gate must behave as before — "
            "$28K > $25K (5× $5K avg) → fires"
        )
        assert detail["floor_applied"] is False


# ---------------------------------------------------------------------------
# Layer 2 — trade_pipeline passes the floor
# ---------------------------------------------------------------------------

def test_trade_pipeline_passes_max_position_dollars_to_gate():
    """Structural pin: the call site at trade_pipeline.py:824 must
    include `max_position_dollars=proposed_dollars` so the floor
    actually reaches the gate. Without this the fix is a no-op."""
    src = (REPO_ROOT / "trade_pipeline.py").read_text()
    # Locate the is_catastrophic call line, then verify the kwarg
    # appears within the next few lines (call is multi-line so a
    # one-shot regex is brittle).
    call_idx = src.find("is_catastrophic(")
    assert call_idx > 0, "is_catastrophic call site missing"
    # Look in the next ~400 chars (covers a generous multi-line call)
    window = src[call_idx:call_idx + 400]
    assert "max_position_dollars=proposed_dollars" in window, (
        "trade_pipeline.py must call is_catastrophic with "
        "max_position_dollars=proposed_dollars within the same call. "
        "Without this kwarg the floor never reaches the gate and the "
        "death-spiral fix doesn't take effect."
    )


# ---------------------------------------------------------------------------
# Layer 3 — AI prompt describes the gate as a backstop
# ---------------------------------------------------------------------------

class TestPromptDescribesGateAsBackstop:

    def _ctx(self, db_path, max_position_pct=0.10):
        from types import SimpleNamespace
        return SimpleNamespace(
            db_path=db_path,
            max_position_pct=max_position_pct,
            max_total_positions=10,
            enable_options=False,
            enable_short_selling=False,
            ai_confidence_threshold=50,
            segment="stocks",
            target_short_pct=0.0,
            short_max_position_pct=0.05,
        )

    def _portfolio(self, equity=250_000):
        return {
            "equity": equity, "cash": 100_000,
            "positions": [],
            "num_positions": 0,
            "drawdown_pct": 0.0,
            "account": {"equity": equity},
        }

    def _candidate(self):
        return {
            "symbol": "AAPL", "price": 150.0, "signal": "BUY",
            "score": 0.7,
            "rsi": 55, "volume_ratio": 1.0, "atr": 1.0, "adx": 20,
            "stoch_rsi": 50, "roc_10": 1.0, "pct_from_52w_high": 0.05,
            "mfi": 50, "cmf": 0, "squeeze": 0, "pct_from_vwap": 0,
            "nearest_fib_dist": 99, "gap_pct": 0,
        }

    def test_prompt_marks_position_cap_as_primary(self, tmp_path):
        from ai_analyst import _build_batch_prompt
        db = _make_db_with_avg(tmp_path, 5_000)
        prompt = _build_batch_prompt(
            [self._candidate()], self._portfolio(),
            {"regime": "neutral", "vix": 18.0, "spy_trend": "flat"},
            ctx=self._ctx(db),
        )
        # The new line marks Max position size as ">>> primary cap <<<"
        assert "Max position size" in prompt
        assert "primary cap" in prompt, (
            "The new block must label the position cap as primary so "
            "the AI sizes by THAT and not by the secondary anomaly "
            "backstop"
        )

    def test_prompt_describes_gate_as_anomaly_backstop(self, tmp_path):
        from ai_analyst import _build_batch_prompt
        db = _make_db_with_avg(tmp_path, 5_000)
        prompt = _build_batch_prompt(
            [self._candidate()], self._portfolio(),
            {"regime": "neutral", "vix": 18.0, "spy_trend": "flat"},
            ctx=self._ctx(db),
        )
        assert "Anti-anomaly backstop" in prompt, (
            "The catastrophic gate is now a backstop ABOVE the "
            "position cap. The prompt should describe it that way "
            "so the AI doesn't try to size by it."
        )
        # And the number shown must equal max(5×avg, position cap)
        # — i.e. $25K (5 × $5K avg) but floored at $25K (10% × $250K)
        m = re.search(
            r"single trade > \$([\d,]+)", prompt,
        )
        assert m is not None, (
            "The anomaly-backstop line must show the threshold $ "
            "value so the operator can verify the contract"
        )
        threshold = int(m.group(1).replace(",", ""))
        # Should be max($25K from 5× avg, $25K from position cap) = $25K
        assert 24_000 <= threshold <= 26_000, (
            f"Threshold should be ~$25K (5×$5K avg = position "
            f"cap 10%×$250K). Got ${threshold:,}."
        )

    def test_prompt_no_more_effective_max_line(self, tmp_path):
        """The earlier-today EFFECTIVE max line is gone — with the
        gate floored, effective max IS max_position_pct, and the
        position-cap line already conveys that."""
        from ai_analyst import _build_batch_prompt
        db = _make_db_with_avg(tmp_path, 5_000)
        prompt = _build_batch_prompt(
            [self._candidate()], self._portfolio(),
            {"regime": "neutral", "vix": 18.0, "spy_trend": "flat"},
            ctx=self._ctx(db),
        )
        assert "EFFECTIVE max size_pct" not in prompt, (
            "With the gate floored, EFFECTIVE max == max_position_pct "
            "and the dedicated EFFECTIVE line is redundant. Removed "
            "to keep the block concise."
        )
