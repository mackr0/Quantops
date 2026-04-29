"""Drawdown-aware capital scaling.

P4.3 of LONG_SHORT_PLAN.md. When the book is in drawdown, the
prudent thing is to size positions smaller, not larger. This is a
Kelly safety net: even when per-trade Kelly says 10%, if we're down
12% from peak, sizes should shrink — both because the edge estimate
may be wrong and because variance compounds against us harder when
we're already down.

The scaling factor is continuous (vs the discrete normal/reduce/pause
states already in portfolio_manager.check_drawdown), and it's surfaced
to the AI prompt as soft guidance that combines with the Kelly
recommendation:

  Suggested size = Kelly × drawdown_scale × max_position_pct

Schedule (linear interpolation between breakpoints):
   0% drawdown → 1.00× (full size)
   5% drawdown → 0.85×
  10% drawdown → 0.65×
  15% drawdown → 0.45×
  20%+        → 0.25× (defensive floor)

This is independent of the existing pause threshold — pause stops
NEW entries entirely, while scaling shrinks sizes for entries that
do happen.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Breakpoints: (drawdown_pct, scale)
_SCHEDULE = [
    (0.0, 1.00),
    (5.0, 0.85),
    (10.0, 0.65),
    (15.0, 0.45),
    (20.0, 0.25),
]
_FLOOR_SCALE = 0.25


def compute_capital_scale(drawdown_pct: float) -> float:
    """Return capital-scaling factor in [0.25, 1.0] for given drawdown.

    Args:
      drawdown_pct: current drawdown as a positive percent number
        (e.g., 12.5 means we're 12.5% below peak equity).

    Linear interpolation between breakpoints; floors at 0.25.
    """
    if drawdown_pct is None or drawdown_pct <= 0:
        return 1.0
    if drawdown_pct >= _SCHEDULE[-1][0]:
        return _FLOOR_SCALE
    for i in range(len(_SCHEDULE) - 1):
        lo_dd, lo_scale = _SCHEDULE[i]
        hi_dd, hi_scale = _SCHEDULE[i + 1]
        if lo_dd <= drawdown_pct <= hi_dd:
            # Linear interp.
            if hi_dd == lo_dd:
                return lo_scale
            t = (drawdown_pct - lo_dd) / (hi_dd - lo_dd)
            return lo_scale + t * (hi_scale - lo_scale)
    return 1.0


def render_for_prompt(dd: Optional[Dict[str, Any]]) -> str:
    """Format the drawdown-scaling guidance as an AI prompt block.

    Returns empty string when not in drawdown (scale = 1.0) or when
    no drawdown data is available — no block is better than a noisy
    "we're fine" line.

    Expects `dd` shaped like portfolio_manager.check_drawdown output:
      {drawdown_pct, peak_equity, current_equity, action}.
    """
    if not dd:
        return ""
    dd_pct = dd.get("drawdown_pct")
    if dd_pct is None or dd_pct <= 0:
        return ""
    scale = compute_capital_scale(dd_pct)
    # Suppress when the rounded display value would read 1.00× — no
    # point telling the AI "multiply by 1.00".
    if round(scale, 2) >= 1.00:
        return ""
    peak = dd.get("peak_equity") or 0
    current = dd.get("current_equity") or 0
    return (
        f"\nDRAWDOWN CAPITAL SCALE: {scale:.2f}× "
        f"(drawdown {dd_pct:.1f}%, peak ${peak:,.0f} → current ${current:,.0f})\n"
        f"  Multiply your suggested position sizes by this factor.\n"
        f"  Recover-mode sizing: smaller bets while we're below peak.\n"
    )
