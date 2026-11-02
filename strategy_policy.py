"""Operator entry-quality policy for screener strategies (2026-11-02,
docs/25 D13; evidence in docs/26 Experiment 2 §4).

Measured on every AI-selected directional prediction resolved since
the 2026-08-24 reset (10,532 across twelve books; outcome = the raw
underlying move at the horizon, sign-adjusted for direction):

    strategy                     n    win    mean move per call
    batch_ai (AI's own pick)  1,565     —    +1.81%
    volume_spike              1,712   61%    +0.70%
    mean_reversion                —     —    +0.39%
    sector_momentum               —     —    +0.24%
    analyst_upgrade_drift         —   39%    −0.14%
    vol_regime                  400   50%    −0.92%
    insider_selling_cluster   1,158   34%    −1.12%   (largest single drag)
    earnings_drift               64   23%    −3.76%

The three at the bottom lose on their own direction with enough
samples that it is not noise. They are removed from the ACTIVE SET —
they do not run, so their votes never reach the shortlist, the prompt
or the prediction journal — rather than down-weighted: a prompt hint
is advisory and the selector kept trading them. The removal is a code
constant (every book at once, reviewable, reverted by one deploy), not
a per-profile flag: the books are one experiment and policy rolls to
all of them together (operator rule).

`volume_spike` is the one screener with a clear positive edge; its
vote carries a note in the candidate line so the selector sees that
this book's own record favors it. Nothing is hard-filtered toward it.

Fail-closed: a disabled strategy is excluded wherever the active set is
decided (`strategies.get_active_strategies`, `get_shadow_strategies`),
again at ranking (`trade_pipeline._rank_candidates` drops a candidate
whose primary vote is disabled, through the deprecated-strategy path),
and its vote is omitted from the prompt line if one is ever present.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

#: Screener strategies that lose on their own direction (module doc).
OPERATOR_DISABLED_STRATEGIES = frozenset({
    "insider_selling_cluster",
    "earnings_drift",
    "vol_regime",
})

#: Strategies whose vote carries a note in the candidate line.
EMPHASIZED_STRATEGIES: Dict[str, str] = {
    "volume_spike": ("this book's most reliable screener since 2026-08-24: "
                     "61% directional hit rate, +0.70% per call"),
}

EMPHASIS_MARK = "★"


def is_disabled(strategy_name: Optional[str]) -> bool:
    return bool(strategy_name) and strategy_name in OPERATOR_DISABLED_STRATEGIES


def filter_active(names: Iterable[str]) -> set:
    """The subset of `names` that policy allows to run."""
    return {n for n in names if not is_disabled(n)}


def primary_strategy(votes: Optional[Dict[str, Any]]) -> Optional[str]:
    """The first non-HOLD vote — the convention `trade_pipeline` uses
    for a prediction's `strategy_type`."""
    for k, v in (votes or {}).items():
        if v != "HOLD":
            return k
    return None


def render_votes(votes: Optional[Dict[str, Any]]) -> str:
    """The `Votes:` text for one candidate line in the selector prompt:
    non-HOLD votes only, disabled strategies omitted, an emphasized
    strategy's vote followed by its note."""
    parts = []
    for k, v in (votes or {}).items():
        if v == "HOLD" or is_disabled(k):
            continue
        note = EMPHASIZED_STRATEGIES.get(k)
        parts.append(f"{k}={v} {EMPHASIS_MARK} [{note}]" if note else f"{k}={v}")
    return ", ".join(parts) if parts else "no strong votes"
