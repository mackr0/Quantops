"""CONFIRM signal when volume_ratio >= 2x AND we're in the
afternoon session — afternoon volume surges have higher signal
than morning gappy volume."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

NAME = "strong_volume_late_session"
DESCRIPTION = "CONFIRM signal when volume >= 2x in the afternoon session"
APPLIES_TO_SIGNALS = ("BUY", "STRONG_BUY", "WEAK_BUY",
                       "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")


def evaluate(candidate: Dict[str, Any], ctx: Any = None) -> Optional[Dict[str, Any]]:
    vr = candidate.get("volume_ratio")
    if vr is None:
        return None
    try:
        v = float(vr)
    except (TypeError, ValueError):
        return None
    if v < 2.0:
        return None
    et = datetime.now(tz=timezone.utc) - timedelta(hours=4)
    if et.weekday() >= 5:
        return None
    minute_of_day = et.hour * 60 + et.minute
    # 13:00 ET - 15:30 ET (afternoon session)
    if not (13*60 <= minute_of_day <= 15*60 + 30):
        return None
    return {"severity": "CONFIRM",
            "reasoning": f"Volume {v:.1f}× in the afternoon — afternoon volume has higher signal than morning gappy volume."}
