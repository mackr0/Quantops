"""Pre-submission order guard.

Every order must pass through `check_can_submit` before calling
`api.submit_order`. This catches the bug where a scan cycle starts
within schedule but the pipeline takes long enough that the actual
order submission falls outside schedule.

Without this guard, after-hours trades happen accidentally on profiles
set to market_hours — the scheduler only checks schedule at cycle
start, not at order time.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def check_can_submit(ctx, symbol: str, side: str) -> bool:
    """Return True if the profile's schedule allows an order right now.

    Logs a warning and returns False if the order would fall outside
    the profile's configured trading window. The caller should skip
    the order — not queue it for later (Alpaca paper fills after-hours
    orders, which is what caused the original bug).
    """
    if ctx is None:
        return True

    now = datetime.now(_ET)

    if ctx.is_within_schedule(now):
        return True

    seg_label = getattr(ctx, "display_name", None) or getattr(ctx, "segment", "unknown")
    logger.warning(
        "[%s] Order BLOCKED: %s %s at %s ET is outside schedule (%s). "
        "The scan cycle started within schedule but the pipeline took "
        "long enough that execution fell outside the window.",
        seg_label, side.upper(), symbol,
        now.strftime("%-I:%M %p"), ctx.schedule_type,
    )
    return False
