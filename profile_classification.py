"""Single source of truth for classifying a trading profile as an
experiment BASELINE (a non-AI control) versus our AI SYSTEM.

The experiment design (docs/15) runs three control profiles alongside
the AI strategies:
  - EXP-A1-BuyHoldSPY   strategy_type='buy_hold'  (100% SPY, weekly rebal)
  - EXP-A1-RandomA/B    strategy_type='random'    (random symbols/day)
These are the benchmark we measure the system AGAINST; they are not the
system. Folding their equity/P&L into a system aggregate, or their (zero)
predictions into AI-accuracy stats, is meaningless and misleading.

Classification is structural, not an allowlist: a profile is a baseline
unless its strategy_type is the AI pipeline ('ai'). Done this way, a NEW
control type added later (e.g. 'momentum_baseline', 'buy_hold_qqq') is
treated as a baseline AUTOMATICALLY — it cannot silently leak into the
system aggregates the day someone adds it. (test-for-the-class, not the
instance — see tests/test_profile_classification.py.)

Used by: the dashboard overview, /performance "All System Profiles"
aggregate, /ai-performance, and comparative_returns.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

# The one strategy_type that denotes our AI system. Everything else is a
# control/baseline. Kept as a constant so the AI value has one name.
AI_STRATEGY_TYPE = "ai"


def is_baseline_strategy(strategy_type: Optional[str]) -> bool:
    """True if this strategy_type is a non-AI experiment control.

    A missing/blank strategy_type defaults to AI (the column default is
    'ai'), so it is NOT treated as a baseline — only an explicit non-'ai'
    value is. Comparison is case/space-insensitive for robustness.
    """
    normalized = (strategy_type or "").strip().lower()
    # Empty (missing/whitespace) → AI default → not a baseline.
    return normalized not in ("", AI_STRATEGY_TYPE)


def is_baseline_profile(profile: Mapping[str, Any]) -> bool:
    """Convenience: classify a profile dict/row by its strategy_type."""
    return is_baseline_strategy(profile.get("strategy_type"))
